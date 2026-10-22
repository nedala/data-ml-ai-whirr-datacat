"""Tokenisation, light stemming, domain synonyms and sparse (BM25) vectors —
shared by the offline embedder, the lexical retrievers, the extractive
answerer and the lexical eval fallbacks."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

STOP = set("""a an and are as at be been but by can could did do does for from had has have how i if in into is it its
me my no not of on or our so than that the their them then there these they this to was we were what when where which
who why will with you your yours i'm im it's dont don't can't cant please hi hello thanks thank us also any all just
would should may might about after before over under up down out off very more most such only own same too s t""".split())

_TOKEN = re.compile(r"[a-z0-9]+(?:[-./][a-z0-9]+)*")

# ISP / front-office synonyms used for offline query expansion
SYNONYMS: dict[str, list[str]] = {
    "ont": ["optical network terminal", "fiber terminal"],
    "olt": ["optical line terminal"],
    "truck roll": ["dispatch", "technician visit"],
    "tech": ["technician"],
    "outage": ["service down", "no service"],
    "down": ["outage", "no internet"],
    "no internet": ["outage", "offline", "los"],
    "offline": ["no internet", "los"],
    "red light": ["los"],
    "slow": ["speed", "wifi"],
    "wifi": ["wi-fi", "wi-fi coverage"],
    "dsl": ["copper", "dslam", "line test"],
    "copper": ["dsl", "dslam"],
    "retrains": ["retrain", "sync", "line test"],
    "ban": ["billing account number", "account number"],
    "account number": ["ban", "billing account number"],
    "clli": ["wire center"],
    "cancel": ["retention", "save offer", "disconnect"],
    "leaving": ["cancel", "retention"],
    "switching": ["cancel", "competitor", "retention"],
    "late fee": ["late fee reversal", "waiver"],
    "late charge": ["late fee"],
    "overcharged": ["billing dispute", "incorrect charge"],
    "charged twice": ["duplicate charge", "billing dispute"],
    "dispute": ["billing dispute", "credit adjustment"],
    "refund": ["credit", "adjustment"],
    "credit": ["adjustment"],
    "move": ["moves", "transfer service"],
    "moving": ["move", "transfer service"],
    "port": ["porting", "number portability"],
    "keep my number": ["porting", "port-in"],
    "return": ["equipment return"],
    "modem": ["equipment", "router"],
    "payment plan": ["payment arrangement", "installment"],
    "can't pay": ["payment arrangement", "past due"],
    "behind": ["past due", "payment arrangement"],
    "escalate": ["escalation matrix", "supervisor"],
    "manager": ["escalation", "supervisor"],
    "promo": ["promotion", "promotional price"],
    "price increase": ["price change", "promotion ended"],
    "autopay": ["automatic payment"],
    "gpon": ["pon"],
    "xgs-pon": ["pon", "10g"],
}


def stem(t: str) -> str:
    if len(t) <= 3 or any(c.isdigit() for c in t):
        return t
    for suf, rep in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            t = t[: -len(suf)] + rep
            break
    return t


def tokens(text: str, keep_stop: bool = False) -> list[str]:
    out = []
    for t in _TOKEN.findall((text or "").lower()):
        if not keep_stop and t in STOP:
            continue
        out.append(stem(t))
    return out


def expand_query(q: str) -> str:
    """Append domain synonyms for phrases present in the query (offline rewrite)."""
    low = q.lower()
    extra: list[str] = []
    for k, vals in SYNONYMS.items():
        if re.search(r"\b" + re.escape(k) + r"\b", low):
            extra.extend(v for v in vals if v not in low)
    return q if not extra else q + " (" + "; ".join(dict.fromkeys(extra)) + ")"


def h32(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=4).digest(), "little") & 0x7FFFFFFF


# ── BM25 sparse vectors (Qdrant applies IDF server-side with modifier=idf) ──
K1, B, AVGDL = 1.2, 0.75, 120.0


def sparse_doc(text: str) -> tuple[list[int], list[float]]:
    toks = tokens(text)
    toks += [a + "_" + b for a, b in zip(toks, toks[1:])]       # bigrams help phrases
    tf = Counter(toks)
    dl = max(1, len(toks))
    idx, val = [], []
    seen: dict[int, float] = {}
    for t, f in tf.items():
        w = f * (K1 + 1) / (f + K1 * (1 - B + B * dl / AVGDL))
        i = h32(t)
        seen[i] = seen.get(i, 0.0) + w
    for i, w in sorted(seen.items()):
        idx.append(i)
        val.append(round(w, 5))
    return idx, val


def sparse_query(text: str) -> tuple[list[int], list[float]]:
    toks = tokens(text)
    toks += [a + "_" + b for a, b in zip(toks, toks[1:])]
    seen: dict[int, float] = {}
    for t in toks:
        seen[h32(t)] = 1.0
    items = sorted(seen.items())
    return [i for i, _ in items], [v for _, v in items]


# ── lexical helpers ──────────────────────────────────────────────────────
def overlap(a: str, b: str) -> float:
    """Share of content tokens of `a` that occur in `b` (0..1)."""
    ta = set(tokens(a))
    if not ta:
        return 0.0
    return len(ta & set(tokens(b))) / len(ta)


def bm25_scores(query: str, docs: list[str]) -> list[float]:
    """Small in-process BM25 (used by the memory backend and offline rerank)."""
    q = tokens(query)
    dtoks = [tokens(d) for d in docs]
    n = len(docs)
    if not n:
        return []
    avg = sum(len(d) for d in dtoks) / n or 1.0
    df = Counter(t for d in dtoks for t in set(d))
    out = []
    for d in dtoks:
        tf = Counter(d)
        s = 0.0
        for t in q:
            if t not in tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            f = tf[t]
            s += idf * f * (K1 + 1) / (f + K1 * (1 - B + B * len(d) / avg))
        out.append(s)
    return out
