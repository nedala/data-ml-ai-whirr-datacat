"""Notebook pickers for Google Cloud identities and projects.

Studio → **Cloud identities** holds the uploaded entities and assigns one per
consumer (``build/studio/gcp_consumers.yaml``). Studio then injects the
*assigned* identity's key — and its projects — into each consumer's process
environment: ``GCP_BQ_JSON`` (BigQuery · read), ``GCP_BQ_WRITE_JSON``
(BigQuery · write), ``GCP_TRINO_JSON`` (Trino · BigQuery catalog), …

A notebook has no Studio session, so it cannot mint somebody else's key. The
picker therefore offers exactly the identities this process already holds a
key for, labelled with the consumer that carries it, plus each identity's data
projects. Defaults are always valid, so *Run all* works without touching the
widgets.

    from bsp_common import pick

    sel = pick.gcp_picker()               # every identity available here
    sel.display()                         # two dropdowns + who you act as
    sel.identity, sel.project, sel.purpose
    creds = sel.credentials()             # google-auth Credentials (or None)

Trino (``pick.gcp_picker(catalogs=CATALOGS)``) filters the projects to those
that actually have a ``bq_<project>`` catalog, and ``sel.catalog`` is the
catalog to put in front of a table name.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from . import gcp

#: How a consumer is described in the dropdown.
LABELS = {
    "bigquery_read": "BigQuery · read",
    "bigquery_write": "BigQuery · write",
    "trino_bigquery": "Trino · BigQuery",
    "vertex": "Vertex AI",
    "gcs": "Cloud Storage",
    "mlflow_artifacts": "MLflow artifacts",
    "artifact_registry": "Artifact Registry",
    "cloud_run": "Cloud Run",
    "workspace": "Workspace tools",
}

#: When one identity carries several keys (BigQuery · read and Trino · BigQuery
#: are usually the same person), show it once, under the first purpose here.
PURPOSE_PRIORITY = [
    "bigquery_read", "trino_bigquery", "bigquery_write",
    "vertex", "gcs", "mlflow_artifacts", "artifact_registry", "cloud_run", "workspace",
]


def bq_catalog(project: str) -> str:
    """Trino's catalog name for a data project — mirrors build/trino/entrypoint.sh
    (``da-tsa-dv`` → ``bq_tsa_dv``; ``TRINO_BQ_STRIP_PREFIX`` overrides ``da-``)."""
    slug = (project or "").strip().lower()
    prefix = os.environ.get("TRINO_BQ_STRIP_PREFIX", "da-")
    if prefix and slug.startswith(prefix.lower()):
        slug = slug[len(prefix):]
    return "bq_" + "".join(c if c.isalnum() else "_" for c in slug)


def bq_catalogs(catalogs: Iterable[str], projects: Iterable[str] | None = None) -> dict[str, str]:
    """{data project: catalog} for the bq_* catalogs Trino actually serves.
    With one generic ``bigquery`` catalog (no data-project list), returns
    ``{project: "bigquery"}``."""
    have = list(catalogs or [])
    if "bigquery" in have:
        return {(p or ""): "bigquery" for p in (projects or [""]) if p}
    out: dict[str, str] = {}
    for p in projects or []:
        cat = bq_catalog(p)
        if cat in have:
            out[p] = cat
    return out


def choices(purposes: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """The identities whose key is in this process, one entry per identity.

    Each entry: ``{identity, purpose, label, type, principal, project,
    exec_project, data_projects, env}``.
    """
    wanted = [p for p in PURPOSE_PRIORITY if purposes is None or p in purposes]
    by_identity: dict[str, dict[str, Any]] = {}
    for purpose in wanted:
        spec = gcp.CONSUMERS.get(purpose)
        if not spec or not os.environ.get(spec[0], "").strip():
            continue
        name = gcp.assigned_name(purpose) or "(legacy GCP_SA_JSON)"
        key = name.casefold()
        if key in by_identity:
            # same identity, another consumer: keep the higher-priority purpose
            # but remember every project it can see
            seen = by_identity[key]
            for p in gcp.data_projects(purpose):
                if p not in seen["data_projects"]:
                    seen["data_projects"].append(p)
            continue
        info = gcp.info(purpose) or {}
        meta = gcp.identity_meta(name)
        data = list(gcp.data_projects(purpose))
        by_identity[key] = {
            "identity": name,
            "purpose": purpose,
            "label": f"{name} · {LABELS.get(purpose, purpose)}",
            "type": info.get("type") or meta.get("type") or "",
            "principal": meta.get("principal") or info.get("client_email") or info.get("client_id") or "",
            "project": gcp.project(purpose),
            "exec_project": gcp.exec_project(purpose),
            "data_projects": data,
            "env": spec[0],
        }
    return list(by_identity.values())


class Picker:
    """Two dropdowns (identity, project) over :func:`choices`.

    Attributes are read live, so a cell executed after the user changes a
    dropdown sees the new value:

        sel.identity   uploaded entity name
        sel.purpose    consumer carrying the key (bigquery_read, …)
        sel.principal  who the identity acts as
        sel.project    selected data project
        sel.catalog    Trino catalog for it ("" when none)
    """

    def __init__(self, purposes=None, project: str | None = None, catalogs=None,
                 identity: str | None = None, title: str = ""):
        self.title = title
        self.catalogs = list(catalogs) if catalogs is not None else None
        self._choices = choices(purposes)
        if not self._choices:
            raise RuntimeError(
                "no Google Cloud identity is configured for this notebook — assign one in "
                "Studio → Cloud identities, then restart the capability from Start & stop")
        self._by_label = {c["label"]: c for c in self._choices}
        # default identity: the caller's hint, else the first (BigQuery · read)
        default = 0
        if identity:
            for i, c in enumerate(self._choices):
                if c["identity"].casefold() == identity.casefold():
                    default = i
                    break
        self.identity_w = self._dropdown(
            "Identity", [c["label"] for c in self._choices], self._choices[default]["label"])
        self.project_w = self._dropdown("Project", [], "")
        self._info = self._html("")
        self.identity_w.observe(lambda _c: self._sync(), names="value")
        self.project_w.observe(lambda _c: self._sync_info(), names="value")
        self._sync(project)

    # ── widgets ──────────────────────────────────────────────────────────
    @staticmethod
    def _dropdown(description, options, value):
        try:
            import ipywidgets as w
        except ImportError:  # headless: plain attributes below still work
            return _Plain(options[0] if options else value)
        return w.Dropdown(options=list(options), value=value or None, description=description,
                          layout=w.Layout(width="440px"),
                          style={"description_width": "72px"})

    @staticmethod
    def _html(value):
        try:
            import ipywidgets as w
        except ImportError:
            return _Plain(value)
        return w.HTML(value)

    def _sync(self, project: str | None = None) -> None:
        c = self._by_label[self.identity_w.value]
        # the identity's own (home) project first, then every data project
        projects = [c["project"]] + [p for p in c["data_projects"] if p and p != c["project"]]
        projects = [p for p in projects if p] or [c["project"]]
        if self.catalogs is not None:
            mapped = bq_catalogs(self.catalogs, projects)
            projects = [p for p in projects if p in mapped] or []
        if projects:
            options = [p for p in projects if p]
            if not options:               # nothing has a catalog: show the default anyway
                options = [c["project"]]
            self.project_w.options = options
            if project in options:
                self.project_w.value = project
            elif self.project_w.value not in options:
                self.project_w.value = options[0]
        else:
            self.project_w.options = [c["project"]]
        self._sync_info()

    def _sync_info(self) -> None:
        c = self._by_label[self.identity_w.value]
        bits = [f"<b>{c['principal'] or c['identity']}</b>"]
        if c["type"]:
            bits.append(c["type"].replace("_", " "))
        bits.append(f"project <b>{self.project}</b>")
        if c["exec_project"] and c["exec_project"] != self.project:
            bits.append(f"jobs bill to {c['exec_project']}")
        if self.catalogs is not None:
            cat = self.catalog
            bits.append(f"catalog <b>{cat}</b>" if cat else "<i>no Trino catalog for this project</i>")
        self._info.value = (
            "<div style='font:13px/1.5 system-ui;color:#3d5258;margin:2px 0 6px'>"
            + " · ".join(bits) + "</div>")

    # ── live values ──────────────────────────────────────────────────────
    @property
    def _choice(self) -> dict[str, Any]:
        return self._by_label[self.identity_w.value]

    @property
    def identity(self) -> str:
        return self._choice["identity"]

    @property
    def purpose(self) -> str:
        return self._choice["purpose"]

    @property
    def principal(self) -> str:
        return self._choice["principal"]

    @property
    def project(self) -> str:
        return self.project_w.value

    @property
    def exec_project(self) -> str:
        return self._choice["exec_project"]

    @property
    def catalog(self) -> str:
        if self.catalogs is None:
            return bq_catalog(self.project)
        return bq_catalogs(self.catalogs, [self.project]).get(self.project, "")

    def credentials(self):
        """google-auth Credentials for the selected identity (None when unset)."""
        return gcp.credentials(self.purpose)

    # ── rendering ────────────────────────────────────────────────────────
    def display(self) -> None:
        try:
            import ipywidgets as w
            from IPython.display import display
        except ImportError:
            print(f"identity: {self.identity} ({self.principal}) · project: {self.project}")
            return
        head = self._html(
            f"<div style='font:600 13px/1.4 system-ui;color:#0b6e79;margin-bottom:4px'>"
            f"{self.title or 'Google Cloud identity'}</div>")
        display(w.VBox([head, self.identity_w, self.project_w, self._info]))


class _Plain:
    """ipywidgets is not installed (e.g. plain python): behave like a value."""

    def __init__(self, value):
        self.value = value
        self.options = [value] if value is not None else []

    def observe(self, *_a, **_k):
        return None


def gcp_picker(purposes: Iterable[str] | None = None, project: str | None = None, catalogs=None,
               identity: str | None = None, title: str = "") -> Picker:
    """Build a :class:`Picker`; see the module docstring."""
    return Picker(purposes=purposes, project=project, catalogs=catalogs,
                  identity=identity, title=title)
