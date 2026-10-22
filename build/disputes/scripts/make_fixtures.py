"""Generate the dispute-inbox fixtures (~40 .eml) + ground truth, reproducibly.

    python build/disputes/scripts/make_fixtures.py [--customers 20000] [--seed 7]

Every dispute is a REAL row of raw.disputes (BAN, invoice `<ban>-<yymm>`,
category, disputed amount) from `bsp_seed.generate.build(customers, seed)` —
the same dataset the warehouse seed loads — so warehouse validation in the
disputes service (raw.customers / invoices / transactions / disputes) works.
Mail wording follows the account's technology (copper DSL vs fiber ONT).

Writes
  build/connectors/fixtures/m365/mailboxes/billing-disputes@example.net/inbox/*.eml
  build/disputes/tests/ground_truth.json

bsp_seed is fully deterministic for a given (customers, seed), so the
invoices referenced here exist in a warehouse seeded with the same values.
"""

from __future__ import annotations

import argparse
import html as htmlmod
import io
import json
import os
import random
import sys
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "build" / "seed"))
MAILBOX = "billing-disputes@example.net"
OUT = ROOT / "build/connectors/fixtures/m365/mailboxes" / MAILBOX / "inbox"
GT = ROOT / "build/disputes/tests/ground_truth.json"

R = random.Random(20261018)

# ── text helpers ──────────────────────────────────────────────────────────
TYPOS = {"account": "acount", "charged": "chargd", "because": "becuase", "received": "recieved",
         "separate": "seperate", "definitely": "definately", "please": "plese", "their": "thier",
         "bill": "bil", "payment": "paymnet"}


def typo(text: str, rate: float) -> str:
    out = []
    for w in text.split(" "):
        k = w.lower().strip(".,!?")
        if k in TYPOS and R.random() < rate and not any(c.isdigit() for c in w):
            w = w.replace(k, TYPOS[k])
        out.append(w)
    return " ".join(out)


def money(x: float) -> str:
    return "" if x is None else f"${x:,.2f}"


def acct_mention(ban: str, style: int) -> str:
    return [f"account {ban}", f"Account #: {ban}", f"BAN {ban}", f"account number {ban}",
            f"my account ({ban})", f"billing account {ban[:3]} {ban[3:6]} {ban[6:]}"][style % 6]


def sig(name: str, phone: str, email: str, style: int) -> str:
    first = name.split()[0]
    return [f"\n\nThanks,\n{name}\n{phone}",
            f"\n\n-- \n{name}\nPhone: {phone}\n{email}",
            f"\n\nRegards,\n{first}",
            f"\n\nSent from my iPhone",
            f"\n\n{name}\nCell {phone}\n\nThis email and any attachments are private. If you received it in error please delete it.",
            f"\n\nThank you for your time,\n{name}"][style % 6]


def month_name(bc: str) -> str:
    y, m = bc.split("-")
    return datetime(int(y), int(m), 1).strftime("%B %Y")


# ── attachments ───────────────────────────────────────────────────────────
def bill_pdf(cust: dict, inv: dict, lines: list[tuple[str, float]]) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    w, h = letter
    c.setFont("Helvetica-Bold", 16)
    c.drawString(54, h - 64, "DataPlatform  |  Monthly Statement")
    c.setFont("Helvetica", 10)
    y = h - 96
    for k, v in [("Customer", cust["customer_name"]), ("Billing account (BAN)", cust["ban"]),
                 ("Invoice number", inv["invoice_id"]), ("Billing period", month_name(inv["bill_cycle"])),
                 ("Statement date", str(inv["invoice_date"])), ("Payment due", str(inv["due_date"]))]:
        c.drawString(54, y, f"{k}:")
        c.drawString(190, y, str(v))
        y -= 16
    y -= 12
    c.setFont("Helvetica-Bold", 11)
    c.drawString(54, y, "Charges this period")
    y -= 18
    c.setFont("Helvetica", 10)
    for label, amt in lines:
        c.drawString(64, y, label)
        c.drawRightString(w - 72, y, money(amt))
        y -= 15
    c.line(54, y + 4, w - 54, y + 4)
    y -= 12
    c.setFont("Helvetica-Bold", 11)
    c.drawString(64, y, "Total amount due")
    c.drawRightString(w - 72, y, money(inv["amount_due"]))
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(54, 54, "Excerpt provided by customer. Synthetic demo data.")
    c.showPage()
    c.save()
    return buf.getvalue()


def bill_png(cust: dict, inv: dict, lines: list[tuple[str, float]]) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 560), (250, 250, 250))
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("DejaVuSans.ttf", 20)
        fb = ImageFont.truetype("DejaVuSans-Bold.ttf", 26)
    except OSError:
        f = fb = ImageFont.load_default()
    d.rectangle([0, 0, 900, 64], fill=(11, 110, 121))
    d.text((24, 16), "My Account  >  Billing  >  Statement", fill="white", font=f)
    d.text((24, 84), f"BAN {cust['ban']}   Invoice {inv['invoice_id']}", fill=(20, 20, 20), font=fb)
    d.text((24, 128), f"Billing period: {month_name(inv['bill_cycle'])}   Due {inv['due_date']}", fill=(60, 60, 60), font=f)
    y = 180
    for label, amt in lines:
        d.text((40, y), label, fill=(30, 30, 30), font=f)
        d.text((700, y), money(amt), fill=(30, 30, 30), font=f)
        y += 36
    d.line([24, y + 6, 876, y + 6], fill=(120, 120, 120), width=2)
    d.text((40, y + 20), "Amount due", fill=(0, 0, 0), font=fb)
    d.text((700, y + 20), money(inv["amount_due"]), fill=(0, 0, 0), font=fb)
    d.ellipse([660, 170, 860, 230], outline=(220, 30, 30), width=4)  # the customer's red circle
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def bill_lines(inv: dict) -> list[tuple[str, float]]:
    out = [("Internet service (recurring)", inv["recurring_charges"])]
    if inv["one_time_charges"]:
        out.append(("One-time charges", inv["one_time_charges"]))
    if inv["late_fee"]:
        out.append(("Late payment fee", inv["late_fee"]))
    if inv["credits"]:
        out.append(("Credits", -inv["credits"]))
    out.append(("Taxes & fees", inv["taxes_fees"]))
    return out


# ── dataset selection ─────────────────────────────────────────────────────
def load(customers: int, seed: int):
    from bsp_seed.generate import build

    ds = build(customers=customers, seed=seed)
    cust = ds["customers"].set_index("ban")
    inv = ds["invoices"].set_index("invoice_id", drop=False)
    subs = ds["subscriptions"].set_index("ban")
    loc = ds["service_locations"].set_index("ban")
    plans = ds["plans"].set_index("plan_id")
    return ds, cust, inv, subs, loc, plans


TECH_WORDS = {
    "fiber": {"svc": "fiber internet", "box": "ONT", "gear": "fiber gateway", "light": "the red LOS light on the ONT",
              "down": "the fiber went down"},
    "copper": {"svc": "DSL internet", "box": "DSL modem", "gear": "DSL modem", "light": "the DSL light blinking on the modem",
               "down": "the DSL line went dead"},
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--customers", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    ds, cust, inv, subs, loc, plans = load(a.customers, a.seed)
    inv = inv.copy()
    inv["invoice_date"] = inv["invoice_date"].astype(str)
    inv["due_date"] = inv["due_date"].astype(str)
    disputes = ds["disputes"].copy()
    disputes["opened_date"] = disputes["opened_date"].astype(str)
    recent = disputes[(disputes.opened_date >= "2026-06-01") & disputes.invoice_id.isin(inv.index)]
    used: set[str] = set()

    def cdict(ban: str) -> dict:
        r, lr = cust.loc[ban], loc.loc[ban]
        plan = plans.loc[subs.loc[ban].plan_id]
        return {"ban": ban, "customer_name": r.customer_name, "email": r.email, "phone": r.phone,
                "city": r.city, "state": r.state, "zip": r.zip, "autopay": bool(r.autopay), "clli": r.clli,
                "technology": lr.technology, "plan_id": plan.name, "plan_name": plan.plan_name,
                "down_mbps": int(plan.down_mbps), "list_price": float(plan.monthly_price),
                "promo_price": float(plan.promo_price), "w": TECH_WORDS[lr.technology]}

    def pick(category: str, *, residential_only: bool = False) -> tuple[dict, dict, float]:
        """A real dispute from raw.disputes (prefer e-mail channel) → (customer, invoice, disputed amount)."""
        df = recent[(recent.category == category) & ~recent.ban.isin(used)]
        if category == "final_bill":  # the mails say "I cancelled": prefer accounts that did cancel
            gone = df[subs.reindex(df.ban).status.to_numpy() == "cancelled"]
            df = gone if len(gone) >= 6 else df
        seg = cust.reindex(df.ban).segment.to_numpy()
        if residential_only or R.random() < 0.85:
            df = df[seg == "residential"]
        em = df[df.channel == "email"]
        df = em if len(em) and R.random() < 0.8 else df
        row = df.iloc[R.randrange(len(df))]
        used.add(row.ban)
        return cdict(row.ban), inv.loc[row.invoice_id].to_dict(), round(float(row.disputed_amount), 2)

    mails: list[dict] = []
    base_time = datetime(2026, 10, 12, 7, 30, tzinfo=timezone.utc)

    def add(category, c, inv_row, subject, body, amount, resolution, *, attach=None, forward=None,
            acct_in_text=True, inv_in_text=True, urgency="normal", from_override=None, period=None):
        mails.append(dict(category=category, cust=c, inv=inv_row, subject=subject, body=body, amount=amount,
                          resolution=resolution, attach=attach, forward=forward, acct_in_text=acct_in_text,
                          inv_in_text=inv_in_text, urgency=urgency, from_override=from_override,
                          period=period or (inv_row["bill_cycle"] if inv_row else None)))

    # 1) late fee despite autopay ×5
    for k in range(5):
        c, i, amt = pick("late_fee_autopay")
        intro = ["I have autopay set up and I was STILL charged a late fee.",
                 "Hello, I noticed a late payment fee on my latest bill even though I'm enrolled in autopay.",
                 f"why is there a {money(amt)} late fee on my bill?? autopay has been on for 2 years",
                 "Good morning. My statement shows a late fee that I do not believe I owe.",
                 "Autopay pulled my payment on time like every month, yet the bill says late fee."][k]
        body = (f"{intro}\n\nThis is for {acct_mention(c['ban'], k)}, invoice {i['invoice_id']} "
                f"({month_name(i['bill_cycle'])}). The late fee is {money(amt)}. "
                f"The payment came out of my checking automatically like it always does, so this is not my fault. "
                f"Please remove the {money(amt)} fee and credit my account."
                + (" If this keeps happening I will cancel." if k == 2 else ""))
        add("late_fee_autopay", c, i, ["Late fee charged - I have autopay", "Incorrect late fee",
                                         "late fee??", "Dispute: late payment fee on autopay account",
                                         "Autopay customer charged late fee"][k],
            body, amt, "credit", urgency="high" if k == 2 else "normal", attach="pdf" if k == 1 else None)

    # 2) promotion expired ×4
    for k in range(4):
        c, i, amt = pick("promo_expired")
        old = c["promo_price"] if c["promo_price"] < c["list_price"] else round(c["list_price"] - amt, 2)
        new = round(old + amt, 2)
        texts = [
            f"Hi there,\n\nI just got my {month_name(i['bill_cycle'])} statement ({i['invoice_id']}) and my promotional discount "
            f"is gone - the bill went from {money(old)} to {money(new)}. Nobody told me the promotion was ending. Can you please "
            f"apply the promo rate again? My account is {c['ban']}.",
            f"When I signed up for {c['plan_name']} the rep said the promo price was good for a year. My promo ended early "
            f"and I am now paying {money(amt)} more. {acct_mention(c['ban'], k)}, invoice {i['invoice_id']}. "
            f"I want the {money(amt)} difference credited and the promotional rate restored.",
            f"promo expired and nobody warned me. was {money(old)} now {money(new)}. account {c['ban']}. "
            f"put the discount back or i'm switching to the other provider",
            f"Hello, the attached screenshot shows my latest bill. The monthly charge increased by {money(amt)} after my promo "
            f"expired. I'd like to be put back on the promotional price. Thanks.",
        ][k]
        add("promo_expired", c, i, ["Promo discount disappeared", f"Promotion ended early - {c['plan_id']}",
                                      "PROMO EXPIRED", "Bill went up after promo"][k],
            texts, amt, ["credit", "credit", "credit", "credit"][k],
            attach="png" if k == 3 else None, acct_in_text=k != 3, inv_in_text=k in (0, 1),
            urgency="high" if k == 2 else "normal")

    # 3) price increase ×5
    for k in range(5):
        c, i, amt = pick("price_increase")
        old = c["list_price"]
        new = round(old + amt, 2)
        texts = [
            f"My internet bill jumped from {money(old)} to {money(new)} this month with no warning. When I signed up for "
            f"{c['plan_name']} I was told the price was locked. {acct_mention(c['ban'], k)}. Invoice {i['invoice_id']}. "
            f"I want the {money(amt)} difference refunded and the original price restored.",
            f"To whom it may concern,\n\nI am writing to formally dispute the price increase on {acct_mention(c['ban'], k)}. "
            f"My plan ({c['plan_name']}) was {money(old)} per month and is now billed at {money(new)} on invoice {i['invoice_id']}. "
            f"I request a credit of {money(amt)} and an explanation in writing.",
            f"price went up AGAIN. was {money(old)} now {money(new)}. BAN {c['ban']}. "
            + ("still on slow DSL and you raise the price?? fix it or i'm switching, the other provider has fiber on my street now"
               if c["technology"] == "copper" else "fix it or i'm switching to the other provider"),
            f"Hello,\n\nYour rate increase notice must have gone to spam - {c['plan_name']} went up by {money(amt)} on my "
            f"{month_name(i['bill_cycle'])} bill. Account number {c['ban']}. Please credit the increase for this cycle.",
            f"Hi, I see a price increase of {money(amt)} on statement {i['invoice_id']}. I've been a customer for years and "
            f"never had a rate hike without notice. Could you credit {money(amt)} back? {acct_mention(c['ban'], 4)}",
        ][k]
        add("price_increase", c, i, ["Unexpected price increase", "Formal dispute - price increase", "PRICE INCREASE",
                                       "Rate increase on my bill", f"Price increase {i['invoice_id']}"][k],
            texts, amt, ["refund", "credit", "credit", "credit", "credit"][k],
            inv_in_text=k in (0, 1, 4), urgency="high" if k == 2 else "normal")

    # 4) duplicate charge ×4
    for k in range(4):
        c, i, amt = pick("duplicate_charge")
        texts = [
            f"I was charged twice for the same bill. Two withdrawals of {money(amt)} hit my bank on the same day for invoice "
            f"{i['invoice_id']}. {acct_mention(c['ban'], k)}. Please refund one of them ASAP, this overdrew my account.",
            f"Hi, it looks like my card was billed two times for {month_name(i['bill_cycle'])}. Each charge was {money(amt)}. "
            f"Account {c['ban']}, invoice {i['invoice_id']}. Please reverse the duplicate.",
            f"Duplicate payment taken. {money(amt)} x2. acct {c['ban']}. Refund the second one please.",
            f"Hello Billing Team,\n\nOur account {c['ban']} was debited twice for invoice {i['invoice_id']} "
            f"({money(amt)} each). Kindly refund the duplicate charge of {money(amt)} to the card on file.",
        ][k]
        add("duplicate_charge", c, i, ["Charged twice!!", "Duplicate charge", "double charge", "Duplicate debit on our account"][k],
            texts, amt, "refund", urgency="high" if k == 0 else "normal", inv_in_text=k != 2, attach="pdf" if k == 3 else None)

    # 5) outage / degraded-service credit ×5
    for k in range(5):
        c, i, amt = pick("outage_credit")
        w = c["w"]
        days = [3, 5, 2, 9, 4][k]
        stated = [None, amt, None, amt, amt][k]
        texts = [
            f"Our {w['svc']} was down for {days} days last month ({month_name(i['bill_cycle'])}). {w['light'].capitalize()} the whole "
            f"time. I work from home and lost income. I'd like a service credit for the outage. Account number {c['ban']}.",
            f"Hello,\nService was out from the 3rd to the 8th. That's {days} days without internet - {w['down']} and the tech "
            f"said it was a {'fiber cut' if c['technology'] == 'fiber' else 'bad pair in the cable'}. I'm requesting a "
            f"{money(stated)} credit on my next bill. Account: {c['ban']}\nInvoice in question: {i['invoice_id']}",
            f"no service for {days} days. i expect a credit. {acct_mention(c['ban'], 2)}",
            f"Good afternoon,\n\nThe outage in {c['city']} (wire center {c['clli']}) left us without service for {days} days. "
            f"Your own status page confirmed it. Please apply a credit of {money(stated)} to {acct_mention(c['ban'], 3)} "
            f"for invoice {i['invoice_id']}.",
            (f"I pay for {c['plan_name']} ({c['down_mbps']} Mbps) but for {days} days the DSL kept dropping and speed tests showed "
             f"under 3 Mbps. Two retrains an hour according to your own tech. Please credit {money(stated)} on "
             f"{i['invoice_id']}. Account {c['ban']}." if c["technology"] == "copper" else
             f"My fiber connection kept dropping for {days} days - the ONT showed a red LOS light on and off. Please credit "
             f"{money(stated)} on {i['invoice_id']}. Account {c['ban']}."),
        ][k]
        add("outage_credit", c, i, ["Outage credit request", "Credit for service outage", "outage",
                                      "Service credit - multi-day outage", "Credit for days without working internet"][k],
            texts, stated, "credit", inv_in_text=k in (1, 3, 4), period=i["bill_cycle"])

    # 6) equipment non-return ×4
    for k in range(4):
        c, i, amt = pick("equipment_fee")
        w = c["w"]
        texts = [
            f"I returned my {w['gear']} at the UPS store on {['Aug 2', 'July 14', 'Sept 1', 'June 30'][k]} and I have the receipt "
            f"with the tracking number. Now I'm being charged a {money(amt)} unreturned equipment fee. "
            f"{acct_mention(c['ban'], k)}. Please remove it.",
            f"Hi - got a bill for {money(amt)} for a {w['gear']} I sent back weeks ago. Invoice {i['invoice_id']}, account {c['ban']}. "
            f"Tracking number 1Z{R.randrange(10**9, 10**10)} shows it was delivered.",
            f"You charged me {money(amt)} for equipment. I dropped the {w['box']} off at your store in {c['city']}!! acct {c['ban']}",
            f"Dear DataPlatform,\n\nPlease waive the non-return equipment charge of {money(amt)} on invoice {i['invoice_id']}. "
            f"The {w['gear']} was returned in the prepaid box you sent. Account {c['ban']}.",
        ][k]
        add("equipment_fee", c, i, ["Unreturned equipment fee - I returned it", "Equipment fee", "equipment charge wrong",
                                      "Non-return fee dispute"][k],
            texts, amt, "waive", inv_in_text=k in (1, 3))

    # 7) final bill after cancellation ×4
    for k in range(4):
        c, i, amt = pick("final_bill")
        texts = [
            f"I cancelled my service and returned everything, but I just received a final bill of {money(amt)} (invoice {i['invoice_id']}). "
            f"I was told my last bill would be prorated to zero. Account {c['ban']}. Please void this.",
            f"Why am I still getting billed?? I cancelled in {month_name(i['bill_cycle'])}. {money(amt)} on {i['invoice_id']}. "
            f"I will not pay this. {acct_mention(c['ban'], 4)}",
            f"Hello,\nI moved out of state and cancelled. Your final statement for {acct_mention(c['ban'], 5)} shows {money(amt)} due. "
            f"I'd like an itemized explanation and the balance removed because I cancelled before the cycle started.",
            f"Hi, I switched providers and cancelled my {c['w']['svc']} last month, but the final bill charges {money(amt)} for a "
            f"full month. Account {c['ban']}. Please remove the charge.",
        ][k]
        add("final_bill", c, i, ["Final bill after cancellation", "STILL BILLED AFTER CANCELLING", "Final statement question",
                                   "Charged after I cancelled"][k],
            texts, amt, "waive", inv_in_text=k in (0, 1), urgency="high" if k == 1 else "normal")

    # 8) installation fee ×4
    for k in range(4):
        c, i, amt = pick("install_fee")
        texts = [
            f"When I upgraded to {'fiber' if c['technology'] == 'fiber' else 'the faster DSL plan'} the rep promised free installation. "
            f"My bill has a {money(amt)} installation charge. Account {c['ban']}, invoice {i['invoice_id']}. Please credit the {money(amt)}.",
            f"Installation fee of {money(amt)}?? The technician never even showed up the first time and I had to reschedule. "
            f"{acct_mention(c['ban'], 1)}",
            f"Hello,\n\nI was charged a professional installation fee ({money(amt)}) but I did a self-install with the "
            f"{c['w']['gear']} kit you mailed. Account {c['ban']}, statement {i['invoice_id']}. I'd appreciate a refund.\n\nThank you",
            f"The {'fiber install crew ran the drop and set up the ONT' if c['technology'] == 'fiber' else 'tech only swapped the DSL filter'} "
            f"in 30 minutes and now there's a {money(amt)} install fee on {i['invoice_id']}. That was supposed to be included. "
            f"BAN {c['ban']}. Please waive it.",
        ][k]
        add("install_fee", c, i, ["Installation fee should be free", "install fee", "Charged for installation I didn't get",
                                    "Install fee on my bill"][k],
            texts, amt, ["credit", "credit", "refund", "waive"][k], inv_in_text=k != 1, attach="pdf" if k == 2 else None)

    # forwarding chains: rewrite three mails as forwarded by a store / care rep
    for idx, who in [(5, ("Dana Whitfield", "dana.whitfield@care.example.net")),
                     (17, ("Retail Store 0417", "store0417@retail.example.net")),
                     (30, ("Marcus Lee", "marcus.lee@care.example.net"))]:
        mails[idx]["forward"] = who

    # ── noise ────────────────────────────────────────────────────────────
    noise = [
        ("Automatic reply: Your DataPlatform bill is ready", "I am out of the office until Monday, October 20 with limited access to email. "
         "For urgent matters please contact my assistant.\n\nBest,\nPat", ("Pat Okafor", "pat.okafor@example.org"), {"Auto-Submitted": "auto-replied"}),
        ("Thank you!", "Just wanted to say thanks to the rep who helped me yesterday, the credit showed up already. Great service!",
         ("Avery Hughes", "avery.h@example.com"), {}),
        ("Fiber Insider: October newsletter", "This month: new Wi-Fi 7 gateways, tips to secure your home network, and our community "
         "giving program.\n\nYou are receiving this because you subscribed. Unsubscribe | Manage preferences",
         ("Fiber Insider", "news@marketing.example.net"), {"List-Unsubscribe": "<mailto:unsubscribe@marketing.example.net>"}),
        ("Out of Office Re: case update", "Thank you for your message. I'm currently on vacation and will respond when I return on 10/27.",
         ("Jordan Price", "jordan.price@example.org"), {"Auto-Submitted": "auto-replied", "X-Auto-Response-Suppress": "All"}),
        ("RE: your service appointment", "Thanks, Tuesday between 8 and 12 works for us. See you then.",
         ("Riley Foster", "riley.foster@example.com"), {}),
        ("Webinar: Modernizing billing operations", "Join industry leaders for a 45 minute session on revenue assurance. "
         "Register now - seats are limited. To stop receiving these emails click here.",
         ("Events Team", "events@vendor.example.com"), {"List-Unsubscribe": "<https://vendor.example.com/unsub>"}),
    ]

    OUT.mkdir(parents=True, exist_ok=True)
    for p in OUT.glob("*.eml"):
        p.unlink()
    order = list(range(len(mails))) + [-(j + 1) for j in range(len(noise))]
    R.shuffle(order)
    truth = []
    for n, oi in enumerate(order, start=1):
        ts = base_time + timedelta(minutes=37 * n + R.randrange(0, 30))
        msg = EmailMessage()
        msg["To"] = f"Billing Disputes <{MAILBOX}>"
        msg["Date"] = format_datetime(ts)
        msg["Message-ID"] = make_msgid(idstring=f"disp{n:03d}", domain="mail.example.net")
        fname = f"{n:03d}.eml"
        if oi < 0:
            subj, body, (nm, em), hdrs = noise[-oi - 1]
            msg["From"] = f"{nm} <{em}>"
            msg["Subject"] = subj
            for k, v in hdrs.items():
                msg[k] = v
            msg.set_content(body)
            truth.append({"file": fname, "message_id": msg["Message-ID"], "subject": subj, "is_dispute": False})
        else:
            m = mails[oi]
            c, i = m["cust"], m["inv"]
            style = R.randrange(6)
            body = typo(m["body"], 0.25 if style in (2, 3) else 0.05) + sig(c["customer_name"], c["phone"], c["email"], style)
            from_name, from_email = c["customer_name"], c["email"]
            if R.random() < 0.15:  # personal address not on file (warehouse email lookup won't find it)
                from_email = c["customer_name"].lower().replace(" ", ".") + f"{R.randrange(10, 99)}@mail.example.com"
            if m["forward"]:
                fw_name, fw_email = m["forward"]
                body = (f"Hi team, customer came in about this, please review.\n\n{fw_name}\n\n"
                        f"---------- Forwarded message ---------\nFrom: {from_name} <{from_email}>\n"
                        f"Date: {format_datetime(ts - timedelta(days=1))}\nSubject: {m['subject']}\nTo: {fw_email}\n\n{body}")
                msg["From"] = f"{fw_name} <{fw_email}>"
                msg["Subject"] = "Fwd: " + m["subject"]
            else:
                msg["From"] = f"{from_name} <{from_email}>"
                msg["Subject"] = m["subject"]
            msg.set_content(body)
            html_body = "<html><body>" + "".join(f"<p>{htmlmod.escape(line)}</p>" for line in body.split("\n") if line) + "</body></html>"
            msg.add_alternative(html_body, subtype="html")
            if m["attach"]:
                lines = bill_lines(i)
                if m["attach"] == "pdf":
                    msg.add_attachment(bill_pdf(c, i, lines), maintype="application", subtype="pdf",
                                       filename=f"statement_{i['bill_cycle']}.pdf")
                else:
                    msg.add_attachment(bill_png(c, i, lines), maintype="image", subtype="png",
                                       filename="bill_screenshot.png")
            truth.append({
                "file": fname, "message_id": msg["Message-ID"], "subject": msg["Subject"], "is_dispute": True,
                "category": m["category"], "ban": c["ban"], "technology": c["technology"], "invoice_id": i["invoice_id"] if i else None,
                "invoice_in_text": m["inv_in_text"], "ban_in_text": m["acct_in_text"],
                "disputed_amount": m["amount"], "currency": "USD", "customer_name": c["customer_name"],
                "contact_email": from_email, "contact_phone": c["phone"], "requested_resolution": m["resolution"],
                "billing_period": m["period"], "urgency": m["urgency"], "attachment": m["attach"],
                "forwarded": bool(m["forward"]),
            })
        (OUT / fname).write_bytes(bytes(msg))
    GT.parent.mkdir(parents=True, exist_ok=True)
    GT.write_text(json.dumps({"customers": a.customers, "seed": a.seed, "mailbox": MAILBOX,
                              "messages": truth}, indent=2, default=str))
    print(f"wrote {len(truth)} messages ({sum(t['is_dispute'] for t in truth)} disputes) to {OUT}")


if __name__ == "__main__":
    main()
