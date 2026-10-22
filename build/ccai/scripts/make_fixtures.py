"""Generate the synthetic CCAI call-log fixtures (reproducible).

    python build/ccai/scripts/make_fixtures.py            # from the repo root
    python build/ccai/scripts/make_fixtures.py --calls 200 --seed 11

Writes
  build/connectors/fixtures/m365/sites/default/CCAI/CallLogs/YYYY/MM/DD/
      conv_<id>.json         CCAI Insights-style conversation export (~75%)
      transcript_<id>.txt    plain-text transcript export (~12%)
      <id>.vtt               WebVTT caption transcript (~12%)
      manifest_YYYYMMDD.csv  daily manifest of the calls in that folder
  build/ccai/tests/ground_truth.json   labels (primary / secondary leaf, resolution)
                                       — kept OUT of the fixtures so accuracy is measurable

Calls reference real synthetic accounts from bsp_seed `build(customers=20000, seed=7)`:
the caller reads out the account's 10-digit BAN (`bsp_seed.ban_for`) and name, and the
call is consistent with the account's technology — copper (DSL: modem sync, noise margin,
line test, DSLAM port, central office) or fiber (FTTH: ONT, light levels, drop fiber) —
and its wire center (CLLI) and plan. DataPlatform sells only copper and fiber internet.
Fiber-only reasons (ONT placement, post-migration issues) go to fiber accounts; copper-to-
fiber upgrade / retirement / availability reasons go to copper accounts.
The customer, location and plan tables do not depend on `months`, so a short
simulation (`--months 3`, the default) yields exactly the same accounts as the
full 24-month warehouse seed, in a fraction of the time.

Every leaf of config/taxonomy/ccai_taxonomy.yaml appears at least once; ~30% of
calls are multi-intent. Customer turns come mainly from HELD-OUT phrasings
(scripts/fixture_phrases.py — not the taxonomy examples the offline tagger trains
on; a canonical example appears in a minority of follow-up turns), with synonym
swaps, fillers, dropped words and ASR-style mis-recognitions, padded with realistic
verification / hold / closing chatter.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture_phrases import PHRASES  # noqa: E402  held-out phrasings (not the taxonomy examples)

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "build" / "seed"))

TAXONOMY = ROOT / "config" / "taxonomy" / "ccai_taxonomy.yaml"
OUT_DIR = ROOT / "build" / "connectors" / "fixtures" / "m365" / "sites" / "default" / "CCAI" / "CallLogs"
GT_PATH = ROOT / "build" / "ccai" / "tests" / "ground_truth.json"
ET = timezone(timedelta(hours=-4))  # US Eastern (daylight time) — call centre local time

QUEUES = {"BIL": "Billing", "TEC": "Repair", "OUT": "Repair", "INS": "Installation", "ORD": "Sales & Service",
          "RET": "Retention", "ACC": "Customer Care", "EQP": "Repair", "FBR": "Sales & Service", "SAL": "Sales",
          "CMP": "Customer Care", "DIG": "Customer Care"}
# common contact reasons are more frequent than rare ones
L1_WEIGHT = {"BIL": 5, "TEC": 5, "OUT": 3, "INS": 2.5, "ORD": 2, "RET": 2.5, "ACC": 1, "EQP": 1.5, "FBR": 1.5,
             "SAL": 1, "CMP": 1, "DIG": 1}
AGENTS = [("AG10411", "Denise"), ("AG10418", "Marcus"), ("AG10422", "Tanya"), ("AG10431", "Luis"), ("AG10437", "Keisha"),
          ("AG10440", "Brandon"), ("AG10452", "Priya"), ("AG10457", "Owen"), ("AG10463", "Carla"), ("AG10470", "Jamal"),
          ("AG10476", "Heather"), ("AG10481", "Victor"), ("AG10488", "Monique"), ("AG10493", "Tyler"), ("AG10502", "Rosa"),
          ("AG10509", "Grant"), ("AG10515", "Alicia"), ("AG10521", "Devon"), ("AG10528", "Shannon"), ("AG10534", "Kevin")]

SYNONYMS = {
    "internet": ["internet", "service", "wifi", "inner net", "connection"],
    "bill": ["bill", "statement", "invoice", "bill"],
    "cancel": ["cancel", "disconnect", "shut off", "stop"],
    "technician": ["technician", "tech", "service guy", "installer"],
    "modem": ["modem", "router", "box", "mode um"],
    "router": ["router", "modem", "router"],
    "charged": ["charged", "billed", "charged"],
    "price": ["price", "rate", "cost"],
    "want": ["want", "wanna", "need", "would like"],
    "really": ["really", "super", "so"],
    "called": ["called", "called in", "phoned"],
    "account": ["account", "acount", "account"],
    "fiber": ["fiber", "fibre", "fiber optic", "five ber"],
    "appointment": ["appointment", "appointment", "visit"],
    "payment": ["payment", "payment", "pament"],
    "outage": ["outage", "out age", "outage"],
}
ASR = [(r"\bto\b", "two"), (r"\byour\b", "you're"), (r"\btheir\b", "there"), (r"\bfor\b", "four"),
       (r"\bDataPlatform\b", "bright speed"), (r"\bwould\b", "wood"), (r"\bknow\b", "no"), (r"\bright\b", "write")]
OPENERS = ["", "", "yeah hi so ", "um ", "okay so ", "hi yes ", "uh yeah ", "so basically ", "hey how are you, so ",
           "alright so ", "yes ma'am so ", "uh hi, "]
FILLERS = ["um", "uh", "like", "you know", "I mean"]
TOPIC_AGENT = {
    "BIL": ["Let me pull up your billing details real quick.", "I can definitely look at that bill with you.",
            "Okay, I'm looking at your last two statements now."],
    "TEC": ["I'm sorry you're dealing with that, let me run a line test.", "Let's see what's going on, I'm checking the connection from here.",
            "Okay, I'm going to send a refresh signal to the equipment."],
    "OUT": ["Let me check the network status for your area.", "I'm looking at the outage map for your address now.",
            "Give me a second to look at what's happening in your area."],
    "INS": ["Let me look at your order and the appointment details.", "I'm pulling up the work order now.",
            "Okay, I see the field work order on your account."],
    "ORD": ["Let me see what options we have at your address.", "I can help with that, let me look at the order.",
            "Sure, I'm checking what's available for you."],
    "RET": ["I'm sorry to hear that, let me see what I can do.", "I understand, let me review the account before we do anything.",
            "Okay, I do want to make sure we look at all your options."],
    "ACC": ["I can help update that for you.", "Sure, let me open your profile.", "Okay, let me check what we have on file."],
    "EQP": ["Let me look at the equipment we have on your account.", "Okay, I'm checking the device details.",
            "I can see the equipment serial number here."],
    "FBR": ["Let me check fiber availability for your address.", "I'm looking at the build information for your area.",
            "Okay, let me check what we have planned for your street."],
    "SAL": ["I'd be happy to help you with that.", "Great, let me check the address for service.", "Sure, let me look at our current offers."],
    "CMP": ["I'm really sorry about that experience.", "I understand your frustration, and I want to help fix this.",
            "I hear you, let me see what happened."],
    "DIG": ["Let me help you with the app.", "Okay, I'm looking at your online account now.", "I can see your digital profile here."],
}
RESOLUTION_AGENT = {
    "resolved": ["Okay, I've taken care of that for you and you should be all set.",
                 "That's done on my end, you should see it reflected right away.",
                 "Alright, that's been fixed and I've added a note to the account."],
    "pending_followup": ["I've opened a ticket, number {ticket}, and someone will follow up within 24 to 48 hours.",
                         "I've scheduled a technician for {day} between eight and noon, ticket {ticket}.",
                         "I've submitted that request, it can take three to five business days, reference {ticket}."],
    "escalated": ["I'm going to transfer you to my supervisor now, please hold.",
                  "I'm escalating this to our escalations team and transferring you over."],
    "unresolved": ["I'm sorry I wasn't able to resolve that for you today.",
                   "Unfortunately there's nothing more I can do on this call."],
}
CUSTOMER_REACTION = {
    "resolved": ["Okay great, thank you so much.", "Perfect, that's what I needed.", "Alright, thanks for your help."],
    "pending_followup": ["Okay, I'll wait for the call then.", "Fine, as long as someone actually shows up.", "Okay, thanks."],
    "escalated": ["Yes please, put me through.", "Fine, transfer me."],
    "unresolved": ["That doesn't help me at all.", "This is ridiculous, I'm not happy.", "So I just have to deal with it, great."],
}
# technology-specific reasons: the account must be on this technology
FIBER_ONLY = {"INS.DEL.ONT_PLACEMENT", "FBR.MIG.MIGRATION_ISSUE"}
COPPER_ONLY = {"FBR.MIG.COPPER_TO_FIBER", "FBR.MIG.COPPER_RETIREMENT", "FBR.AVL.FIBER_AVAILABILITY",
               "FBR.AVL.BUILD_TIMELINE", "FBR.AVL.ADDRESS_NOT_FOUND"}
# phrases that only make sense on one technology
FIBER_WORDS = re.compile(r"\b(gig|fiber|fibre|ont|optical)\b", re.I)
COPPER_WORDS = re.compile(r"\b(dsl|copper|phone line)\b", re.I)
COPPER_SWAP = {  # fiber-flavoured held-out phrasings, re-voiced for a copper DSL caller
    "I pay for the gig and I ran a test and it's showing like eighty":
        "I pay for the eighty meg and I ran a test and it's showing like twelve",
    "how much more for the two gig plan": "how much more for the hundred and forty meg plan",
}
# agent-side line diagnostics (agent turns are not tagged; they make the call read true to the plant)
PLANT_GROUPS = {"TEC.CON", "TEC.TS", "OUT.STAT", "OUT.RPT", "OUT.NOT", "EQP.MOD", "INS.APPT", "INS.DEL", "INS.FLD"}
TECH_AGENT = {
    "copper": ["I can see you're on our DSL service out of the {clli} central office, I'm running a line test now.",
               "Okay, your DSL modem is syncing at about {sync} meg right now and the noise margin is on the low side.",
               "I'm checking the DSLAM port for your line in the {clli} office, give me a second.",
               "The line test shows a long copper loop, about {loop} thousand feet from the {clli} office."],
    "fiber": ["I can see you're on fiber out of the {clli} office, I'm checking the ONT now.",
              "Okay, I'm pinging the ONT at the house, the light levels are coming back around minus {rx} dBm.",
              "Your fiber line runs back to the {clli} office, I'm checking the ONT status from here.",
              "I can see your {ont} ONT online, let me look at the optical readings."],
}
TECH_CUSTOMER = {  # the caller naming their service, appended to the opener
    "copper": ["I have the DSL.", "It's the DSL through the phone jack.", "We've got the copper DSL internet."],
    "fiber": ["I have the fiber.", "We're on the fiber service.", "It's the fiber internet."],
}
TECH_FBR_AGENT = {
    "copper": ["I can see your address is on copper DSL from the {clli} office, let me check the fiber build there.",
               "You're on our DSL service today, let me look at the fiber plans for the {clli} wire center."],
    "fiber": ["I can see you're already on fiber from the {clli} office."],
}
CHATTER = ["hold on let me grab the bill", "okay", "uh huh", "yeah", "sorry the dog is barking", "can you hear me okay",
           "hang on one second", "mm hmm", "right", "okay go ahead"]


def spoken_ban(ban: str, rng: random.Random) -> str:
    """The 10-digit BAN as a caller says it: whole, grouped 3-3-4, or digit by digit."""
    words = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
    style = rng.random()
    if style < 0.40:
        return ban
    if style < 0.65:
        return f"{ban[:3]} {ban[3:6]} {ban[6:]}"
    if style < 0.80:
        return " ".join(ban)
    return " ".join(rng.choice(["zero", "oh"]) if d == "0" else words[int(d)] for d in ban)


def fits(text: str, tech: str) -> bool:
    return not (FIBER_WORDS.search(text) if tech == "copper" else COPPER_WORDS.search(text))


def for_tech(texts: list[str], tech: str, leaf_id: str, need: int = 2) -> list[str]:
    """Phrasings consistent with the account's technology (re-voiced or as-is when too few fit).
    Fiber-upgrade, sales, retention and complaint talk may name fiber whatever the caller has today."""
    if leaf_id.split(".")[0] in ("FBR", "SAL", "RET", "CMP"):
        return texts
    if tech == "copper":
        texts = [COPPER_SWAP.get(t, t) for t in texts]
    ok = [t for t in texts if fits(t, tech)]
    return ok if len(ok) >= need else ok + [t for t in texts if t not in ok][: need - len(ok)]


def leaf_fits(leaf_id: str, tech: str) -> bool:
    return not ((leaf_id in FIBER_ONLY and tech != "fiber") or (leaf_id in COPPER_ONLY and tech != "copper"))


def paraphrase(text: str, rng: random.Random, noise: float = 1.0) -> str:
    t = text.strip()
    for w, alts in SYNONYMS.items():
        if re.search(rf"\b{w}\b", t, re.I) and rng.random() < 0.5 * noise:
            t = re.sub(rf"\b{w}\b", rng.choice(alts), t, count=1, flags=re.I)
    for pat, rep in ASR:
        if re.search(pat, t) and rng.random() < 0.25 * noise:
            t = re.sub(pat, rep, t, count=1)
    words = t.split()
    out = []
    for w in words:
        r = rng.random()
        if r < 0.04 * noise and len(words) > 6:
            continue  # dropped by ASR
        if r > 1 - 0.04 * noise:
            out.append(rng.choice(FILLERS))
        if r > 1 - 0.015 * noise:
            out.append(w)  # stutter / repeat
        out.append(w)
    t = " ".join(out)
    if rng.random() < 0.08 * noise:
        cut = rng.randint(3, max(3, len(out) - 1))
        t = " ".join(t.split()[:cut]) + " [inaudible] " + " ".join(t.split()[cut:])
    t = rng.choice(OPENERS) + t
    if rng.random() < 0.35:  # many STT exports are lower-case without punctuation
        t = re.sub(r"[.,?!]", "", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def conv_id(rng: random.Random) -> str:
    return "%016x" % rng.getrandbits(64)


def load_taxonomy() -> tuple[str, list[dict]]:
    doc = yaml.safe_load(TAXONOMY.read_text())
    leaves = []
    for d in doc["domains"]:
        for g in d["groups"]:
            for leaf in g["leaves"]:
                leaves.append({**leaf, "l1": d["id"], "l1_name": d["name"], "l2": g["id"], "l2_name": g["name"]})
    return str(doc.get("version")), leaves


def load_accounts(months: int):
    from bsp_seed.generate import build

    ds = build(customers=20000, months=months, seed=7)
    loc = ds["service_locations"][["ban", "technology", "ont_model", "loop_length_kft"]]
    cust = ds["customers"][["ban", "customer_name", "clli", "phone"]].merge(loc, on="ban")
    cust = cust.merge(ds["subscriptions"][["ban", "plan_id"]], on="ban")
    plans = ds["plans"].set_index("plan_id")
    cust["plan_name"] = plans.plan_name.reindex(cust.plan_id).to_numpy()
    cust["plan_spoken"] = cust.plan_name.str.replace(" (copper)", "", regex=False)  # "Internet 80", "Fiber Gig"
    cust["down_mbps"] = plans.down_mbps.reindex(cust.plan_id).to_numpy()
    cust["monthly_price"] = plans.monthly_price.reindex(cust.plan_id).to_numpy()
    return cust.to_dict("records")


def build_call(rng: random.Random, leaf: dict, secondaries: list[dict], acct: dict, start: datetime) -> dict:
    agent_id, agent_name = rng.choice(AGENTS)
    first = acct["customer_name"].split()[0]
    churny = leaf.get("churn_risk") or leaf["l1"] in ("CMP", "RET")
    base = -0.55 if churny else (-0.25 if leaf["l1"] in ("BIL", "TEC", "OUT", "INS", "EQP") else 0.15)
    if leaf["id"] == "CMP.ESC.SUPERVISOR_REQUEST":
        resolution = "escalated"
    else:
        resolution = rng.choices(["resolved", "pending_followup", "escalated", "unresolved"], [55, 25, 8, 12])[0]
    end_s = {"resolved": 0.45, "pending_followup": 0.05, "escalated": -0.6, "unresolved": -0.7}[resolution]
    turns: list[tuple[str, str]] = []  # (role, text)
    A, C = "AGENT", "CUSTOMER"
    turns.append((A, rng.choice([f"Thank you for calling DataPlatform, my name is {agent_name}. Can I start with the account number or the phone number on the account?",
                                 f"Thanks for calling DataPlatform, this is {agent_name}, who do I have the pleasure of speaking with today?",
                                 f"DataPlatform customer care, {agent_name} speaking, can I get your account number please?"])))
    turns.append((C, rng.choice(["yeah it's ", "sure, account number is ", "um hold on, it's ", "it's "]) + spoken_ban(acct["ban"], rng)))
    turns.append((A, "Thank you. And can you verify the name on the account for me?"))
    turns.append((C, acct["customer_name"] if rng.random() < 0.8 else acct["customer_name"].lower()))
    turns.append((A, rng.choice([f"Thank you {first}. How can I help you today?", f"Perfect, thanks {first}, what can I do for you?",
                                 "Thanks for verifying. What's going on today?"])))
    tech = acct["technology"]
    held = for_tech(list(PHRASES[leaf["id"]]), tech, leaf["id"])
    rng.shuffle(held)
    ex = for_tech(list(leaf["examples"]), tech, leaf["id"])
    rng.shuffle(ex)
    opener = paraphrase(held[0], rng)
    r = rng.random()
    if r < 0.4:
        opener += " " + rng.choice([f"I'm on the {acct['plan_spoken']} plan.", "It's been like this for a few days.",
                                    "I've been a customer a long time.", "I'm calling from my cell."])
    elif r < 0.65 and leaf["l2"] in PLANT_GROUPS:
        opener += " " + rng.choice(TECH_CUSTOMER[tech])
    turns.append((C, opener))
    turns.append((A, rng.choice(TOPIC_AGENT[leaf["l1"]])))
    loop = acct["loop_length_kft"] if tech == "copper" and acct["loop_length_kft"] == acct["loop_length_kft"] else 6.0
    facts = {"clli": acct["clli"], "ont": acct.get("ont_model") or "fiber", "loop": f"{loop:.0f}",
             "sync": f"{min(acct['down_mbps'], max(3, 160 - 8.5 * loop)) * rng.uniform(0.55, 0.95):.0f}",
             "rx": f"{rng.uniform(16, 27):.1f}"}
    if leaf["l2"] in PLANT_GROUPS and rng.random() < 0.75:
        turns.append((A, rng.choice(TECH_AGENT[tech]).format(**facts)))
    elif leaf["l1"] == "FBR" and rng.random() < 0.75:
        turns.append((A, rng.choice(TECH_FBR_AGENT[tech]).format(**facts)))
    if rng.random() < 0.5:
        turns.append((C, rng.choice(CHATTER)))
        turns.append((A, rng.choice(["No problem, take your time.", "Okay.", "Thanks for your patience."])))
    turns.append((A, rng.choice(["Can you tell me a little more about what's happening?", "When did you first notice this?",
                                 "Okay, and is this the first time you've called about it?"])))
    # follow-up detail: the other held-out phrasing; only sometimes a (noisy) canonical example
    turns.append((C, paraphrase(held[1] if rng.random() < 0.7 else ex[0], rng)))
    if rng.random() < 0.25:
        turns.append((A, rng.choice(["I see.", "Got it.", "Okay, I understand."])))
        turns.append((C, paraphrase(ex[1], rng, noise=1.5)))
    turns.append((A, rng.choice(["Okay, give me just a moment while I look into this.", "Let me place you on a brief hold while I check.",
                                 "Bear with me one second."])))
    ticket = f"TK{rng.randint(10_000_000, 99_999_999)}"
    day = (start + timedelta(days=rng.randint(1, 4))).strftime("%A")
    turns.append((A, rng.choice(RESOLUTION_AGENT[resolution]).format(ticket=ticket, day=day)))
    turns.append((C, rng.choice(CUSTOMER_REACTION[resolution])))
    for sec in secondaries:
        sex = for_tech(list(PHRASES[sec["id"]]), tech, sec["id"])
        rng.shuffle(sex)
        cue = rng.choice(["oh and one more thing, ", "also while I have you, ", "and another thing, ", "oh before you go, ",
                          "", "", "oh yeah, "])  # not every caller signposts the second issue
        turns.append((C, cue + paraphrase(sex[0], rng)))
        turns.append((A, rng.choice(TOPIC_AGENT[sec["l1"]])))
        if rng.random() < 0.4:
            turns.append((C, paraphrase(sex[1], rng)))
        turns.append((A, rng.choice(["Okay, I've made a note of that as well.", "Alright, I've taken care of that too.",
                                     "I've added that to the ticket."])))
    turns.append((A, "Is there anything else I can help you with today?"))
    turns.append((C, rng.choice(["no that's it", "No, that's all, thanks.", "nope", "No. Thanks."]) if resolution != "unresolved"
                  else rng.choice(["No. I'm just frustrated.", "no", "Not unless you can actually fix it."])))
    turns.append((A, f"Thank you for choosing DataPlatform {first}, have a great day."))

    # timing + sentiment
    t = rng.uniform(1.0, 3.0)
    out_turns = []
    n_c = sum(1 for r, _ in turns if r == C)
    ci = 0
    for role, text in turns:
        words = max(1, len(text.split()))
        dur = words / rng.uniform(2.2, 3.2) + rng.uniform(0.3, 1.2)
        if role == C:
            frac = ci / max(1, n_c - 1)
            ci += 1
            s = base + (end_s - base) * frac + rng.uniform(-0.15, 0.15)
        else:
            s = 0.2 + rng.uniform(-0.1, 0.25)
        out_turns.append({"role": role, "text": text, "start": round(t, 2), "end": round(t + dur, 2),
                          "sentiment": round(max(-1, min(1, s)), 2), "confidence": round(rng.uniform(0.78, 0.97), 3)})
        held = role == A and any(k in text for k in ("look into this", "brief hold", "Bear with me", "Please hold", "transfer"))
        gap = rng.uniform(0.4, 2.0) + (rng.uniform(25, 150) if held else 0)
        t += dur + gap
    return {"conversation_id": conv_id(rng), "agent_id": agent_id, "agent_name": agent_name,
            "queue": QUEUES[leaf["l1"]] if rng.random() < 0.8 else "General", "start": start,
            "duration": round(t + rng.uniform(1, 4), 1), "ani": "***-***-" + acct["phone"][-4:],
            "turns": out_turns, "ban": acct["ban"], "technology": tech, "clli": acct["clli"],
            "plan_id": acct["plan_id"], "resolution": resolution}


def _offset(s: float) -> str:
    return f"{s:.3f}s"


def to_insights_json(call: dict) -> bytes:
    segs = []
    sil = 0.0
    prev_end = 0.0
    for tr in call["turns"]:
        words = tr["text"].split()
        step = (tr["end"] - tr["start"]) / max(1, len(words))
        sil += max(0.0, tr["start"] - prev_end)
        prev_end = tr["end"]
        segs.append({
            "text": tr["text"], "confidence": tr["confidence"], "languageCode": "en-US",
            "channelTag": 2 if tr["role"] == "AGENT" else 1,
            "segmentParticipant": {"role": "HUMAN_AGENT" if tr["role"] == "AGENT" else "END_USER"},
            "words": [{"word": w, "startOffset": _offset(tr["start"] + i * step),
                       "endOffset": _offset(tr["start"] + (i + 1) * step)} for i, w in enumerate(words)],
            "sentiment": {"score": tr["sentiment"], "magnitude": round(abs(tr["sentiment"]) * 1.4 + 0.1, 2)},
        })
    start = call["start"].astimezone(timezone.utc)
    cid = call["conversation_id"]
    cust = [t["sentiment"] for t in call["turns"] if t["role"] == "CUSTOMER"]
    doc = {
        "name": f"projects/bsp-ccai-prod/locations/us-central1/conversations/{cid}",
        "dataSource": {"gcsSource": {"audioUri": f"gs://bsp-ccai-audio/{start:%Y/%m/%d}/{cid}.wav"}},
        "createTime": (start + timedelta(seconds=call["duration"] + 90)).isoformat().replace("+00:00", "Z"),
        "startTime": start.isoformat().replace("+00:00", "Z"),
        "duration": _offset(call["duration"]), "turnCount": len(segs), "languageCode": "en-US",
        "medium": "PHONE_CALL", "agentId": call["agent_id"],
        "labels": {"queue": call["queue"], "ani": call["ani"], "site": "raleigh-cc", "brand": "dataplatform"},
        "callMetadata": {"customerChannel": 1, "agentChannel": 2},
        "transcript": {"transcriptSegments": segs},
        "latestAnalysis": {
            "name": f"projects/bsp-ccai-prod/locations/us-central1/conversations/{cid}/analyses/1",
            "analysisResult": {"callAnalysisMetadata": {
                "sentiments": [{"channelTag": 1, "sentimentData": {"score": round(sum(cust) / len(cust), 2),
                                                                  "magnitude": round(sum(abs(x) for x in cust), 2)}}],
                "silence": {"silenceDuration": _offset(sil), "silencePercentage": round(100 * sil / call["duration"], 1)},
            }}},
    }
    return json.dumps(doc, indent=1).encode()


def _hms(s: float, ms: bool = False) -> str:
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{sec:06.3f}" if ms else f"{int(h):02d}:{int(m):02d}:{int(sec):02d}"


def to_text(call: dict) -> bytes:
    lines = ["DataPlatform Contact Center - Call Transcript",
             f"Conversation ID: {call['conversation_id']}",
             f"Start Time: {call['start'].isoformat()}",
             f"Agent ID: {call['agent_id']}",
             f"Queue: {call['queue']}",
             f"Caller ANI: {call['ani']}",
             f"Duration: {_hms(call['duration'])}",
             "-" * 60]
    for tr in call["turns"]:
        lines.append(f"[{_hms(tr['start'])}] {'Agent' if tr['role'] == 'AGENT' else 'Customer'}: {tr['text']}")
    return ("\n".join(lines) + "\n").encode()


def to_vtt(call: dict) -> bytes:
    out = ["WEBVTT", "",
           f"NOTE conversation_id={call['conversation_id']} agent_id={call['agent_id']} queue={call['queue']} "
           f"start={call['start'].isoformat()} ani={call['ani']} duration={call['duration']}", ""]
    for i, tr in enumerate(call["turns"], 1):
        who = "Agent" if tr["role"] == "AGENT" else "Customer"
        out += [str(i), f"{_hms(tr['start'], True)} --> {_hms(tr['end'], True)}", f"<v {who}>{tr['text']}", ""]
    return "\n".join(out).encode()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--calls", type=int, default=200)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--months", type=int, default=3, help="bsp_seed simulation months (accounts are identical for any value)")
    ap.add_argument("--start", default="2026-10-05")
    ap.add_argument("--days", type=int, default=10)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    version, leaves = load_taxonomy()
    accounts = load_accounts(args.months)
    by_tech: dict[str, list[dict]] = {}
    for a in accounts:
        by_tech.setdefault(a["technology"], []).append(a)

    # primary leaves: every leaf once, the rest weighted by domain volume
    primaries = list(leaves)
    weights = [L1_WEIGHT[x["l1"]] for x in leaves]
    primaries += rng.choices(leaves, weights=weights, k=max(0, args.calls - len(leaves)))
    rng.shuffle(primaries)

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)
    d0 = datetime.fromisoformat(args.start).replace(tzinfo=ET)
    truth: dict[str, dict] = {}
    manifests: dict[str, list[dict]] = {}
    for i, leaf in enumerate(primaries[: args.calls]):
        tech_pref = "fiber" if leaf["id"] in FIBER_ONLY else "copper" if leaf["id"] in COPPER_ONLY else None
        acct = rng.choice(by_tech[tech_pref]) if tech_pref else rng.choice(accounts)
        secs: list[dict] = []
        r = rng.random()
        if r < 0.30:
            k = 2 if r < 0.05 else 1
            pool = [x for x in leaves if x["l2"] != leaf["l2"] and leaf_fits(x["id"], acct["technology"])]
            secs = rng.sample(pool, k)
        day = d0 + timedelta(days=i % args.days)
        start = day.replace(hour=rng.randint(8, 19), minute=rng.randint(0, 59), second=rng.randint(0, 59))
        call = build_call(rng, leaf, secs, acct, start)
        fmt = rng.choices(["json", "txt", "vtt"], [76, 12, 12])[0]
        cid = call["conversation_id"]
        folder = OUT_DIR / f"{start:%Y/%m/%d}"
        folder.mkdir(parents=True, exist_ok=True)
        name = {"json": f"conv_{cid}.json", "txt": f"transcript_{cid}.txt", "vtt": f"{cid}.vtt"}[fmt]
        data = {"json": to_insights_json, "txt": to_text, "vtt": to_vtt}[fmt](call)
        (folder / name).write_bytes(data)
        rel = f"{start:%Y/%m/%d}/{name}"
        truth[cid] = {"file": rel, "format": fmt, "primary": leaf["id"], "secondary": [s["id"] for s in secs],
                      "ban": call["ban"], "technology": call["technology"], "clli": call["clli"],
                      "plan_id": call["plan_id"], "resolution": call["resolution"],
                      "churn_risk": bool(leaf.get("churn_risk") or any(s.get("churn_risk") for s in secs))}
        manifests.setdefault(f"{start:%Y/%m/%d}", []).append({
            "conversation_id": cid, "file_name": name, "format": fmt, "agent_id": call["agent_id"], "queue": call["queue"],
            "start_time": start.isoformat(), "duration_sec": call["duration"], "ani_masked": call["ani"],
            "sha256": hashlib.sha256(data).hexdigest()})

    for day, rows in sorted(manifests.items()):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["start_time"]))
        (OUT_DIR / day / f"manifest_{day.replace('/', '')}.csv").write_text(buf.getvalue())
    # one truncated export, as happens when an upload is interrupted (exercises error handling / re-run)
    last_day = sorted(manifests)[-1]
    (OUT_DIR / last_day / "conv_partial_upload.json").write_text('{"name": "projects/bsp-ccai-prod/locations/us-central1/conversations/'
                                                                  'ffffffffffffffff", "transcript": {"transcriptSegments": [{"text": "thank you for')

    GT_PATH.parent.mkdir(parents=True, exist_ok=True)
    GT_PATH.write_text(json.dumps({"taxonomy_version": version, "generator": "build/ccai/scripts/make_fixtures.py",
                                   "seed": args.seed, "calls": truth}, indent=1, sort_keys=True))
    fmts = {f: sum(1 for t in truth.values() if t["format"] == f) for f in ("json", "txt", "vtt")}
    covered = len({t["primary"] for t in truth.values()})
    multi = sum(1 for t in truth.values() if t["secondary"])
    print(f"wrote {len(truth)} calls {fmts}, {len(manifests)} manifests, leaves covered {covered}/{len(leaves)}, "
          f"multi-intent {multi} → {OUT_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
