"""Strict validation of user-supplied package specs. Anything that does not
match is rejected before it can reach an argv list."""
from __future__ import annotations

import re

MAX_LEN = 200

# PEP 508 project name
PY_NAME = re.compile(r"^(?:[A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])$")
PY_EXTRAS = r"(?:\[\s*[A-Za-z0-9][A-Za-z0-9._-]*(?:\s*,\s*[A-Za-z0-9][A-Za-z0-9._-]*)*\s*\])?"
PY_VERSION_TOKEN = r"[A-Za-z0-9][A-Za-z0-9.*+!_-]*"
PY_CLAUSE = rf"(?:===|==|!=|<=|>=|~=|<|>)\s*{PY_VERSION_TOKEN}"
PY_SPEC = re.compile(
    rf"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?P<extras>{PY_EXTRAS})\s*"
    rf"(?P<spec>{PY_CLAUSE}(?:\s*,\s*{PY_CLAUSE})*)?\s*$"
)
PY_EXACT_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+!_-]*$")

DATASET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")


class SpecError(ValueError):
    pass


def canon(name: str) -> str:
    """PEP 503 normalised name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def py_spec(s: str) -> dict:
    """Validate one requirement like ``polars``, ``pandas[excel]>=2,<3``.
    Returns {name, canon, extras, spec, text}."""
    if not isinstance(s, str):
        raise SpecError("package spec must be a string")
    s = s.strip()
    if not s or len(s) > MAX_LEN:
        raise SpecError("empty or too long package spec")
    if s.startswith("-"):
        raise SpecError(f"options are not allowed: {s!r}")
    m = PY_SPEC.match(s)
    if not m:
        raise SpecError(f"not a valid package requirement: {s!r} "
                        "(use name, name==1.2.3, name>=1,<2 or name[extra])")
    name = m.group("name")
    extras = re.sub(r"\s+", "", m.group("extras") or "")
    spec = re.sub(r"\s+", "", m.group("spec") or "")
    return {"name": name, "canon": canon(name), "extras": extras, "spec": spec,
            "text": f"{name}{extras}{spec}"}


def py_name(s: str) -> str:
    if not isinstance(s, str) or not PY_NAME.match(s.strip()) or len(s) > MAX_LEN:
        raise SpecError(f"not a valid Python package name: {s!r}")
    return s.strip()


def py_version(s: str) -> str:
    if not isinstance(s, str) or not PY_EXACT_VERSION.match(s) or len(s) > 64:
        raise SpecError(f"not a valid version: {s!r}")
    return s


def dataset_id(s: str) -> str:
    if not isinstance(s, str) or not DATASET_ID.match(s) or ".." in s.split("/"):
        raise SpecError(f"not a valid dataset id: {s!r}")
    return s


def parse_requirements(text: str) -> dict:
    """Parse a pasted/uploaded requirements.txt. Option lines (-r, -e,
    --index-url, URLs, paths) are rejected, not followed."""
    accepted, rejected = [], []
    seen = set()
    for raw in (text or "").splitlines()[:2000]:
        line = raw.split(" #", 1)[0].split("\t#", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if ";" in line:
            rejected.append({"line": raw.strip(), "reason": "environment markers are not supported"})
            continue
        try:
            spec = py_spec(line)
        except SpecError as exc:
            rejected.append({"line": raw.strip(), "reason": str(exc)})
            continue
        if spec["canon"] in seen:
            continue
        seen.add(spec["canon"])
        accepted.append(spec["text"])
    return {"accepted": accepted, "rejected": rejected}
