"""Parse CCAI call exports into one normalised call record.

Supported: CCAI Insights conversation JSON (transcript.transcriptSegments),
a generic {"turns":[…]} JSON, plain-text transcripts ("[hh:mm:ss] Agent: …"),
WebVTT (<v Speaker> cues, NOTE metadata) and daily CSV manifests.

Normalised record:
  call_id, format, started_at (ISO), duration_sec, agent_id, queue, ani_masked,
  language, ban, turns[{id, role: agent|customer|system, text, start, end, sentiment}],
  metrics{agent_talk_sec, customer_talk_sec, agent_talk_pct, silence_sec, silence_pct,
          avg_customer_sentiment, sentiment_start, sentiment_end}, meta{…}
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath

NUM_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
             "seven": "7", "eight": "8", "nine": "9"}
# BAN (billing account number): 10 digits, DataPlatform BANs start with 3 — said as one number,
# grouped ("310 000 0008", "310-000-0008") or digit by digit ("three one zero oh …").
BAN_RE = re.compile(r"3\d{9}")
DIGIT_RUN_RE = re.compile(r"\d+(?:[\s.-]+\d+)*")

POS = {"thank", "thanks", "great", "perfect", "appreciate", "awesome", "good", "helpful", "happy", "love", "resolved",
       "fixed", "excellent", "wonderful", "glad", "works", "nice"}
NEG = {"ridiculous", "frustrated", "angry", "terrible", "awful", "worst", "cancel", "never", "unacceptable", "rude",
       "problem", "down", "slow", "broken", "not", "doesn't", "won't", "can't", "upset", "fed", "again", "still", "waiting",
       "hour", "complaint", "lied", "overcharged", "unhappy", "disappointed", "useless", "horrible", "late", "damage"}


class ParseError(ValueError):
    pass


def detect(name: str, data: bytes) -> str:
    ext = PurePosixPath(name.lower()).suffix
    head = data[:64].lstrip()
    if ext == ".vtt" or head.startswith(b"WEBVTT"):
        return "vtt"
    if ext == ".csv":
        return "manifest"
    if ext == ".json" or head.startswith(b"{"):
        return "insights_json"
    if ext in (".txt", ".log", ""):
        return "text"
    return "unsupported"


def lexicon_sentiment(text: str) -> float:
    words = re.findall(r"[a-z']+", text.lower())
    if not words:
        return 0.0
    p = sum(w in POS for w in words)
    n = sum(w in NEG for w in words)
    if p == n == 0:
        return 0.0
    return round(max(-1.0, min(1.0, (p - n) / (p + n) * min(1.0, (p + n) / 3))), 2)


def find_ban(texts: list[str]) -> str | None:
    """First 10-digit BAN in the texts (written, grouped or spoken digit by digit)."""
    for t in texts:
        norm = " ".join(NUM_WORDS.get(w, w) for w in re.findall(r"[a-z0-9]+", t.lower()))
        for m in DIGIT_RUN_RE.finditer(norm):
            groups = re.split(r"[\s.-]+", m.group(0))
            for i in range(len(groups)):          # a BAN may sit inside a longer run of spoken digits
                acc = ""
                for g in groups[i:]:
                    acc += g
                    if len(acc) >= 10:
                        break
                if len(acc) == 10 and BAN_RE.fullmatch(acc):
                    return acc
    return None


def _secs(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):  # protobuf Duration {"seconds": .., "nanos": ..}
        return float(v.get("seconds", 0)) + float(v.get("nanos", 0)) / 1e9
    s = str(v).strip()
    if s.endswith("s") and re.match(r"^[\d.]+s$", s):
        return float(s[:-1])
    if re.match(r"^\d+:\d{2}(:\d{2})?(\.\d+)?$", s):
        parts = [float(x) for x in s.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0.0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    try:
        return float(s)
    except ValueError:
        return None


def _iso(v) -> str | None:
    if not v:
        return None
    s = str(v).strip().replace(" ET", "")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _role(v: str) -> str:
    v = (v or "").upper()
    if v in ("HUMAN_AGENT", "AGENT", "AUTOMATED_AGENT", "REP", "ADVISOR") or v.startswith("AGENT"):
        return "agent"
    if v in ("END_USER", "CUSTOMER", "CALLER", "USER") or v.startswith("CUST"):
        return "customer"
    return "system"


def _metrics(turns: list[dict], duration: float | None) -> dict:
    a = sum(max(0.0, (t["end"] or 0) - (t["start"] or 0)) for t in turns if t["role"] == "agent")
    c = sum(max(0.0, (t["end"] or 0) - (t["start"] or 0)) for t in turns if t["role"] == "customer")
    spans = sorted((t["start"] or 0, t["end"] or 0) for t in turns if t["end"] is not None)
    speech, cur_s, cur_e = 0.0, None, None
    for s, e in spans:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                speech += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        speech += cur_e - cur_s
    dur = duration or (spans[-1][1] if spans else 0.0)
    silence = max(0.0, dur - speech) if dur else 0.0
    cs = [t["sentiment"] for t in turns if t["role"] == "customer" and t.get("sentiment") is not None]
    third = max(1, len(cs) // 3)
    return {"agent_talk_sec": round(a, 1), "customer_talk_sec": round(c, 1),
            "agent_talk_pct": round(100 * a / (a + c), 1) if a + c else None,
            "silence_sec": round(silence, 1), "silence_pct": round(100 * silence / dur, 1) if dur else None,
            "avg_customer_sentiment": round(sum(cs) / len(cs), 3) if cs else None,
            "sentiment_start": round(sum(cs[:third]) / third, 3) if cs else None,
            "sentiment_end": round(sum(cs[-third:]) / third, 3) if cs else None}


def _finish(rec: dict) -> dict:
    turns = rec["turns"]
    if not turns:
        raise ParseError("no transcript turns found")
    for i, t in enumerate(turns):
        t["id"] = i
        t["text"] = re.sub(r"\s+", " ", t["text"]).strip()
        if t.get("sentiment") is None:
            t["sentiment"] = lexicon_sentiment(t["text"]) if t["role"] == "customer" else None
            t["sentiment_source"] = "lexicon"
    if not rec.get("duration_sec"):
        rec["duration_sec"] = max((t["end"] or 0) for t in turns) if turns else None
    rec["ban"] = rec.get("ban") or find_ban([t["text"] for t in turns if t["role"] == "customer"])
    rec["metrics"] = _metrics(turns, rec.get("duration_sec"))
    rec.setdefault("language", "en-US")
    return rec


def parse_insights_json(data: bytes, name: str) -> dict:
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ParseError(f"invalid JSON ({e.__class__.__name__}: {e})") from None
    if not isinstance(doc, dict):
        raise ParseError("JSON root is not an object")
    if "conversation" in doc and isinstance(doc["conversation"], dict):  # some exports wrap it
        doc = doc["conversation"]
    labels = doc.get("labels") or {}
    turns: list[dict] = []
    segs = (doc.get("transcript") or {}).get("transcriptSegments")
    if segs is not None:
        role_by_channel = {}
        cm = doc.get("callMetadata") or {}
        if cm.get("agentChannel") is not None:
            role_by_channel[cm["agentChannel"]] = "agent"
        if cm.get("customerChannel") is not None:
            role_by_channel[cm["customerChannel"]] = "customer"
        for s in segs:
            words = s.get("words") or []
            role = _role((s.get("segmentParticipant") or {}).get("role", ""))
            if role == "system" and s.get("channelTag") in role_by_channel:
                role = role_by_channel[s["channelTag"]]
            start = _secs(words[0].get("startOffset")) if words else _secs(s.get("startOffset"))
            end = _secs(words[-1].get("endOffset")) if words else _secs(s.get("endOffset"))
            sent = (s.get("sentiment") or {}).get("score")
            turns.append({"role": role, "text": s.get("text") or " ".join(w.get("word", "") for w in words),
                          "start": start, "end": end, "sentiment": sent, "asr_confidence": s.get("confidence")})
    elif isinstance(doc.get("turns"), list):
        for t in doc["turns"]:
            turns.append({"role": _role(t.get("role") or t.get("speaker", "")), "text": t.get("text", ""),
                          "start": _secs(t.get("start")), "end": _secs(t.get("end")), "sentiment": t.get("sentiment")})
    else:
        raise ParseError("JSON has neither transcript.transcriptSegments nor turns")
    cid = (doc.get("name") or "").rsplit("/", 1)[-1] or doc.get("conversation_id") or doc.get("id") or PurePosixPath(name).stem
    analysis = ((doc.get("latestAnalysis") or {}).get("analysisResult") or {}).get("callAnalysisMetadata") or {}
    return _finish({"call_id": str(cid), "format": "insights_json", "started_at": _iso(doc.get("startTime")),
                    "duration_sec": _secs(doc.get("duration")), "agent_id": doc.get("agentId") or labels.get("agent_id"),
                    "queue": labels.get("queue"), "ani_masked": labels.get("ani"), "language": doc.get("languageCode"),
                    "turns": turns, "meta": {"medium": doc.get("medium"), "labels": labels,
                                             "audio_uri": ((doc.get("dataSource") or {}).get("gcsSource") or {}).get("audioUri"),
                                             "insights_silence": analysis.get("silence"),
                                             "insights_sentiments": analysis.get("sentiments")}})


HEADER_RE = re.compile(r"^([A-Za-z][A-Za-z ]{1,30}):\s*(.+)$")
LINE_RE = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)\]\s*([A-Za-z ]{2,20}):\s*(.*)$")


def parse_text(data: bytes, name: str) -> dict:
    text = data.decode("utf-8-sig", errors="replace")
    head: dict[str, str] = {}
    turns: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or set(line) <= {"-", "="}:
            continue
        m = LINE_RE.match(line)
        if m:
            turns.append({"role": _role(m.group(2).strip()), "text": m.group(3), "start": _secs(m.group(1)), "end": None,
                          "sentiment": None})
            continue
        m = HEADER_RE.match(line)
        if m and not turns:
            head[m.group(1).strip().lower()] = m.group(2).strip()
        elif turns:
            turns[-1]["text"] += " " + line  # wrapped line
    if not turns:  # "Agent: …" without timestamps
        for line in text.splitlines():
            m = re.match(r"^(Agent|Customer|Caller|Rep)\s*:\s*(.*)$", line.strip(), re.I)
            if m:
                turns.append({"role": _role(m.group(1)), "text": m.group(2), "start": None, "end": None, "sentiment": None})
    dur = _secs(head.get("duration"))
    for i, t in enumerate(turns):  # end = next start (minus a beat)
        nxt = turns[i + 1]["start"] if i + 1 < len(turns) else dur
        if t["start"] is not None and nxt is not None:
            t["end"] = max(t["start"], nxt - 0.5)
    return _finish({"call_id": head.get("conversation id") or head.get("call id") or PurePosixPath(name).stem.replace("transcript_", ""),
                    "format": "text", "started_at": _iso(head.get("start time") or head.get("date")), "duration_sec": dur,
                    "agent_id": head.get("agent id") or head.get("agent"), "queue": head.get("queue"),
                    "ani_masked": head.get("caller ani") or head.get("ani"), "turns": turns, "meta": {"header": head}})


CUE_RE = re.compile(r"(\d{1,2}:)?(\d{2}):(\d{2})\.(\d{3})\s+-->\s+(\d{1,2}:)?(\d{2}):(\d{2})\.(\d{3})")


def _vtt_ts(h, m, s, ms) -> float:
    return int((h or "0:")[:-1]) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_vtt(data: bytes, name: str) -> dict:
    text = data.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    if not text.lstrip().startswith("WEBVTT"):
        raise ParseError("missing WEBVTT header")
    meta: dict[str, str] = {}
    turns: list[dict] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [x for x in block.strip().split("\n") if x.strip()]
        if not lines:
            continue
        if lines[0].startswith("NOTE"):
            for k, v in re.findall(r"(\w+)=(\S+)", " ".join(lines)):
                meta[k] = v
            continue
        idx = next((i for i, x in enumerate(lines) if CUE_RE.search(x)), None)
        if idx is None:
            continue
        m = CUE_RE.search(lines[idx])
        start, end = _vtt_ts(*m.group(1, 2, 3, 4)), _vtt_ts(*m.group(5, 6, 7, 8))
        payload = " ".join(lines[idx + 1:])
        sm = re.match(r"^<v\s+([^>]+)>(.*)$", payload)
        speaker, body = (sm.group(1), sm.group(2)) if sm else ("", payload)
        if not sm:
            sm2 = re.match(r"^(Agent|Customer|Caller)\s*:\s*(.*)$", payload, re.I)
            if sm2:
                speaker, body = sm2.group(1), sm2.group(2)
        body = re.sub(r"</?[^>]+>", "", body)
        turns.append({"role": _role(speaker.strip()), "text": body, "start": start, "end": end, "sentiment": None})
    return _finish({"call_id": meta.get("conversation_id") or PurePosixPath(name).stem, "format": "vtt",
                    "started_at": _iso(meta.get("start")), "duration_sec": _secs(meta.get("duration")),
                    "agent_id": meta.get("agent_id"), "queue": meta.get("queue"), "ani_masked": meta.get("ani"),
                    "turns": turns, "meta": {"note": meta}})


def parse_manifest(data: bytes, name: str) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace"))))
    if rows and "conversation_id" not in rows[0]:
        raise ParseError("manifest has no conversation_id column")
    return rows


def parse(name: str, data: bytes) -> tuple[str, dict | list]:
    """→ (kind, record) where kind is 'call' (record = normalised call) or 'manifest' (record = rows)."""
    fmt = detect(name, data)
    if fmt == "manifest":
        return "manifest", parse_manifest(data, name)
    if fmt == "insights_json":
        return "call", parse_insights_json(data, name)
    if fmt == "vtt":
        return "call", parse_vtt(data, name)
    if fmt == "text":
        return "call", parse_text(data, name)
    raise ParseError(f"unsupported file type: {name}")
