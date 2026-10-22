"""Spark (local mode) for notebooks and ETL scripts in the workspace image.

One SparkSession per Python process: the first call starts the JVM, every later
call returns the SAME session (a singleton), so notebook cells and helper
functions never fight over drivers, ports or memory.

    from bsp_common import spark as bsp_spark
    spark = bsp_spark.get_spark("late-balance-etl")     # starts (or reuses) the session
    df = spark.read.format("jdbc").options(**bsp_spark.jdbc_options("marts.fct_invoice")).load()
    bsp_spark.stop_spark()                              # frees the JVM; get_spark() starts a new one

    get_spark(app_name="bsp", **conf)  the cached session, else builder.getOrCreate()
    jdbc_options(schema_table, **extra) Spark JDBC options for the warehouse (bsp_edw);
                                        its repr() masks the password
    jdbc_url()                          jdbc:postgresql://host:port/db (no credentials)
    stop_spark()                        stop the session (no-op when none is running)
    lake_path(*parts)                   a path under the shared lake (LAKE_DIR, /data/lake)

Image facts (Dockerfile, build/image/spark/): JRE 21 at JAVA_HOME, defaults in
SPARK_CONF_DIR=/opt/spark-conf/spark-defaults.conf (UTC session time zone, no
console progress bar, 8 shuffle partitions, dynamic partition overwrite), the
PostgreSQL JDBC driver at /opt/spark-jars/postgresql.jar (baked in: runtime
egress is allowlisted, so nothing is downloaded). Driver memory: env
SPARK_DRIVER_MEMORY (default 2g); local cores: env SPARK_LOCAL_CORES (default
min(4, CPUs)). Warehouse connection: PG_HOST, PG_PORT, PG_DB, PG_USER and the
secret PG_PASS from the process environment (see settings.py)."""

from __future__ import annotations

import os
import threading
from typing import Any

from .settings import env

JDBC_DRIVER = "org.postgresql.Driver"
DEFAULT_JAR = "postgresql.jar"

_SPARK: Any = None            # the module-level cached session (the singleton)
_LOCK = threading.Lock()


def _jdbc_jar() -> str:
    path = os.path.join(env("SPARK_JARS_DIR", "/opt/spark-jars"), DEFAULT_JAR)
    return path if os.path.exists(path) else ""


def _local_cores() -> int:
    try:
        return max(1, int(env("SPARK_LOCAL_CORES")))
    except ValueError:                       # unset or not a number
        return min(4, os.cpu_count() or 1)


def _alive(session: Any) -> bool:
    try:
        return session is not None and session.sparkContext._jsc is not None
    except Exception:  # noqa: BLE001
        return False


def get_spark(app_name: str = "bsp", **conf: Any):
    """The one SparkSession of this process.

    The first call builds it (local[N] master, driver memory from
    SPARK_DRIVER_MEMORY, the JDBC driver on spark.jars, any extra ``conf``
    such as ``**{"spark.sql.shuffle.partitions": "4"}``); later calls return
    the same object. ``app_name`` and ``conf`` only apply when a new session
    is started — stop_spark() first to change them."""
    global _SPARK
    with _LOCK:
        if _alive(_SPARK):
            return _SPARK
        from pyspark.sql import SparkSession

        builder = (SparkSession.builder
                   .appName(app_name)
                   .master(env("SPARK_MASTER", f"local[{_local_cores()}]"))
                   .config("spark.driver.memory", env("SPARK_DRIVER_MEMORY", "2g"))
                   .config("spark.driver.bindAddress", env("SPARK_LOCAL_IP", "127.0.0.1"))
                   .config("spark.ui.showConsoleProgress", "false")
                   .config("spark.sql.session.timeZone", "UTC"))
        jar = _jdbc_jar()
        if jar:
            builder = builder.config("spark.jars", jar)
        for key, value in conf.items():
            builder = builder.config(key, str(value))
        _SPARK = builder.getOrCreate()
        return _SPARK


def stop_spark() -> None:
    """Stop the session and the JVM-side context; the next get_spark() starts fresh."""
    global _SPARK
    with _LOCK:
        session, _SPARK = _SPARK, None
        if session is None:
            from pyspark.sql import SparkSession

            session = SparkSession.getActiveSession()
        if session is not None:
            session.stop()


def jdbc_url() -> str:
    return f"jdbc:postgresql://{env('PG_HOST', 'postgres')}:{env('PG_PORT', '5432')}/{env('PG_DB', 'bsp_edw')}"


class JdbcOptions(dict):
    """A plain dict for ``spark.read.options(**opts)`` whose repr hides the password."""

    def __repr__(self) -> str:
        return repr({k: ("***" if k == "password" else v) for k, v in self.items()})

    __str__ = __repr__


def jdbc_options(schema_table: str, **extra: Any) -> JdbcOptions:
    """Spark JDBC options for ``schema.table`` in the warehouse (Postgres bsp_edw).

    ``extra`` adds or overrides options, e.g. ``fetchsize``, ``partitionColumn``
    / ``lowerBound`` / ``upperBound`` / ``numPartitions`` for parallel reads,
    or ``truncate="true"`` for writes. Pass ``schema_table=""`` together with
    ``query="select ..."`` to read a query instead of a table."""
    opts = JdbcOptions(url=jdbc_url(), user=env("PG_USER", "analyst"), password=env("PG_PASS"),
                       driver=JDBC_DRIVER, fetchsize="10000")
    if schema_table:
        opts["dbtable"] = schema_table
    opts.update({k: str(v) for k, v in extra.items()})
    return opts


def lake_path(*parts: str) -> str:
    """A path under the shared lake folder (LAKE_DIR, default /data/lake)."""
    return os.path.join(env("LAKE_DIR", "/data/lake"), *parts)
