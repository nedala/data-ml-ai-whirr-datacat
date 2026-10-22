"""Load, validate and publish the contact-reason taxonomy (config/taxonomy/ccai_taxonomy.yaml)."""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import config

EXPECTED_LEAVES = 104
ID_RE = re.compile(r"^[A-Z]{3}\.[A-Z]{2,4}\.[A-Z0-9_]+$")
REQUIRED = ("id", "name", "definition", "include", "exclude", "examples", "keywords", "mapping", "churn_risk")


@dataclass
class Leaf:
    id: str
    name: str
    definition: str
    include: str
    exclude: str
    examples: list[str]
    keywords: list[str]
    mapping: dict
    churn_risk: bool
    l1: str
    l1_name: str
    l2: str
    l2_name: str

    def doc(self) -> str:
        """Description used for retrieval/embeddings (no exclude text: it names other leaves)."""
        return f"{self.name}. {self.definition} {self.include} Examples: {' '.join(self.examples)} Keywords: {', '.join(self.keywords)}"

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Taxonomy:
    version: str
    name: str
    digest: str
    leaves: list[Leaf]
    domains: list[dict] = field(default_factory=list)
    path: str = ""

    @property
    def by_id(self) -> dict[str, Leaf]:
        return {x.id: x for x in self.leaves}

    @property
    def ids(self) -> list[str]:
        return [x.id for x in self.leaves]

    def tree(self) -> list[dict]:
        out = []
        for d in self.domains:
            out.append({"id": d["id"], "name": d["name"], "groups": [
                {"id": g["id"], "name": g["name"], "leaves": [x["id"] for x in g["leaves"]]} for g in d["groups"]]})
        return out


def parse(text: str, path: str = "") -> Taxonomy:
    doc = yaml.safe_load(text) or {}
    leaves = []
    for d in doc.get("domains", []):
        for g in d.get("groups", []):
            for x in g.get("leaves", []):
                leaves.append(Leaf(id=x.get("id", ""), name=x.get("name", ""), definition=x.get("definition", ""),
                                   include=x.get("include", ""), exclude=x.get("exclude", ""),
                                   examples=list(x.get("examples") or []), keywords=list(x.get("keywords") or []),
                                   mapping=dict(x.get("mapping") or {}), churn_risk=bool(x.get("churn_risk")),
                                   l1=d.get("id", ""), l1_name=d.get("name", ""), l2=g.get("id", ""), l2_name=g.get("name", "")))
    return Taxonomy(version=str(doc.get("version", "0")), name=str(doc.get("name", "")),
                    digest=hashlib.sha256(text.encode()).hexdigest()[:12], leaves=leaves,
                    domains=doc.get("domains", []), path=path)


def validate(tax: Taxonomy, expected: int = EXPECTED_LEAVES) -> list[str]:
    errs: list[str] = []
    ids = tax.ids
    if len(ids) != expected:
        errs.append(f"expected {expected} leaves, found {len(ids)}")
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        errs.append(f"duplicate leaf ids: {sorted(dup)}")
    l1s = [d.get("id") for d in tax.domains]
    if len(set(l1s)) != len(l1s):
        errs.append("duplicate L1 ids")
    for x in tax.leaves:
        for k in REQUIRED:
            v = getattr(x, k)
            if v in ("", None, [], {}) and k != "churn_risk":
                errs.append(f"{x.id or '?'}: missing {k}")
        if not ID_RE.match(x.id):
            errs.append(f"{x.id}: id must look like ABC.GRP.LEAF_NAME")
        if not x.id.startswith(x.l2 + ".") or not x.l2.startswith(x.l1 + "."):
            errs.append(f"{x.id}: id must start with its L2 id ({x.l2}) which starts with its L1 id ({x.l1})")
        if not 3 <= len(x.examples) <= 5:
            errs.append(f"{x.id}: needs 3-5 examples (has {len(x.examples)})")
        for mk in ("ticket_category", "sap_reason"):
            if mk not in x.mapping:
                errs.append(f"{x.id}: mapping.{mk} missing")
    return errs


_lock = threading.Lock()
_cache: dict = {}


def load(path: Path | None = None) -> Taxonomy:
    p = Path(path or config.TAXONOMY_PATH)
    mtime = p.stat().st_mtime
    with _lock:
        if _cache.get("key") != (str(p), mtime):
            _cache["tax"] = parse(p.read_text(encoding="utf-8"), str(p))
            _cache["key"] = (str(p), mtime)
        return _cache["tax"]


def sync_to_db(tax: Taxonomy) -> int:
    """Publish this version to ccai.taxonomy (idempotent per version)."""
    from . import db

    n = 0
    for x in tax.leaves:
        n += db.ex("""insert into ccai.taxonomy (leaf_id, version, l1, l1_name, l2, l2_name, name, definition, include,
                      exclude, examples, keywords, mapping, churn_risk, digest)
                      values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      on conflict (leaf_id, version) do update set name = excluded.name, definition = excluded.definition,
                      include = excluded.include, exclude = excluded.exclude, examples = excluded.examples,
                      keywords = excluded.keywords, mapping = excluded.mapping, churn_risk = excluded.churn_risk,
                      digest = excluded.digest, loaded_at = now()""",
                   (x.id, tax.version, x.l1, x.l1_name, x.l2, x.l2_name, x.name, x.definition, x.include, x.exclude,
                    db.J(x.examples), db.J(x.keywords), db.J(x.mapping), x.churn_risk, tax.digest))
    return n
