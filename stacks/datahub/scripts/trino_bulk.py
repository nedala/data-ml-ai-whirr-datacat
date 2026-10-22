#!/usr/bin/env python3
"""Bulk schema ingestion of big Trino catalogs (the BigQuery catalogs bq_*)
into DataHub — run by ingest.sh inside the ingestion image.

DataHub's trino source asks Trino three metadata questions per table; on a
BigQuery catalog each one is a BigQuery API round trip (seconds), so a few
hundred tables take hours. This reads the same metadata in a few bulk
queries per catalog (information_schema.columns + table comments, filtered
to the schemas trino_scopes.yml allows) and emits the same entities the
trino source would: datasets ``urn:li:dataset:(urn:li:dataPlatform:trino,
catalog.schema.table,ENV)`` with properties, schema (column names, types,
comments), subtype and the catalog / schema containers.

METADATA ONLY: no table data is read; profiling and usage stay off.

    python trino_bulk.py <catalog> <trino_scopes.yml>
"""
from __future__ import annotations

import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import trino
import yaml
from datahub.emitter.mce_builder import make_data_platform_urn, make_dataset_urn
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.mcp_builder import add_dataset_to_container, gen_containers
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.ingestion.source.sql.sql_utils import gen_database_key, gen_schema_key
from datahub.metadata.schema_classes import (
    ArrayTypeClass, BooleanTypeClass, BytesTypeClass, DatasetPropertiesClass, DateTypeClass, NumberTypeClass,
    OtherSchemaClass, RecordTypeClass, SchemaFieldClass, SchemaFieldDataTypeClass, SchemaMetadataClass, StatusClass,
    StringTypeClass, SubTypesClass, TimeTypeClass,
)

PLATFORM = "trino"
ENV = os.environ.get("DATAHUB_ENV", "PROD")


def field_type(native: str) -> SchemaFieldDataTypeClass:
    t = native.lower()
    if t.startswith(("varchar", "char", "json", "uuid")):
        cls = StringTypeClass
    elif t.startswith(("bigint", "integer", "smallint", "tinyint", "double", "real", "decimal")):
        cls = NumberTypeClass
    elif t.startswith("boolean"):
        cls = BooleanTypeClass
    elif t.startswith("date"):
        cls = DateTypeClass
    elif t.startswith(("timestamp", "time")):
        cls = TimeTypeClass
    elif t.startswith("array"):
        cls = ArrayTypeClass
    elif t.startswith(("row", "map")):
        cls = RecordTypeClass
    elif t.startswith("varbinary"):
        cls = BytesTypeClass
    else:
        cls = StringTypeClass
    return SchemaFieldDataTypeClass(type=cls())


def allowed(schema: str, scope: dict) -> bool:
    if schema == "information_schema":
        return False
    allow = scope.get("allow") or [".*"]
    deny = scope.get("deny") or []
    return any(re.search(p, schema, re.I) for p in allow) and not any(re.search(p, schema, re.I) for p in deny)


def rows(cur, sql: str) -> list[tuple]:
    cur.execute(sql)
    return cur.fetchall()


def main() -> int:
    cat, scopes = sys.argv[1], yaml.safe_load(open(sys.argv[2]))["catalogs"]
    scope = scopes.get(cat) or {}
    conn = trino.dbapi.connect(host=os.environ.get("TRINO_HOST", "trino"), port=int(os.environ.get("TRINO_PORT", "8080")),
                               user=os.environ.get("TRINO_USER", "datahub"), catalog=cat, http_scheme="http",
                               request_timeout=1800)
    cur = conn.cursor()
    t0 = time.time()
    schemas = [r[0] for r in rows(cur, f'SELECT schema_name FROM "{cat}".information_schema.schemata') if allowed(r[0], scope)]
    print(f"[bulk] {cat}: {len(schemas)} schema(s) allowed", flush=True)
    if not schemas:
        return 0
    in_list = ", ".join("'" + s.replace("'", "''") + "'" for s in schemas)

    def schema_columns(sch: str) -> list[tuple]:
        # one query per schema, a few in parallel: on BigQuery each table's columns cost an API round
        # trip, so one catalog-wide query can outlive query.max-execution-time
        c = trino.dbapi.connect(host=conn.host, port=conn.port, user=conn.user, catalog=cat, http_scheme="http",
                                request_timeout=1800).cursor()
        lit = sch.replace("'", "''")
        t1 = time.time()
        try:
            out = rows(c, f'SELECT table_schema, table_name, column_name, data_type, ordinal_position, comment '
                          f"FROM \"{cat}\".information_schema.columns WHERE table_schema = '{lit}'")
        except Exception as e:  # noqa: BLE001 — one slow / broken dataset must not sink the catalog
            print(f"[bulk] {cat}.{sch}: skipped ({str(e)[:140]})", flush=True)
            skipped.append(sch)
            return []
        print(f"[bulk] {cat}.{sch}: {len({r[1] for r in out})} table(s) in {time.time() - t1:.0f}s", flush=True)
        return out

    cols: list[tuple] = []
    skipped: list[str] = []
    with ThreadPoolExecutor(max_workers=int(os.environ.get("TRINO_BULK_PARALLEL", "3"))) as pool:
        for i, part in enumerate(pool.map(schema_columns, schemas), 1):
            cols += part
            if i % 10 == 0:
                print(f"[bulk] {cat}: columns of {i}/{len(schemas)} schema(s) read", flush=True)
    comments: dict = {}
    # BigQuery table descriptions via system.metadata.table_comments are slow and
    # often fail (regional INFORMATION_SCHEMA); opt in per catalog
    try:
        if not scope.get("table_comments", not cat.startswith("bq_")):
            raise RuntimeError("skipped (table_comments: false)")
        comments = {(s, t): c for s, t, c in rows(cur, "SELECT schema_name, table_name, comment FROM system.metadata.table_comments "
                                                        f"WHERE catalog_name = '{cat}' AND schema_name IN ({in_list})") if c}
    except Exception as e:  # noqa: BLE001 — descriptions are optional
        print(f"[bulk] {cat}: table comments unavailable ({str(e)[:120]})", flush=True)
        comments = {}
    tables: dict[tuple[str, str], list[tuple]] = {}
    for s, t, c, dt, pos, cm in cols:
        tables.setdefault((s, t), []).append((pos, c, dt, cm))
    print(f"[bulk] {cat}: {len(tables)} table(s), {len(cols)} column(s), {len(comments)} table comment(s) "
          f"read in {time.time() - t0:.0f}s", flush=True)

    emitter = DatahubRestEmitter(os.environ["DATAHUB_GMS_URL"],
                                 extra_headers={"Authorization": f"Basic __datahub_system:{os.environ['DATAHUB_SYSTEM_CLIENT_SECRET']}"})
    db_key = gen_database_key(cat, PLATFORM, None, ENV)
    for wu in gen_containers(container_key=db_key, name=cat, sub_types=["Database"]):
        emitter.emit(wu.metadata)
    for s in sorted({s for s, _ in tables}):
        for wu in gen_containers(container_key=gen_schema_key(cat, s, PLATFORM, None, ENV), name=s, sub_types=["Schema"],
                                 parent_container_key=db_key):
            emitter.emit(wu.metadata)
    n = 0
    for (s, t), fields in sorted(tables.items()):
        urn = make_dataset_urn(PLATFORM, f"{cat}.{s}.{t}", ENV)
        schema = SchemaMetadataClass(
            schemaName=f"{cat}.{s}.{t}", platform=make_data_platform_urn(PLATFORM), version=0, hash="",
            platformSchema=OtherSchemaClass(rawSchema=""),
            fields=[SchemaFieldClass(fieldPath=c, nativeDataType=dt, type=field_type(dt), description=cm or None,
                                     nullable=True) for _, c, dt, cm in sorted(fields)])
        props = DatasetPropertiesClass(name=t, qualifiedName=f"{cat}.{s}.{t}", description=comments.get((s, t)),
                                       customProperties={"trino_catalog": cat, "ingested_by": "trino_bulk"})
        for aspect in (props, schema, SubTypesClass(typeNames=["Table"]), StatusClass(removed=False)):
            emitter.emit(MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect))
        for wu in add_dataset_to_container(gen_schema_key(cat, s, PLATFORM, None, ENV), urn):
            emitter.emit(wu.metadata)
        n += 1
        if n % 100 == 0:
            print(f"[bulk] {cat}: {n}/{len(tables)} emitted", flush=True)
    print(f"[bulk] {cat}: {n} dataset(s) emitted in {time.time() - t0:.0f}s"
          + (f"; skipped dataset(s): {', '.join(skipped)}" if skipped else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
