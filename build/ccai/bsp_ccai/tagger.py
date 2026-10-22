"""Hybrid call tagger.

1. Candidate shortlist   — embeddings (`llm.embed`, `embed-default`; leaf embeddings cached per
                           taxonomy version) ∪ the offline model's top-k (TF-IDF retrieval).
2. LLM selection         — `classify-default` returns strict JSON {primary, secondary[≤2],
                           confidence, rationale, evidence_turn_ids, summary, resolution_status},
                           validated against taxonomy ids (one repair retry, then offline fallback).
3. Calibration           — combines the LLM self-score, the retrieval margin and the agreement
                           between methods into one confidence in [0, 1].

Offline (no gateway, `llm.available()` false, or route=offline): TF-IDF (word + char n-grams,
robust to ASR noise) → logistic regression + nearest-centroid ensemble trained on the
taxonomy's examples, names, definitions and keywords. Customer turns are scored one by one so
multi-intent calls ("oh and one more thing…") yield secondary tags and evidence turns.

Also extracts: summary, customer-sentiment trajectory, resolution status and churn-risk flag.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config
from .taxonomy import Taxonomy

log = logging.getLogger("ccai.tagger")

SECONDARY_CUES = re.compile(r"\b(one more thing|another thing|while i have you|before you go|also|and also|one other thing)\b", re.I)
# the customer reading out the BAN (10 digits, grouped or digit by digit) — verification, not a contact reason
VERIFY_RE = re.compile(r"\b(3\d{9}|3\d{2}[\s.-]\d{3}[\s.-]\d{4}|3(?:\s\d){9}|three one (?:zero|oh)|account number is)\b", re.I)
COMPETITORS = re.compile(r"\b(spectrum|t-?mobile|verizon|starlink|xfinity|comcast|at&t|att|frontier|another company|other provider)\b", re.I)
CLOSING_RE = re.compile(r"^\W*(ok(ay)?|alright|fine|perfect|great|yes|yeah|no|nope|thanks?|thank you)\b.{0,40}$", re.I)
CANCEL_RE = re.compile(r"\b(cancel|disconnect|switching|leaving|shut (it )?off)\b", re.I)


# ── offline model ────────────────────────────────────────────────────────
class OfflineModel:
    def __init__(self, tax: Taxonomy) -> None:
        from scipy.sparse import hstack
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import normalize

        self.tax = tax
        self.ids = tax.ids
        docs, y = [], []
        for i, leaf in enumerate(tax.leaves):
            parts = list(leaf.examples) + [f"{leaf.name}. {leaf.definition} {leaf.include}", " ".join(leaf.keywords),
                                           leaf.name + " " + " ".join(leaf.keywords[:4])]
            for d in parts:
                docs.append(d)
                y.append(i)
        self._hstack, self._normalize = hstack, normalize
        self.word = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=1, stop_words="english",
                                    token_pattern=r"(?u)\b[a-z0-9][a-z0-9']+\b", lowercase=True)
        self.char = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True, min_df=2, lowercase=True)
        X = self._vec(docs, fit=True)
        self.y = np.array(y)
        self.lr = LogisticRegression(C=20.0, max_iter=3000)
        self.lr.fit(X, self.y)
        cent = np.vstack([np.asarray(X[self.y == i].mean(axis=0)).ravel() for i in range(len(self.ids))])
        self.centroids = normalize(cent)
        self.n_train = len(docs)

    def _vec(self, texts: list[str], fit: bool = False):
        if fit:
            Xw, Xc = self.word.fit_transform(texts), self.char.fit_transform(texts)
        else:
            Xw, Xc = self.word.transform(texts), self.char.transform(texts)
        return self._normalize(self._hstack([Xw, 0.6 * Xc]).tocsr())

    def proba(self, texts: list[str]) -> np.ndarray:
        X = self._vec(texts)
        p_lr = self.lr.predict_proba(X)
        cos = np.asarray(X @ self.centroids.T)
        z = cos * 18.0
        z -= z.max(axis=1, keepdims=True)
        p_c = np.exp(z)
        p_c /= p_c.sum(axis=1, keepdims=True)
        return 0.6 * p_lr + 0.4 * p_c


_models: dict[str, OfflineModel] = {}
_model_lock = threading.Lock()


def offline_model(tax: Taxonomy) -> OfflineModel:
    key = tax.version + tax.digest
    with _model_lock:
        if key not in _models:
            t = time.time()
            _models.clear()
            _models[key] = OfflineModel(tax)
            log.info("offline tagger trained on %d docs in %.1fs", _models[key].n_train, time.time() - t)
        return _models[key]


# ── call helpers ─────────────────────────────────────────────────────────
def substantive_customer_turns(call: dict) -> list[dict]:
    out = []
    for t in call["turns"]:
        if t["role"] != "customer":
            continue
        words = t["text"].split()
        if len(words) < 4 or VERIFY_RE.search(t["text"]) or CLOSING_RE.search(t["text"]):
            continue
        out.append(t)
    if not out:
        out = [t for t in call["turns"] if t["role"] == "customer"] or call["turns"]
    return out


def customer_text(call: dict, limit: int = 4000) -> str:
    return " ".join(t["text"] for t in substantive_customer_turns(call))[:limit]


@dataclass
class OfflineResult:
    primary: str
    primary_score: float
    secondary: list[dict]
    candidates: list[dict]
    evidence: list[int]
    margin: float
    agreement: float
    probs: np.ndarray


def offline_tag(call: dict, tax: Taxonomy) -> OfflineResult:
    model = offline_model(tax)
    turns = substantive_customer_turns(call)
    texts = [t["text"] for t in turns]
    P = model.proba(texts + [" ".join(texts)])
    per_turn, doc = P[:-1], P[-1]
    # segment the call: turns introduced by a cue ("oh and one more thing") start a new intent segment
    # …and so does the first customer turn after the agent has wrapped up the previous issue
    wrapped_before: set[int] = set()
    wrapped = False
    for t in call["turns"]:
        if t["role"] == "agent" and any(p.search(t["text"]) for _, p in RES_PATTERNS):
            wrapped = True
        elif t["role"] == "customer" and wrapped and len(t["text"].split()) >= 4 and not CLOSING_RE.search(t["text"]):
            wrapped_before.add(t["id"])
            wrapped = False
    seg_ids, seg = [], 0
    for i, t in enumerate(turns):
        if i > 0 and (SECONDARY_CUES.search(t["text"][:40]) or t["id"] in wrapped_before):
            seg += 1
        seg_ids.append(seg)
    seg_ids = np.array(seg_ids)
    # primary: first segment dominates (customers lead with the main reason), whole-call doc adds context
    first = per_turn[seg_ids == 0] if (seg_ids == 0).any() else per_turn
    w = first.max(axis=1, keepdims=True)
    agg_first = (first * w).sum(axis=0) / max(1e-9, w.sum())
    score = 0.65 * agg_first + 0.35 * doc
    order = np.argsort(-score)
    p_idx = int(order[0])
    primary = model.ids[p_idx]
    margin = float(score[order[0]] - score[order[1]])
    top_turn = per_turn.argmax(axis=1)
    agreement = float((top_turn[seg_ids == 0] == p_idx).mean()) if (seg_ids == 0).any() else 0.0
    # secondary: best leaf of each later segment, plus strong off-primary turns in the first segment
    sec: dict[str, float] = {}
    for s in range(1, seg + 1):
        ps = per_turn[seg_ids == s]
        if not len(ps):
            continue
        wts = ps.max(axis=1, keepdims=True)
        agg = (ps * wts).sum(axis=0) / max(1e-9, wts.sum())
        j = int(agg.argmax())
        if model.ids[j] != primary and agg[j] >= 0.08:
            sec[model.ids[j]] = max(sec.get(model.ids[j], 0), float(agg[j]))
    for i in np.flatnonzero(seg_ids == 0):
        j = int(top_turn[i])
        if model.ids[j] != primary and per_turn[i, j] >= 0.35 and model.tax.by_id[model.ids[j]].l2 != model.tax.by_id[primary].l2:
            sec[model.ids[j]] = max(sec.get(model.ids[j], 0), float(per_turn[i, j]) * 0.8)
    secondary = [{"leaf": k, "score": round(v, 4)} for k, v in sorted(sec.items(), key=lambda kv: -kv[1])[:2]]
    ev_scores = per_turn[:, p_idx]
    evidence = [int(turns[i]["id"]) for i in np.argsort(-ev_scores)[:2] if ev_scores[i] > 0.02]
    for s in secondary:
        j = model.ids.index(s["leaf"])
        k = int(per_turn[:, j].argmax())
        s["evidence"] = [int(turns[k]["id"])]
    cands = [{"leaf": model.ids[int(i)], "score": round(float(score[int(i)]), 4)} for i in order[:10]]
    return OfflineResult(primary, float(score[p_idx]), secondary, cands, evidence, margin, agreement, score)


# ── calibration ──────────────────────────────────────────────────────────
DEFAULT_CALIBRATION = {
    # logistic over [p1, margin, agreement, llm_self, method_agreement]; hand-set priors,
    # refit from reviewer feedback via POST /api/quality/recalibrate
    "offline": {"bias": -2.4, "w": [5.0, 7.0, 1.6, 0.0, 0.0]},
    "llm": {"bias": -3.0, "w": [1.5, 3.0, 0.5, 2.6, 1.8]},
    "fitted_on": 0,
}


def _cal_path() -> Path:
    return config.data_path("models") / "calibration.json"


def load_calibration() -> dict:
    try:
        return json.loads(_cal_path().read_text())
    except Exception:
        return json.loads(json.dumps(DEFAULT_CALIBRATION))


def calibrate(features: list[float], route: str, cal: dict | None = None) -> float:
    cal = cal or load_calibration()
    c = cal["llm" if route == "llm" else "offline"]
    z = c["bias"] + sum(w * f for w, f in zip(c["w"], features))
    return round(1 / (1 + math.exp(-z)), 4)


def band(conf: float, threshold: float) -> str:
    return "high" if conf >= max(threshold + 0.2, 0.8) else "medium" if conf >= threshold else "low"


def fit_calibration(rows: list[dict]) -> dict:
    """rows: [{features:[…], route, correct: bool}] → refit per-route logistic (needs both classes)."""
    from sklearn.linear_model import LogisticRegression

    cal = load_calibration()
    fitted = 0
    for route in ("offline", "llm"):
        rs = [r for r in rows if (r["route"] == "llm") == (route == "llm") and r.get("features")]
        ys = [int(r["correct"]) for r in rs]
        if len(rs) >= 20 and 0 < sum(ys) < len(ys):
            m = LogisticRegression(C=1.0).fit(np.array([r["features"] for r in rs]), np.array(ys))
            cal[route] = {"bias": float(m.intercept_[0]), "w": [float(x) for x in m.coef_[0]]}
            fitted += len(rs)
    cal["fitted_on"] = fitted
    cal["fitted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _cal_path().write_text(json.dumps(cal, indent=2))
    return cal


# ── extraction ───────────────────────────────────────────────────────────
RES_PATTERNS = [
    ("escalated", re.compile(r"(transfer(ring)? you to my supervisor|escalat|supervisor now|transferring you)", re.I)),
    ("unresolved", re.compile(r"(wasn't able to resolve|nothing more i can do|unable to (help|resolve)|couldn't resolve)", re.I)),
    ("pending_followup", re.compile(r"(ticket|scheduled a technician|follow up|business days|reference|submitted that request|technician for)", re.I)),
    ("resolved", re.compile(r"(taken care of|all set|been fixed|done on my end|reflected|resolved|fixed it|that's done)", re.I)),
]


def sentiment_trajectory(call: dict) -> tuple[str, list[float]]:
    series = [float(t["sentiment"]) for t in call["turns"] if t["role"] == "customer" and t.get("sentiment") is not None]
    if not series:
        return "unknown", []
    k = max(1, len(series) // 3)
    start, end = sum(series[:k]) / k, sum(series[-k:]) / k
    mean = sum(series) / len(series)
    if end - start > 0.25:
        traj = "improving"
    elif start - end > 0.25:
        traj = "declining"
    else:
        traj = "stable-" + ("positive" if mean > 0.15 else "negative" if mean < -0.15 else "neutral")
    return traj, [round(x, 2) for x in series]


def resolution_status(call: dict) -> str:
    agent_tail = [t["text"] for t in call["turns"] if t["role"] == "agent"][-6:]
    text = " ".join(agent_tail)
    for name, pat in RES_PATTERNS:
        if pat.search(text):
            return name
    return "unknown"


def churn_signal(call: dict, tax: Taxonomy, primary: str, secondary: list[dict], series: list[float],
                 resolution: str) -> tuple[bool, list[str]]:
    reasons = []
    by = tax.by_id
    if by[primary].churn_risk:
        reasons.append(f"contact reason: {by[primary].name}")
    for s in secondary:
        if by.get(s["leaf"]) and by[s["leaf"]].churn_risk:
            reasons.append(f"secondary reason: {by[s['leaf']].name}")
    ct = customer_text(call)
    if CANCEL_RE.search(ct):
        reasons.append("cancellation language")
    if COMPETITORS.search(ct):
        reasons.append("competitor mentioned")
    if series and series[-1] < -0.4 and resolution in ("unresolved", "escalated"):
        reasons.append(f"ended negative and {resolution}")
    strong = any(r.startswith(("contact reason", "cancellation", "competitor")) for r in reasons)
    return bool(strong or len(reasons) >= 2), reasons


RES_TEXT = {"resolved": "Resolved on the call", "pending_followup": "Follow-up pending (ticket / visit)",
            "escalated": "Escalated", "unresolved": "Not resolved", "unknown": "Outcome unclear"}


def offline_summary(call: dict, tax: Taxonomy, primary: str, secondary: list[dict], resolution: str, traj: str,
                    evidence: list[int]) -> str:
    by = tax.by_id
    who = f"Customer ({call.get('ban')})" if call.get("ban") else "Customer"
    s = f"{who} called about {by[primary].name.lower()}"
    if secondary:
        s += "; also " + ", ".join(by[x["leaf"]].name.lower() for x in secondary if x["leaf"] in by)
    s += f". {RES_TEXT.get(resolution, resolution)}. Sentiment {traj.replace('-', ' ')}."
    if evidence:
        quote = call["turns"][evidence[0]]["text"]
        s += f' Key quote: "{quote[:140]}{"…" if len(quote) > 140 else ""}"'
    return s


# ── LLM route ────────────────────────────────────────────────────────────
_emb_lock = threading.Lock()


def leaf_embeddings(tax: Taxonomy, alias: str) -> np.ndarray:
    from bsp_common import llm

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", alias)
    path = config.data_path("cache") / f"leaf_emb_{tax.version}_{tax.digest}_{safe}.json"
    with _emb_lock:
        if path.exists():
            return np.array(json.loads(path.read_text()))
        vecs: list[list[float]] = []
        docs = [x.doc() for x in tax.leaves]
        for i in range(0, len(docs), 32):
            vecs += llm.embed(docs[i:i + 32], model=alias)
        path.write_text(json.dumps(vecs))
        return np.array(vecs)


def _unit(a: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(a, axis=-1, keepdims=True)
    return a / np.where(n == 0, 1, n)


SYSTEM_PROMPT = """You tag DataPlatform (US internet provider: fiber (FTTH, ONT) and copper DSL) contact-centre calls
with contact reasons from a fixed taxonomy. Choose ONLY ids from the candidate list. The primary reason is why the
customer called; add up to 2 secondary reasons only if the customer clearly raised another distinct issue.
Return ONLY a JSON object:
{"primary": "<leaf id>", "secondary": ["<leaf id>", ...], "confidence": <0..1>, "rationale": "<= 30 words",
 "evidence_turn_ids": [<turn ids that show the reason>], "summary": "<= 50 words, neutral, no personal data beyond the BAN>",
 "resolution_status": "resolved|pending_followup|escalated|unresolved|unknown"}"""


def _transcript_for_prompt(call: dict, limit: int = 9000) -> str:
    lines = [f"[{t['id']}] {t['role'].upper()}: {t['text']}" for t in call["turns"]]
    out, n = [], 0
    for ln in lines:
        n += len(ln) + 1
        if n > limit:
            out.append("[…truncated…]")
            break
        out.append(ln)
    return "\n".join(out)


def llm_tag(call: dict, tax: Taxonomy, off: OfflineResult, st: dict) -> dict:
    from bsp_common import llm

    by = tax.by_id
    cand_ids = [c["leaf"] for c in off.candidates[:8]]
    emb_top, emb_margin = None, None
    try:
        L = _unit(leaf_embeddings(tax, st["embed_alias"]))
        v = _unit(np.array(llm.embed([customer_text(call, 2000)], model=st["embed_alias"])[0]))
        sims = L @ v
        order = np.argsort(-sims)
        emb_top = tax.ids[int(order[0])]
        emb_margin = float(sims[order[0]] - sims[order[1]])
        for i in order[:10]:
            if tax.ids[int(i)] not in cand_ids:
                cand_ids.append(tax.ids[int(i)])
    except Exception as e:  # embeddings optional: fall back to TF-IDF retrieval only
        log.info("embedding shortlist unavailable: %s", e)
    cand_ids = cand_ids[:15]
    cand_txt = "\n".join(f"- {i}: {by[i].name} — {by[i].definition}" for i in cand_ids)
    user = f"Candidates:\n{cand_txt}\n\nTranscript (turn ids in brackets):\n{_transcript_for_prompt(call)}"
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
    err = ""
    t0 = time.time()
    for attempt in range(2):
        if err:
            msgs = msgs[:2] + [{"role": "user", "content": f"Your previous answer was invalid ({err}). Return only valid JSON using candidate ids."}]
        text = llm.chat(msgs, model=st["classify_alias"], temperature=0, response_format={"type": "json_object"})
        m = re.search(r"\{.*\}", text, re.S)
        try:
            out = json.loads(m.group(0) if m else text)
        except Exception:
            err = "not JSON"
            continue
        p = str(out.get("primary", "")).strip()
        if p not in by:
            err = f"unknown primary id {p!r}"
            continue
        secs = [s for s in (out.get("secondary") or []) if isinstance(s, str) and s in by and s != p][:2]
        n_turns = len(call["turns"])
        ev = [int(i) for i in (out.get("evidence_turn_ids") or []) if str(i).lstrip("-").isdigit() and 0 <= int(i) < n_turns][:5]
        try:
            self_conf = max(0.0, min(1.0, float(out.get("confidence", 0.5))))
        except (TypeError, ValueError):
            self_conf = 0.5
        res = out.get("resolution_status")
        return {"primary": p, "secondary": secs, "self_conf": self_conf, "rationale": str(out.get("rationale", ""))[:400],
                "evidence": ev or off.evidence, "summary": str(out.get("summary", ""))[:600] or None,
                "resolution": res if res in RES_TEXT else None, "emb_top": emb_top, "emb_margin": emb_margin,
                "candidates": cand_ids, "latency_ms": int((time.time() - t0) * 1000), "attempts": attempt + 1}
    raise ValueError(f"LLM output invalid after retry: {err}")


# ── public entry point ───────────────────────────────────────────────────
def tag_call(call: dict, tax: Taxonomy, st: dict, use_llm: bool) -> dict:
    t0 = time.time()
    off = offline_tag(call, tax)
    traj, series = sentiment_trajectory(call)
    resolution = resolution_status(call)
    by = tax.by_id
    route, model_name, llm_out, fallback_reason = "offline", "tfidf-lr-centroid", None, None
    if use_llm:
        try:
            llm_out = llm_tag(call, tax, off, st)
            route, model_name = "llm", st["classify_alias"]
        except Exception as e:
            fallback_reason = f"{e.__class__.__name__}: {e}"[:300]
            route = "offline-fallback"
            log.warning("LLM tagging failed for %s → offline: %s", call.get("call_id"), fallback_reason)
    if llm_out:
        primary = llm_out["primary"]
        off_rank = next((i for i, c in enumerate(off.candidates) if c["leaf"] == primary), 99)
        method_agree = 1.0 if off_rank == 0 else 0.6 if off_rank < 3 else 0.2
        if llm_out["emb_top"]:
            method_agree = 0.7 * method_agree + 0.3 * (1.0 if llm_out["emb_top"] == primary else 0.0)
        p_score = float(off.probs[tax.ids.index(primary)])
        feats = [p_score, off.margin if off_rank == 0 else 0.0, off.agreement, llm_out["self_conf"], method_agree]
        conf = calibrate(feats, "llm")
        secondary = [{"leaf": s, "score": None} for s in llm_out["secondary"]]
        evidence = llm_out["evidence"]
        rationale = llm_out["rationale"]
        resolution = llm_out["resolution"] or resolution
        signals = {"llm_self": llm_out["self_conf"], "retrieval_margin": round(off.margin, 4),
                   "embedding_margin": llm_out["emb_margin"], "offline_rank": off_rank, "method_agreement": method_agree,
                   "turn_agreement": round(off.agreement, 3), "attempts": llm_out["attempts"], "features": feats}
        candidates = [{"leaf": c, "score": round(float(off.probs[tax.ids.index(c)]), 4)} for c in llm_out["candidates"]]
    else:
        primary = off.primary
        feats = [off.primary_score, off.margin, off.agreement, 0.0, 0.0]
        conf = calibrate(feats, "offline")
        secondary = off.secondary
        evidence = off.evidence
        top_kw = [k for k in by[primary].keywords if k.lower() in customer_text(call).lower()][:3]
        rationale = (f"Closest match to '{by[primary].name}'" + (f" (keywords: {', '.join(top_kw)})" if top_kw else "")
                     + f"; score {off.primary_score:.2f}, margin {off.margin:.2f} over the next reason.")
        signals = {"retrieval_score": round(off.primary_score, 4), "retrieval_margin": round(off.margin, 4),
                   "turn_agreement": round(off.agreement, 3), "features": feats}
        if fallback_reason:
            signals["fallback_reason"] = fallback_reason
        candidates = off.candidates
    churn, churn_reasons = churn_signal(call, tax, primary, secondary, series, resolution)
    summary = (llm_out or {}).get("summary") or offline_summary(call, tax, primary, secondary, resolution, traj, evidence)
    return {"primary_leaf": primary, "secondary": secondary, "candidates": candidates, "confidence": conf,
            "confidence_band": band(conf, float(st.get("confidence_threshold", 0.6))), "signals": signals,
            "rationale": rationale, "evidence_turns": evidence, "summary": summary, "sentiment_trajectory": traj,
            "sentiment_series": series, "resolution_status": resolution, "churn_risk": churn, "churn_reasons": churn_reasons,
            "model": model_name, "route": route, "taxonomy_version": tax.version,
            "latency_ms": int((time.time() - t0) * 1000)}


_llm_state = {"at": 0.0, "ok": False}


def llm_available(ttl: float = 30.0) -> bool:
    if time.time() - _llm_state["at"] > ttl:
        try:
            from bsp_common import llm

            _llm_state["ok"] = bool(llm.available())
        except Exception:
            _llm_state["ok"] = False
        _llm_state["at"] = time.time()
    return _llm_state["ok"]


def should_use_llm(st: dict) -> bool:
    route = st.get("route", "auto")
    if route == "offline":
        return False
    return llm_available()
