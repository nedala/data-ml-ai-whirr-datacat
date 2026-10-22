"""A small, honest feature store (plus an optional Feast mirror).

Why not Feast as the system of record? Feast resolves in the shared env, but
its Postgres offline store is a contrib/alpha plugin, its point-in-time join
runs through Dask on the file store, and its registry adds another moving
part to a live demo. The contract we need is small, so it is implemented
here directly on the warehouse — and the very same YAML definitions are
mirrored into a real Feast repo (`feast_sync()`), so a team that standardises
on Feast loses nothing.

  definitions   feature_repo/features.yaml (feature views, versions, TTLs)
  registry      features.feature_registry (definition, hash, materialisation stats)
                + a copy of the YAML under ${DATA_ROOT}/mlops/feature_repo
  offline       features.fv_<view>_v<version>  (ban, event_ts, features…)
  training set  get_historical_features(): point-in-time join — for each
                (ban, event_timestamp) the latest row with
                event_ts <= event_timestamp and within the view's TTL
  online        features.online_features (ban → jsonb of every feature,
                as of the latest materialisation) — read by the model gateway
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from . import db
from .config import ENTITY, FEATURE_REPO_DIR, TS

PKG_DEFS = Path(__file__).resolve().parent / "feature_repo" / "features.yaml"
REGISTRY_DDL = """
create schema if not exists features;
create table if not exists features.feature_registry (
    feature_view   text not null,
    version        integer not null,
    def_hash       text not null,
    definition     jsonb not null,
    source         text,
    offline_table  text,
    rows           bigint,
    first_event    date,
    last_event     date,
    materialized_at timestamptz,
    online_rows    bigint,
    online_materialized_at timestamptz,
    registered_at  timestamptz not null default now(),
    primary key (feature_view, version)
);
create table if not exists features.online_features (
    ban      text primary key,
    event_ts        date,
    features        jsonb not null,
    view_versions   jsonb,
    materialized_at timestamptz not null default now()
);
"""


def definitions_path() -> Path:
    """Working copy under /data/mlops/feature_repo (seeded from the package).

    The working copy follows the packaged definitions while nobody has edited
    it (the sha256 it was seeded from is kept next to it); an edited copy is
    kept — unless its entity key no longer matches the warehouse key
    (config.ENTITY), in which case it is backed up and re-seeded."""
    p = FEATURE_REPO_DIR / "features.yaml"
    seed = p.with_suffix(".yaml.seeded-sha256")
    try:
        pkg = PKG_DEFS.read_bytes()
        pkg_sha = hashlib.sha256(pkg).hexdigest()
        if p.exists():
            cur = p.read_bytes()
            cur_sha = hashlib.sha256(cur).hexdigest()
            untouched = seed.exists() and seed.read_text().strip() == cur_sha
            try:
                stale_key = (yaml.safe_load(cur) or {}).get("entity", {}).get("join_key") != ENTITY
            except Exception:
                stale_key = True
            if cur_sha != pkg_sha and (untouched or stale_key):
                if not untouched:
                    p.with_suffix(f".yaml.bak-{time.strftime('%Y%m%d%H%M%S')}").write_bytes(cur)
                p.unlink()
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(pkg)
            seed.write_text(pkg_sha)
        return p
    except OSError:
        return PKG_DEFS


def load_definitions(path: str | Path | None = None) -> dict:
    return yaml.safe_load(Path(path or definitions_path()).read_text())


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


def offline_table(view: dict) -> str:
    return f"features.fv_{view['name']}_v{view['version']}"


def all_features(defs: dict | None = None) -> list[str]:
    defs = defs or load_definitions()
    return [f["name"] for v in defs["feature_views"] for f in v["features"]]


def view_of(feature: str, defs: dict | None = None) -> dict:
    defs = defs or load_definitions()
    for v in defs["feature_views"]:
        if feature in [f["name"] for f in v["features"]]:
            return v
    raise KeyError(feature)


def apply(defs: dict | None = None, verbose: bool = True) -> list[dict]:
    """Register views (idempotent) and materialise the offline store."""
    from .data import ensure_base

    ensure_base()
    defs = defs or load_definitions()
    db.execute(REGISTRY_DDL)
    src, ts = defs["source"]["table"], defs["source"]["timestamp"]
    out = []
    for v in defs["feature_views"]:
        t0 = time.time()
        tbl = offline_table(v)
        cols = ", ".join(f["name"] for f in v["features"])
        db.execute(f"drop table if exists {tbl}")
        db.execute(f"create table {tbl} as select {ENTITY}, {ts}::date as event_ts, {cols}, now() as created_at "
                   f"from {src}")
        db.execute(f"create index on {tbl} ({ENTITY}, event_ts)")
        st = db.fetchone(f"select count(*) n, min(event_ts) a, max(event_ts) b from {tbl}")
        db.execute("""insert into features.feature_registry
            (feature_view, version, def_hash, definition, source, offline_table, rows, first_event, last_event, materialized_at)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
            on conflict (feature_view, version) do update set def_hash=excluded.def_hash, definition=excluded.definition,
              source=excluded.source, offline_table=excluded.offline_table, rows=excluded.rows,
              first_event=excluded.first_event, last_event=excluded.last_event, materialized_at=now()""",
                   (v["name"], v["version"], _hash(v), json.dumps(v), src, tbl, st["n"], st["a"], st["b"]))
        out.append({"feature_view": v["name"], "version": v["version"], "table": tbl, "rows": st["n"],
                    "features": len(v["features"]), "seconds": round(time.time() - t0, 2)})
        if verbose:
            print(f"  · {v['name']}_v{v['version']}: {st['n']:,} rows, {len(v['features'])} features → {tbl}")
    return out


def _view_frame(v: dict) -> pd.DataFrame:
    df = db.query_df(f"select * from {offline_table(v)}")
    df["event_ts"] = pd.to_datetime(df["event_ts"])
    return df.drop(columns=["created_at"], errors="ignore")


def get_historical_features(entity_df: pd.DataFrame, features: list[str], timestamp_col: str = TS,
                            defs: dict | None = None) -> pd.DataFrame:
    """Point-in-time-correct join: each entity row gets, per feature view, the
    latest feature row with event_ts <= the entity timestamp (within TTL)."""
    from .data import coerce

    defs = defs or load_definitions()
    ent = entity_df.copy()
    ent[timestamp_col] = pd.to_datetime(ent[timestamp_col])
    ent["_row"] = np.arange(len(ent))
    out = ent.sort_values(timestamp_col)
    by_view: dict[str, list[str]] = {}
    for f in features:
        v = view_of(f, defs)
        by_view.setdefault(v["name"], []).append(f)
    for vname, feats in by_view.items():
        v = next(x for x in defs["feature_views"] if x["name"] == vname)
        fv = _view_frame(v)[[ENTITY, "event_ts"] + feats].sort_values("event_ts")
        fv = fv.rename(columns={"event_ts": f"_ts_{vname}"})
        with warnings.catch_warnings():     # numpy ≥ 2.5 warns inside pandas' merge_asof tolerance check
            warnings.simplefilter("ignore", DeprecationWarning)
            out = pd.merge_asof(out, fv, left_on=timestamp_col, right_on=f"_ts_{vname}", by=ENTITY,
                                direction="backward", tolerance=pd.Timedelta(days=int(v.get("ttl_days", 400))))
        out = out.drop(columns=[f"_ts_{vname}"])
    out = out.sort_values("_row").drop(columns=["_row"]).reset_index(drop=True)
    return coerce(out)


def materialize_online(as_of=None, defs: dict | None = None, verbose: bool = True) -> dict:
    """Latest value of every feature per account (as of `as_of`) → online store."""
    defs = defs or load_definitions()
    db.execute(REGISTRY_DDL)
    t0 = time.time()
    as_of = pd.Timestamp(as_of) if as_of is not None else None
    merged = None
    for v in defs["feature_views"]:
        fv = _view_frame(v)
        if as_of is not None:
            fv = fv[fv.event_ts <= as_of]
        fv = fv.sort_values("event_ts").groupby(ENTITY).tail(1)
        fv = fv.rename(columns={"event_ts": f"_ts_{v['name']}"})
        merged = fv if merged is None else merged.merge(fv, on=ENTITY, how="outer")
    ts_cols = [c for c in merged.columns if c.startswith("_ts_")]
    merged["event_ts"] = merged[ts_cols].max(axis=1)
    feats = all_features(defs)
    versions = json.dumps({v["name"]: v["version"] for v in defs["feature_views"]})
    rows = []
    for rec in merged[[ENTITY, "event_ts"] + feats].to_dict("records"):
        f = {k: (None if (isinstance(x, float) and np.isnan(x)) else (x.item() if hasattr(x, "item") else x))
             for k, x in rec.items() if k in feats}
        f = {k: (int(v) if isinstance(v, bool) else v) for k, v in f.items()}   # same encoding as training
        rows.append((rec[ENTITY], rec["event_ts"].date() if pd.notna(rec["event_ts"]) else None, json.dumps(f), versions))
    import psycopg2.extras

    with db.connect() as c, c.cursor() as cur:
        cur.execute("truncate features.online_features")
        psycopg2.extras.execute_values(cur, "insert into features.online_features (ban, event_ts, features, view_versions) "
                                       "values %s", rows, page_size=2000)
        for v in defs["feature_views"]:
            cur.execute("update features.feature_registry set online_rows=%s, online_materialized_at=now() "
                        "where feature_view=%s and version=%s", (len(rows), v["name"], v["version"]))
    res = {"accounts": len(rows), "as_of": str(merged["event_ts"].max().date()), "seconds": round(time.time() - t0, 2)}
    if verbose:
        print(f"  · online store: {res['accounts']:,} accounts as of {res['as_of']} ({res['seconds']}s)")
    return res


def get_online_features(bans: list[str]) -> dict[str, dict]:
    if not bans:
        return {}
    rows = db.fetchall("select ban, event_ts, features from features.online_features where ban = any(%s)",
                       (list(bans),))
    return {r["ban"]: {**r["features"], "_event_ts": str(r["event_ts"])} for r in rows}


def freshness() -> list[dict]:
    db.execute(REGISTRY_DDL)
    rows = db.fetchall("""select feature_view, version, def_hash, offline_table, rows, first_event, last_event,
                                 materialized_at, online_rows, online_materialized_at,
                                 jsonb_array_length(definition->'features') n_features, definition->>'description' description,
                                 (definition->>'ttl_days')::int ttl_days, definition->>'owner' owner,
                                 definition->'features' features
                          from features.feature_registry order by feature_view, version""")
    now = datetime.now(timezone.utc)
    for r in rows:
        m = r.get("materialized_at")
        r["offline_age_min"] = round((now - m).total_seconds() / 60, 1) if m else None
        o = r.get("online_materialized_at")
        r["online_age_min"] = round((now - o).total_seconds() / 60, 1) if o else None
    return rows


# ── Feast mirror (optional) ───────────────────────────────────────────────
_FEAST_TYPES = {"string": "String", "int": "Int64", "float": "Float64"}


def feast_sync(defs: dict | None = None, verbose: bool = True, materialize: bool = True) -> dict:
    """Export the views to a Feast repo (file offline store, sqlite online
    store), `apply` them and materialise; returns a status dict. Never raises."""
    try:
        import feast  # noqa: F401
        from feast import Entity, FeatureStore, FeatureView, Field, FileSource, ValueType
        from feast import types as ft
    except Exception as e:
        return {"status": "skipped", "reason": f"feast not installed ({e.__class__.__name__})"}
    t0 = time.time()
    try:
        defs = defs or load_definitions()
        repo = FEATURE_REPO_DIR / "feast"
        data = repo / "data"
        # the Feast repo is a derived mirror of features.yaml: rebuild it from scratch so views
        # that left the definitions (old versions, an old entity key) do not linger in its registry
        shutil.rmtree(data, ignore_errors=True)
        data.mkdir(parents=True, exist_ok=True)
        (repo / "feature_store.yaml").write_text(yaml.safe_dump({
            "project": defs["project"], "provider": "local", "registry": str(data / "registry.db"),
            "online_store": {"type": "sqlite", "path": str(data / "online.db")},
            "offline_store": {"type": "file"}, "entity_key_serialization_version": 3}))
        cust = Entity(name=defs["entity"]["name"], join_keys=[ENTITY], value_type=ValueType.STRING,
                      description=defs["entity"].get("description", ""))
        objs: list = [cust]
        lines = ["# Generated by bsp_mlops.featurestore.feast_sync() from features.yaml — do not edit.",
                 "from datetime import timedelta", "from feast import Entity, FeatureView, Field, FileSource, ValueType",
                 "from feast.types import Float64, Int64, String", "",
                 f"customer = Entity(name='{defs['entity']['name']}', join_keys=['{ENTITY}'], value_type=ValueType.STRING)"]
        from datetime import timedelta

        for v in defs["feature_views"]:
            fv = _view_frame(v)
            fv["event_ts"] = pd.to_datetime(fv["event_ts"]).dt.tz_localize("UTC")
            for f in v["features"]:
                if f["dtype"] == "int":
                    fv[f["name"]] = pd.to_numeric(fv[f["name"]], errors="coerce").fillna(0).astype("int64")
                elif f["dtype"] == "float":
                    fv[f["name"]] = pd.to_numeric(fv[f["name"]], errors="coerce").astype("float64")
                else:
                    fv[f["name"]] = fv[f["name"]].astype(str)
            pq = data / f"{v['name']}_v{v['version']}.parquet"
            fv.to_parquet(pq, index=False)
            src = FileSource(name=f"{v['name']}_v{v['version']}_src", path=str(pq), timestamp_field="event_ts")
            schema = [Field(name=f["name"], dtype=getattr(ft, _FEAST_TYPES[f["dtype"]])) for f in v["features"]]
            objs.append(FeatureView(name=f"{v['name']}_v{v['version']}", entities=[cust], schema=schema, source=src,
                                    ttl=timedelta(days=int(v.get("ttl_days", 400))), online=True,
                                    description=v.get("description", "")))
            fields = ", ".join(f"Field(name='{f['name']}', dtype={_FEAST_TYPES[f['dtype']]})" for f in v["features"])
            lines += ["", f"{v['name']}_v{v['version']} = FeatureView(",
                      f"    name='{v['name']}_v{v['version']}', entities=[customer], ttl=timedelta(days={v.get('ttl_days', 400)}),",
                      f"    schema=[{fields}],",
                      f"    source=FileSource(path='{pq}', timestamp_field='event_ts'))"]
        (repo / "definitions.py").write_text("\n".join(lines) + "\n")
        store = FeatureStore(repo_path=str(repo))
        store.apply(objs)
        res = {"status": "applied", "repo": str(repo), "feature_views": len(objs) - 1}
        if materialize:
            end = datetime.now(timezone.utc)
            store.materialize(start_date=datetime(2015, 1, 1, tzinfo=timezone.utc), end_date=end)
            res["materialized"] = True
        res["seconds"] = round(time.time() - t0, 1)
        if verbose:
            print(f"  · Feast mirror: {res['feature_views']} views applied + materialised at {repo} ({res['seconds']}s)")
        return res
    except Exception as e:  # mirror is optional — report, never break the pipeline
        return {"status": "error", "reason": f"{e.__class__.__name__}: {e}"[:500]}


def feast_online(bans: list[str], features: list[str] | None = None) -> dict:
    """Read the same online features back through Feast (parity check)."""
    from feast import FeatureStore

    defs = load_definitions()
    refs = [f"{view_of(f, defs)['name']}_v{view_of(f, defs)['version']}:{f}" for f in (features or all_features(defs))]
    store = FeatureStore(repo_path=str(FEATURE_REPO_DIR / "feast"))
    return store.get_online_features(features=refs, entity_rows=[{ENTITY: a} for a in bans]).to_dict()
