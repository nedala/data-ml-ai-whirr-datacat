"""Deterministic synthetic ISP dataset for the DataPlatform appliances.

DataPlatform sells two internet technologies — **copper (DSL)** and **fiber
(FTTH)** — and every table is keyed by the **BAN** (billing account number,
10 digits). Every appliance demos against the SAME accounts: the warehouse
seed, the churn model, the dispute e-mails and the CCAI call transcripts all
draw from `build(seed=…)`, so a BAN seen in a dispute exists in billing, in the
churn features and in the call logs.

Tables (schema `raw` in the warehouse; parquet under lake/samples/bsp/):
  wire_centers        clli · central offices with fiber passings / copper lines
  customers           ban · who, where, how they pay
  service_locations   ban · technology (copper | fiber), CLLI, plant, competitor overlap
  plans, subscriptions
  usage_monthly       ban × month · traffic, latency, outage minutes
  ont_metrics         ban × month · fiber ONT optics: rx power, LOS, BIP errors, reboots
  copper_line_metrics ban × month · DSL line: SNR margin, attenuation, sync rate, retrains
  network_events      outages / degradations by CLLI
  service_tickets     ban · trouble and care tickets
  customer_calls      ban · contact-center calls (queue, reason, disposition, sentiment)
  dispatches          ban · field dispatches / truck rolls (repair, install, upgrade)
  invoices            ban × bill cycle · recurring / one-time / taxes / credits / late fees
  transactions        ban · ledger: payments, late fees, credits, refunds, adjustments
  disputes            ban · billing disputes and their outcome
  revenue             ban × month · recurring, one-time, credits, net revenue
  nps_surveys         ban · relationship NPS and verbatims
  customer_monthly    ban × month · feature-ready snapshot with `churned_next_90d`

The data is synthetic. Churn carries real signal (copper vs fiber, line and
optical quality, outages, repeat dispatches and calls, disputes, late
payments, price increases, NPS, tenure, competitor fiber) and the last months
carry a deliberate distribution shift (a price-increase wave + an outage
cluster) so drift detection has something to find.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

KEY = "ban"

STATES = {
    # state: (weight, [(city, 4-letter CLLI place code), …])
    "NC": (0.20, [("Raleigh", "RLGH"), ("Wilson", "WLSN"), ("Rocky Mount", "RCMT"), ("Hickory", "HCKR"), ("Goldsboro", "GLBR")]),
    "OH": (0.16, [("Mansfield", "MNFD"), ("Lima", "LIMA"), ("Elyria", "ELYR"), ("Marion", "MARN"), ("Sandusky", "SNDS")]),
    "VA": (0.12, [("Charlottesville", "CHVL"), ("Bristol", "BRTL"), ("Winchester", "WNCH"), ("Danville", "DNVL")]),
    "PA": (0.09, [("Erie", "ERIE"), ("Altoona", "ALTN"), ("Johnstown", "JHTW"), ("Williamsport", "WLPT")]),
    "MO": (0.09, [("Joplin", "JPLN"), ("Jefferson City", "JFCY"), ("Sedalia", "SDLA"), ("Rolla", "ROLA")]),
    "TX": (0.08, [("Tyler", "TYLR"), ("Lufkin", "LFKN"), ("Nacogdoches", "NCGD"), ("Kilgore", "KLGR")]),
    "FL": (0.07, [("Fort Myers", "FTMY"), ("Ocala", "OCAL"), ("Leesburg", "LSBG"), ("Naples", "NPLS")]),
    "AL": (0.06, [("Dothan", "DTHN"), ("Enterprise", "ENTP"), ("Ozark", "OZRK")]),
    "SC": (0.05, [("Rock Hill", "RKHL"), ("Gaffney", "GFNY"), ("Lancaster", "LNCS")]),
    "TN": (0.04, [("Johnson City", "JHCY"), ("Kingsport", "KGPT")]),
    "IN": (0.04, [("Bloomington", "BLTN"), ("Kokomo", "KKMO"), ("Muncie", "MNCE")]),
}

PLANS = pd.DataFrame([
    # plan_id, name, technology, down, up, list price, promo price, promo months
    ("CU-25", "Internet 25 (copper)", "copper", 25, 2, 55.0, 55.0, 0),
    ("CU-80", "Internet 80 (copper)", "copper", 80, 10, 60.0, 50.0, 6),
    ("CU-140", "Internet 140 (copper)", "copper", 140, 20, 65.0, 55.0, 6),
    ("FB-300", "Fiber 300", "fiber", 300, 300, 59.0, 49.0, 12),
    ("FB-500", "Fiber 500", "fiber", 500, 500, 69.0, 59.0, 12),
    ("FB-1G", "Fiber Gig", "fiber", 940, 940, 79.0, 65.0, 12),
    ("FB-2G", "Fiber 2 Gig", "fiber", 2000, 2000, 109.0, 99.0, 6),
], columns=["plan_id", "plan_name", "technology", "down_mbps", "up_mbps",
            "monthly_price", "promo_price", "promo_months"])

TICKET_CATEGORIES = {
    "billing": ["incorrect_charge", "promo_expired", "late_fee", "autopay_failure", "refund_request"],
    "technical": ["no_internet", "slow_speed", "intermittent", "wifi_coverage", "equipment_fault"],
    "installation": ["missed_appointment", "install_delay", "drop_line", "ont_placement"],
    "account": ["move_service", "plan_change", "cancel_request", "update_payment"],
}

CALL_QUEUES = {   # queue: (weight, reasons)
    "billing": (0.34, ["bill_explanation", "late_fee", "promo_expired", "payment_arrangement", "dispute_charge"]),
    "technical": (0.36, ["no_internet", "slow_speed", "intermittent", "wifi_help", "equipment"]),
    "retention": (0.12, ["cancel_request", "price_negotiation", "competitor_offer"]),
    "sales": (0.10, ["fiber_availability", "plan_upgrade", "new_service"]),
    "move": (0.08, ["move_service", "transfer_service"]),
}

DISPATCH_CAUSES = {
    "fiber": ["drop_fiber_damage", "ont_fault", "connector_dirty", "inside_wiring", "no_trouble_found"],
    "copper": ["dslam_port", "bridge_tap", "drop_wire_corrosion", "inside_wiring", "no_trouble_found"],
}

DISPUTE_CATEGORIES = ["late_fee_autopay", "promo_expired", "price_increase", "duplicate_charge",
                      "outage_credit", "equipment_fee", "final_bill", "install_fee"]

VERBATIM = {
    "promoter": ["Fiber has been rock solid, speeds are exactly as advertised.",
                 "Install tech was great, whole process was easy.",
                 "Gig speeds for the whole family, no complaints.",
                 "Support fixed my issue on the first call."],
    "passive": ["Service is fine but the price keeps creeping up.",
                "Speeds are ok in the evening, wifi upstairs is weak.",
                "Billing is confusing but the internet works.",
                "It works, nothing special."],
    "detractor": ["Outages every week and nobody tells us why.",
                  "My bill went up again after the promo ended, considering switching.",
                  "Waited three weeks for a technician.",
                  "DSL is too slow, when is fiber coming to my street?",
                  "Charged a late fee even though autopay was on."],
}


@dataclass
class Dataset:
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)

    def __getitem__(self, name: str) -> pd.DataFrame:
        return self.tables[name]

    def summary(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.tables.items()}


def ban_for(index: int) -> str:
    """BAN of the index-th synthetic account: 10 digits, stable across runs."""
    return f"{3100000000 + index * 7 + (index % 5)}"


def invoice_id(ban: str, month: pd.Timestamp) -> str:
    return f"{ban}-{month.strftime('%y%m')}"


def _wire_centers(rng: np.random.Generator) -> pd.DataFrame:
    rows = []
    for st, (_, cities) in STATES.items():
        for city, code in cities:
            for suffix in ("XA", "DS")[: 1 + (zlib.crc32(city.encode()) % 2)]:
                passings = int(rng.integers(4000, 30000))
                rows.append({"clli": f"{code}{st}{suffix}", "wire_center_name": f"{city} {suffix}", "state": st, "city": city,
                             "fiber_passings": passings, "copper_lines": int(rng.integers(3000, 22000)),
                             "olt_count": max(1, passings // 4000), "dslam_count": int(rng.integers(4, 30)),
                             "fiber_build_year": int(rng.integers(2019, 2026)),
                             "latitude": round(float(rng.uniform(26.0, 42.5)), 4),
                             "longitude": round(float(rng.uniform(-97.0, -76.0)), 4)})
    return pd.DataFrame(rows)


def build(customers: int = 20000, months: int = 24, seed: int = 7, start: str = "2024-10-01") -> Dataset:
    rng = np.random.default_rng(seed)
    month_idx = pd.date_range(start=start, periods=months, freq="MS")
    n = customers

    # ── wire centers, customers, locations ──────────────────────────────
    wc = _wire_centers(rng)
    st_names = list(STATES)
    st_w = np.array([STATES[s][0] for s in st_names]); st_w /= st_w.sum()
    state = rng.choice(st_names, size=n, p=st_w)
    wc_by_state = {s: wc[wc.state == s] for s in st_names}
    pick = np.array([rng.integers(0, len(wc_by_state[s])) for s in state])
    clli = np.array([wc_by_state[s].clli.iat[i] for s, i in zip(state, pick)])
    city = np.array([wc_by_state[s].city.iat[i] for s, i in zip(state, pick)])
    ban = np.array([ban_for(i) for i in range(n)])
    segment = rng.choice(["residential", "smb"], size=n, p=[0.88, 0.12])
    signup = pd.to_datetime(start) - pd.to_timedelta(rng.integers(30, 365 * 8, size=n), unit="D")
    fiber_available = rng.random(n) < np.where(np.isin(state, ["NC", "OH", "VA"]), 0.58, 0.40)
    technology = np.where(fiber_available & (rng.random(n) < 0.80), "fiber", "copper")
    competitor_fiber = rng.random(n) < np.where(technology == "fiber", 0.25, 0.42)
    first = ["Avery", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Quinn", "Jamie", "Drew", "Reese",
             "Parker", "Rowan", "Emerson", "Hayden", "Sawyer", "Logan", "Kendall", "Blake"]
    last = ["Carter", "Nguyen", "Patel", "Brooks", "Rivera", "Coleman", "Hughes", "Foster", "Ward",
            "Price", "Sanders", "Bennett", "Russell", "Griffin", "Hayes", "Myers", "Ford", "Barnes"]
    names = np.array([f"{rng.choice(first)} {rng.choice(last)}" for _ in range(n)])
    names = np.where(segment == "smb", np.char.add(np.array([x.split()[1] for x in names]), " Holdings LLC"), names)
    cust = pd.DataFrame({
        "ban": ban, "customer_name": names, "segment": segment, "state": state, "city": city, "clli": clli,
        "zip": [f"{rng.integers(10000, 99999)}" for _ in range(n)],
        "census_block": [f"{rng.integers(10**14, 10**15)}" for _ in range(n)],
        "latitude": np.round(rng.uniform(26.0, 42.5, n), 5), "longitude": np.round(rng.uniform(-97.0, -76.0, n), 5),
        "signup_date": signup.date,
        "credit_band": rng.choice(["A", "B", "C", "D"], size=n, p=[0.35, 0.35, 0.2, 0.1]),
        "autopay": rng.random(n) < 0.58, "paperless": rng.random(n) < 0.64,
        "email": [f"ban{b}@example.net" for b in ban],
        "phone": [f"+1-555-{rng.integers(100, 999)}-{rng.integers(1000, 9999)}" for _ in range(n)],
        "preferred_channel": rng.choice(["phone", "chat", "app", "store"], size=n, p=[0.45, 0.25, 0.25, 0.05]),
    })
    loc = pd.DataFrame({
        "location_id": [f"LOC{i:08d}" for i in range(n)], "ban": ban, "clli": clli,
        "technology": technology, "fiber_available": fiber_available, "competitor_fiber_available": competitor_fiber,
        "max_down_mbps": np.where(technology == "fiber", 2000, rng.choice([25, 80, 140], size=n)),
        "plant_build_year": np.where(technology == "fiber", rng.integers(2019, 2026, n), rng.integers(1975, 2005, n)),
        "ont_model": np.where(technology == "fiber", rng.choice(["XGS-PON 5G", "GPON G-240W", "XGS-PON 10G"], size=n), None),
        "loop_length_kft": np.where(technology == "copper", np.round(rng.gamma(4.0, 2.2, n).clip(1, 18), 1), np.nan),
    })

    plan_by_tech = {t: PLANS[PLANS.technology == t].plan_id.tolist() for t in ("copper", "fiber")}
    plan_id = np.array([rng.choice(plan_by_tech[t]) for t in technology])
    P = PLANS.set_index("plan_id")
    price = P.monthly_price.reindex(plan_id).to_numpy()
    promo_price = P.promo_price.reindex(plan_id).to_numpy()
    promo_months = P.promo_months.reindex(plan_id).to_numpy()
    is_fiber = technology == "fiber"
    loop_kft = loc.loop_length_kft.to_numpy()

    # ── latent risk + network events ────────────────────────────────────
    base_logit = (-5.0 + 1.0 * (~is_fiber) + 0.55 * competitor_fiber - 0.35 * cust.autopay.to_numpy()
                  + 0.25 * np.isin(cust.credit_band.to_numpy(), ["C", "D"]))
    tenure0 = ((pd.to_datetime(start) - signup).days / 30.4).to_numpy()
    nps_last = np.clip(rng.normal(7.4, 2.0, n) - 1.5 * (~is_fiber), 0, 10).round()
    ont_bad = is_fiber & (rng.random(n) < 0.07)        # chronically weak optics (dirty connector, bend)
    events = []
    cllis = wc.clli.to_numpy()
    for m_i, m in enumerate(month_idx):
        k = rng.poisson(len(cllis) * (0.25 if m_i < months - 4 else 0.6))
        for c in rng.choice(cllis, size=k):
            t0 = m + pd.Timedelta(days=int(rng.integers(0, 27)), hours=int(rng.integers(0, 23)))
            dur = float(rng.gamma(2.0, 90 if c[4:6] in ("OH", "PA") else 60))
            events.append((f"NE{len(events):07d}", c, t0, t0 + pd.Timedelta(minutes=dur),
                           rng.choice(["outage", "degradation", "maintenance"], p=[0.45, 0.4, 0.15]), m_i))
    ev = pd.DataFrame(events, columns=["event_id", "clli", "started_at", "ended_at", "event_type", "_m"])
    ev["duration_min"] = ((ev.ended_at - ev.started_at).dt.total_seconds() / 60).round(1)
    outage_by = ev[ev.event_type != "maintenance"].groupby(["clli", "_m"]).duration_min.sum()

    # ── monthly simulation ──────────────────────────────────────────────
    active = np.ones(n, dtype=bool)
    churn_month = np.full(n, -1)
    pim = np.full(n, -1)                                # price-increase month
    late_hist = np.zeros(n); tix_hist = np.zeros(n); call_hist = np.zeros(n)
    disp_hist = np.zeros(n); dispute_hist = np.zeros(n)
    monthly: dict[str, list[pd.DataFrame]] = {k: [] for k in
        ("usage", "ont", "dsl", "invoices", "revenue", "snap")}
    tix, calls, disp, txns, disputes, nps = [], [], [], [], [], []
    agents = [f"AG{i:04d}" for i in range(1, 181)]
    techs = [f"TC{i:04d}" for i in range(1, 121)]

    def stamp(m: pd.Timestamp) -> pd.Timestamp:
        return m + pd.Timedelta(days=int(rng.integers(0, 27)), minutes=int(rng.integers(0, 1440)))

    for m_i, m in enumerate(month_idx):
        drift = m_i >= months - 4
        idx = np.flatnonzero(active)
        k = len(idx)
        b = ban[idx]
        fib = is_fiber[idx]
        hit = (pim[idx] < 0) & (rng.random(k) < (0.10 if drift else 0.015))
        pim[idx[hit]] = m_i
        raised = pim[idx] >= 0
        cur_price = price[idx] * np.where(raised, 1.12 if drift else 1.07, 1.0)
        outage = outage_by.reindex(list(zip(clli[idx], [m_i] * k)), fill_value=0).to_numpy() * rng.uniform(0.3, 1.0, k)

        # line / optical quality
        rx = np.where(ont_bad[idx], rng.normal(-25.8, 1.2, k), rng.normal(-17.5, 2.5, k)).clip(-30, -8)
        los = rng.poisson(np.where(fib, 0.15 + 0.6 * (rx < -25) + 0.004 * outage, 0))
        snr = (14.0 - 0.45 * np.nan_to_num(loop_kft[idx]) + rng.normal(0, 1.5, k)).clip(1, 20)
        retrains = rng.poisson(np.where(~fib, 1.5 + 2.5 * (snr < 6) + 0.006 * outage, 0))
        bad_line = np.where(fib, rx < -25, snr < 6)

        gb_down = rng.gamma(3.0, np.where(fib, 190, 95))
        latency = np.where(fib, rng.normal(12, 3, k), rng.normal(36 + 1.2 * np.nan_to_num(loop_kft[idx]), 8, k)).clip(4)
        month_s = m.date()
        monthly["usage"].append(pd.DataFrame({"ban": b, "usage_month": month_s, "gb_down": gb_down.round(1),
            "gb_up": (gb_down * rng.uniform(0.05, 0.3, k)).round(1), "avg_latency_ms": latency.round(1),
            "outage_minutes": outage.round(1), "wifi_devices": rng.integers(2, 25, k)}))
        f = fib
        monthly["ont"].append(pd.DataFrame({"ban": b[f], "metric_month": month_s, "ont_model": loc.ont_model.to_numpy()[idx][f],
            "rx_power_dbm": rx[f].round(2), "rx_power_min_dbm": (rx[f] - rng.uniform(0.2, 2.0, f.sum())).round(2),
            "tx_power_dbm": rng.normal(2.5, 0.4, f.sum()).round(2), "los_events": los[f],
            "bip_errors": rng.poisson(np.where(rx[f] < -25, 900, 40)), "reboots": rng.poisson(0.2 + 0.5 * (rx[f] < -25)),
            "uptime_pct": (100 - outage[f] / 432 - los[f] * 0.05).clip(90, 100).round(3)}))
        c = ~fib
        monthly["dsl"].append(pd.DataFrame({"ban": b[c], "metric_month": month_s, "snr_margin_db": snr[c].round(1),
            "attenuation_db": (8 + 3.1 * np.nan_to_num(loop_kft[idx][c]) + rng.normal(0, 2, c.sum())).clip(5, 75).round(1),
            "sync_rate_down_mbps": np.minimum(P.down_mbps.reindex(plan_id[idx][c]).to_numpy(),
                                              (160 - 8.5 * np.nan_to_num(loop_kft[idx][c])).clip(3, 160)).round(1),
            "retrains": retrains[c], "crc_errors": rng.poisson(np.where(snr[c] < 6, 4000, 150))}))

        # events: tickets → calls → dispatches
        p_tix = 0.04 + 0.002 * outage.clip(0, 300) + 0.10 * bad_line + 0.03 * (~fib) + (0.04 if drift else 0)
        n_tix = rng.poisson(p_tix)
        n_calls = rng.poisson(0.10 + 0.9 * n_tix + 0.15 * raised + 0.05 * (~fib))
        tix_hist[idx] = 0.6 * tix_hist[idx] + n_tix
        call_hist[idx] = 0.6 * call_hist[idx] + n_calls
        disp_month = np.zeros(k)
        for j in np.flatnonzero(n_tix):
            for _ in range(n_tix[j]):
                cat = rng.choice(list(TICKET_CATEGORIES), p=[0.30, 0.47, 0.08, 0.15])
                opened = stamp(m)
                tid = f"TK{len(tix):08d}"
                tix.append((tid, b[j], opened, opened + pd.Timedelta(hours=float(rng.gamma(2, 22 if cat == "technical" else 10))),
                            cat, rng.choice(TICKET_CATEGORIES[cat]), rng.choice(["phone", "chat", "app"], p=[0.6, 0.25, 0.15]),
                            bool(rng.random() < 0.6 - 0.2 * bad_line[j])))
                if cat in ("technical", "installation") and rng.random() < 0.28 + 0.25 * bad_line[j]:
                    tech_ = "fiber" if fib[j] else "copper"
                    sched = opened + pd.Timedelta(hours=float(rng.uniform(4, 96)))
                    status = rng.choice(["completed", "missed", "rescheduled", "cancelled"], p=[0.82, 0.06, 0.08, 0.04])
                    disp.append((f"DS{len(disp):08d}", b[j], tid, clli[idx[j]], "repair" if cat == "technical" else "install",
                                 sched, sched + pd.Timedelta(hours=float(rng.uniform(0.7, 3.5))) if status == "completed" else pd.NaT,
                                 status, rng.choice(techs),
                                 rng.choice(DISPATCH_CAUSES[tech_], p=[0.3, 0.25, 0.15, 0.15, 0.15])))
                    disp_month[j] += 1
        disp_hist[idx] = 0.7 * disp_hist[idx] + disp_month
        for j in np.flatnonzero(n_calls):
            for _ in range(n_calls[j]):
                qs = list(CALL_QUEUES)
                q = rng.choice(qs, p=[CALL_QUEUES[x][0] for x in qs])
                if raised[j] and rng.random() < 0.4:
                    q = "retention" if rng.random() < 0.5 else "billing"
                t0 = stamp(m)
                senti = float(np.clip(rng.normal(0.15 - 0.35 * bad_line[j] - 0.3 * (q == "retention"), 0.4), -1, 1))
                calls.append((f"CL{len(calls):09d}", b[j], t0, int(rng.gamma(3, 140)), q, rng.choice(CALL_QUEUES[q][1]),
                              rng.choice(["resolved", "transferred", "escalated", "callback"], p=[0.62, 0.18, 0.08, 0.12]),
                              bool(call_hist[idx[j]] > 1.2), rng.choice(agents), round(senti, 2)))

        # billing: invoices, ledger, disputes, revenue
        promo_left = promo_months[idx] - (tenure0[idx] + m_i)
        discount = np.where(promo_left > 0, price[idx] - promo_price[idx], 0)
        one_time = np.where(rng.random(k) < 0.02, rng.choice([49.0, 99.0], size=k), 0.0)
        taxes = np.round((cur_price + one_time) * 0.071, 2)
        late = rng.random(k) < (0.06 + 0.08 * np.isin(cust.credit_band.to_numpy()[idx], ["C", "D"])
                                - 0.04 * cust.autopay.to_numpy()[idx])
        late_hist[idx] = 0.8 * late_hist[idx] + late
        credits = np.zeros(k)
        n_disp = rng.random(k) < (0.004 + 0.05 * (late & cust.autopay.to_numpy()[idx]) + 0.06 * (pim[idx] == m_i)
                                  + 0.002 * outage.clip(0, 300) / 60)
        for j in np.flatnonzero(n_disp):
            cat = ("late_fee_autopay" if late[j] and cust.autopay.iat[idx[j]] else "price_increase" if pim[idx[j]] == m_i
                   else "outage_credit" if outage[j] > 120 else rng.choice(DISPUTE_CATEGORIES))
            amt = {"late_fee_autopay": 10.0, "price_increase": round(float(cur_price[j] - price[idx[j]]), 2) or 5.0,
                   "outage_credit": round(float(cur_price[j] / 30 * max(1, outage[j] // 1440 + 1)), 2)}.get(cat, round(float(rng.uniform(8, 120)), 2))
            opened = stamp(m)
            status = rng.choice(["approved", "partial", "denied", "open"], p=[0.55, 0.15, 0.2, 0.1] if m_i < months - 1 else [0.2, 0.05, 0.05, 0.7])
            credit = amt if status == "approved" else round(amt / 2, 2) if status == "partial" else 0.0
            credits[j] += credit
            disputes.append((f"DP{len(disputes):08d}", b[j], invoice_id(b[j], m), opened.date(),
                             rng.choice(["email", "phone", "chat", "store"], p=[0.35, 0.45, 0.15, 0.05]), cat, amt, status,
                             (opened + pd.Timedelta(days=int(rng.integers(1, 21)))).date() if status != "open" else None, credit))
        dispute_hist[idx] = 0.7 * dispute_hist[idx] + n_disp
        late_fee = np.where(late, 10.0, 0.0)
        amount = np.round(cur_price - discount + one_time + taxes + late_fee - credits, 2)
        inv_status = np.where(late, "late", "paid")
        wo = late & (rng.random(k) < 0.08)
        inv_status = np.where(wo, "written_off" if m_i < months - 2 else "open", inv_status)
        inv_ids = np.array([invoice_id(x, m) for x in b])
        monthly["invoices"].append(pd.DataFrame({"invoice_id": inv_ids, "ban": b, "bill_cycle": m.strftime("%Y-%m"),
            "invoice_date": month_s, "due_date": (m + pd.Timedelta(days=21)).date(),
            "recurring_charges": np.round(cur_price - discount, 2), "one_time_charges": one_time, "taxes_fees": taxes,
            "credits": np.round(credits, 2), "late_fee": late_fee, "amount_due": amount, "status": inv_status}))
        monthly["revenue"].append(pd.DataFrame({"ban": b, "revenue_month": month_s, "technology": technology[idx],
            "plan_id": plan_id[idx], "clli": clli[idx], "state": state[idx],
            "recurring_revenue": np.round(cur_price - discount, 2), "one_time_revenue": one_time,
            "late_fee_revenue": late_fee, "credits": np.round(credits, 2),
            "net_revenue": np.round(cur_price - discount + one_time + late_fee - credits, 2)}))
        for j in np.flatnonzero(late_fee):
            txns.append((f"TX{len(txns):09d}", b[j], inv_ids[j], (m + pd.Timedelta(days=22)).date(), "late_fee", 10.0, None, "payment after due date"))
        for j in np.flatnonzero(credits):
            txns.append((f"TX{len(txns):09d}", b[j], inv_ids[j], (m + pd.Timedelta(days=int(rng.integers(3, 25)))).date(),
                         "credit", -round(float(credits[j]), 2), None, "dispute resolution"))
        paid = ~wo
        pay_day = rng.integers(1, 20, k) + np.where(late, rng.integers(22, 40, k), 0)
        for j in np.flatnonzero(paid):
            txns.append((f"TX{len(txns):09d}", b[j], inv_ids[j], (m + pd.Timedelta(days=int(pay_day[j]))).date(), "payment",
                         -float(amount[j]), "autopay" if cust.autopay.iat[idx[j]] else rng.choice(["card", "ach", "check", "store"]), None))

        # NPS (sampled)
        survey = rng.random(k) < 0.04
        nps_new = np.clip(nps_last[idx] - 0.004 * outage - 1.2 * n_tix - 1.0 * bad_line - 1.5 * (pim[idx] == m_i)
                          + rng.normal(0.3, 1.2, k), 0, 10).round()
        nps_last[idx[survey]] = nps_new[survey]
        for j in np.flatnonzero(survey):
            s = nps_new[j]
            band = "promoter" if s >= 9 else "passive" if s >= 7 else "detractor"
            nps.append((f"NPS{len(nps):07d}", b[j], stamp(m).date(), int(s), rng.choice(VERBATIM[band]),
                        {"promoter": "positive", "passive": "neutral", "detractor": "negative"}[band]))

        # churn hazard
        tenure = tenure0[idx] + m_i
        logit = (base_logit[idx] + 0.004 * outage.clip(0, 400) + 0.30 * tix_hist[idx] + 0.20 * call_hist[idx]
                 + 0.45 * disp_hist[idx] + 0.6 * dispute_hist[idx] + 0.9 * bad_line + 0.6 * late_hist[idx]
                 + 1.1 * raised * (m_i - pim[idx] < 3) - 0.18 * (nps_last[idx] - 7) - 0.5 * (tenure > 36)
                 + 0.35 * (promo_left <= 0) * (promo_left > -2))
        churned = rng.random(k) < 1 / (1 + np.exp(-logit))
        monthly["snap"].append(pd.DataFrame({
            "ban": b, "snapshot_month": month_s, "technology": technology[idx], "plan_id": plan_id[idx], "clli": clli[idx],
            "monthly_charge": np.round(cur_price, 2), "tenure_months": np.round(tenure, 1),
            "autopay": cust.autopay.to_numpy()[idx], "credit_band": cust.credit_band.to_numpy()[idx],
            "competitor_fiber_available": competitor_fiber[idx], "gb_down": gb_down.round(1),
            "avg_latency_ms": latency.round(1), "outage_minutes": outage.round(1), "tickets_month": n_tix,
            "tickets_decayed": tix_hist[idx].round(3), "calls_30d": n_calls, "calls_decayed": call_hist[idx].round(3),
            "dispatches_90d": disp_hist[idx].round(3), "disputes_90d": dispute_hist[idx].round(3),
            "late_payments_decayed": late_hist[idx].round(3), "nps_last": nps_last[idx],
            "ont_rx_power_dbm": np.where(fib, rx.round(2), np.nan), "ont_los_events": np.where(fib, los, np.nan),
            "dsl_snr_margin_db": np.where(~fib, snr.round(1), np.nan), "dsl_retrains": np.where(~fib, retrains, np.nan),
            "recent_price_increase": raised & (m_i - pim[idx] < 3), "promo_expired": promo_left <= 0,
            "segment": segment[idx], "state": state[idx], "_m": m_i}))
        churn_month[idx[churned]] = m_i
        active[idx[churned]] = False

    reasons = ["price", "competitor_fiber", "outages", "moving", "service_quality", "billing_issue"]
    end = [month_idx[c] + pd.Timedelta(days=int(rng.integers(1, 27))) if c >= 0 else pd.NaT for c in churn_month]
    subs = pd.DataFrame({
        "subscription_id": [f"SUB{i:08d}" for i in range(n)], "ban": ban, "plan_id": plan_id,
        "start_date": signup.date, "end_date": pd.to_datetime(pd.Series(end)).dt.date,
        "status": np.where(churn_month >= 0, "cancelled", "active"),
        "cancel_reason": np.where(churn_month >= 0, rng.choice(reasons, size=n, p=[0.3, 0.22, 0.18, 0.12, 0.1, 0.08]), None),
        "contract_term": rng.choice(["month_to_month", "12_month", "24_month"], size=n, p=[0.7, 0.2, 0.1]),
    })
    snap = pd.concat(monthly["snap"], ignore_index=True)
    c = pd.Series(churn_month, index=ban).reindex(snap.ban).to_numpy()
    snap["churned_next_90d"] = (c >= snap._m.to_numpy()) & (c < snap._m.to_numpy() + 3)
    snap["label_complete"] = snap._m.to_numpy() <= months - 3
    snap = snap.drop(columns="_m")

    tables = {
        "wire_centers": wc,
        "customers": cust,
        "service_locations": loc,
        "plans": PLANS.copy(),
        "subscriptions": subs,
        "usage_monthly": pd.concat(monthly["usage"], ignore_index=True),
        "ont_metrics": pd.concat(monthly["ont"], ignore_index=True),
        "copper_line_metrics": pd.concat(monthly["dsl"], ignore_index=True),
        "network_events": ev.drop(columns="_m"),
        "service_tickets": pd.DataFrame(tix, columns=["ticket_id", "ban", "opened_at", "closed_at", "category", "subcategory",
                                                      "channel", "first_contact_resolved"]),
        "customer_calls": pd.DataFrame(calls, columns=["call_id", "ban", "call_start", "duration_sec", "queue", "reason",
                                                       "disposition", "repeat_call_7d", "agent_id", "sentiment_score"]),
        "dispatches": pd.DataFrame(disp, columns=["dispatch_id", "ban", "ticket_id", "clli", "dispatch_type", "scheduled_at",
                                                  "completed_at", "status", "tech_id", "cause"]),
        "invoices": pd.concat(monthly["invoices"], ignore_index=True),
        "transactions": pd.DataFrame(txns, columns=["txn_id", "ban", "invoice_id", "txn_date", "txn_type", "amount",
                                                    "payment_method", "reason"]),
        "disputes": pd.DataFrame(disputes, columns=["dispute_id", "ban", "invoice_id", "opened_date", "channel", "category",
                                                    "disputed_amount", "status", "resolved_date", "credit_amount"]),
        "revenue": pd.concat(monthly["revenue"], ignore_index=True),
        "nps_surveys": pd.DataFrame(nps, columns=["survey_id", "ban", "survey_date", "nps_score", "verbatim", "sentiment_label"]),
        "customer_monthly": snap,
    }
    d = tables["dispatches"]
    d = d.sort_values(["ban", "scheduled_at"])
    prev = d.groupby("ban").scheduled_at.shift()
    d["repeat_dispatch_30d"] = (d.scheduled_at - prev).dt.days.le(30).fillna(False)
    tables["dispatches"] = d.sort_values("dispatch_id").reset_index(drop=True)
    return Dataset(tables)
