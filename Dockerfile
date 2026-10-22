# syntax=docker/dockerfile:1.7
# ============================================================================
# DataPlatform Data & AI Appliance — workspace image  (bsp/appliance:latest)
# Build context: repo root. ONE image, many roles (plan/architecture.md §1):
# studio, nginx, px-proxy, docs, appliance (JupyterLab + Workbench), code
# (code-server) and every component service (connectors, canvas, mlops,
# harness, mcp, disputes, ccai, rag, superset …). Compose picks the role by
# entrypoint. The Desktop image (build/browser) is built FROM this one.
#
# Multi-arch: linux/amd64 and linux/arm64. Every download is arch-aware.
#
# Build-time egress (optional):
#   docker build --build-arg HTTPS_PROXY=http://host.docker.internal:3128 \
#                --build-arg HTTP_PROXY=http://host.docker.internal:3128 \
#                --secret id=build_ca,src=build/certs/corporate/root.pem -t bsp/appliance:latest .
#   The optional `build_ca` secret is trusted per step by
#   build/image/with-build-trust.sh and NOT baked in — at runtime trust comes
#   from /trust (px-proxy).
#
# Stages:  base (+ ODBC 18) ─┬─ python    main env: build/image/requirements-platform.txt +
#                │            EVERY build/requirements/*.txt except superset.txt (one uv resolution)
#                ├─ superset  /opt/venvs/superset from build/requirements/superset.txt (skipped if absent)
#                ├─ egress    /opt/venvs/egress (px-proxy: mitmproxy, isolated pins)
#                ├─ ide       code-server + Open VSX extensions
#                ├─ clitools  docker CLI + compose plugin (Studio), Claude Code CLI
#                ├─ sparkjars PostgreSQL JDBC driver for Spark (pinned, sha256-checked)
#                └─ final     + headless JRE 21 (PySpark, build/requirements/spark.txt)
# ============================================================================

ARG PYTHON_IMAGE=python:3.12-slim-trixie

# ── Stage 0: runtime OS base ─────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS base
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY --chmod=755 build/image/with-build-trust.sh /usr/local/bin/with-build-trust
RUN --mount=type=secret,id=build_ca,required=false \
    with-build-trust bash -c '\
    set -euo pipefail; \
    apt-get update -qq; \
    apt-get install -y --no-install-recommends \
      ca-certificates curl git less procps bash unzip tini gnupg openssl \
      openssh-client nginx libpq5 postgresql-client jq libgomp1; \
    rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/default'

# Microsoft ODBC Driver 18 for SQL Server (SQL Server / Azure SQL through
# pyodbc — integration canvas). packages.microsoft.com publishes a repo per
# Debian release (debian/13 = trixie) for amd64 and arm64; the repo
# definition is purged after the install so later apt runs never reach it.
RUN --mount=type=secret,id=build_ca,required=false \
    with-build-trust bash -c '\
    set -euo pipefail; . /etc/os-release; \
    APT=(apt-get -o Acquire::https::CAInfo="${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"); \
    curl -fsSL --retry 3 -o /tmp/mprod.deb "https://packages.microsoft.com/config/debian/${VERSION_ID}/packages-microsoft-prod.deb"; \
    dpkg -i /tmp/mprod.deb; rm /tmp/mprod.deb; \
    "${APT[@]}" update -qq; \
    ACCEPT_EULA=Y "${APT[@]}" install -y --no-install-recommends msodbcsql18 unixodbc; \
    dpkg -P packages-microsoft-prod; \
    rm -rf /var/lib/apt/lists/*; \
    odbcinst -q -d -n "ODBC Driver 18 for SQL Server"'

# ── Stage 1: main Python environment — ONE resolution ────────────────────
FROM base AS python
RUN --mount=type=secret,id=build_ca,required=false \
    with-build-trust bash -c '\
    set -euo pipefail; apt-get update -qq; \
    apt-get install -y --no-install-recommends build-essential pkg-config libpq-dev libffi-dev unixodbc-dev; \
    rm -rf /var/lib/apt/lists/*'
COPY build/image/requirements-platform.txt /opt/appliance/requirements-platform.txt
COPY build/requirements /opt/appliance/requirements
RUN --mount=type=secret,id=build_ca,required=false \
    --mount=type=cache,target=/root/.cache/uv \
    with-build-trust bash -c '\
    set -euo pipefail; \
    python -m pip install --upgrade pip setuptools wheel uv; \
    REQS=(-r /opt/appliance/requirements-platform.txt); \
    for f in /opt/appliance/requirements/*.txt; do \
      [[ "$(basename "$f")" == superset.txt ]] && continue; REQS+=(-r "$f"); done; \
    echo "installing: ${REQS[*]}"; \
    UV_LINK_MODE=copy UV_HTTP_TIMEOUT=300 uv pip install --system --compile-bytecode "${REQS[@]}"; \
    rm -rf /usr/local/lib/python3.*/site-packages/pandas/tests'
# Smoke test: every platform module the roles depend on must import.
RUN python - <<'PY'
import importlib, sys
mods = [
    "jupyterlab", "ipykernel", "ipywidgets", "nbconvert", "nbclient", "papermill", "jupytext", "sql", "itables",
    "numpy", "pandas", "polars", "duckdb", "duckdb_engine", "pyarrow", "scipy", "plotly", "matplotlib", "openpyxl",
    "sqlalchemy", "psycopg2", "dbt.version", "dbt.adapters.postgres", "dbt.adapters.bigquery", "dbt.adapters.trino",
    "trino", "trino.sqlalchemy", "google.cloud.bigquery", "google.cloud.bigquery_storage", "db_dtypes", "google.auth",
    "fastapi", "uvicorn", "multipart", "sse_starlette", "websockets", "httpx", "requests", "yaml", "ruamel.yaml",
    "docker", "jose", "cryptography", "itsdangerous", "argon2", "msal",
    "mkdocs", "material", "playwright", "pyspark", "pyspark.sql", "py4j",
]
# integration canvas connectors (build/requirements/canvas.txt): checked
# strictly when the canvas requirements declare them, reported otherwise
import pathlib, re
declared = " ".join(p.read_text().lower() for p in pathlib.Path("/opt/appliance/requirements").glob("*.txt"))
for m, dist in (("pyodbc", "pyodbc"), ("oracledb", "oracledb"), ("pymssql", "pymssql"), ("pymysql", "pymysql"),
                ("gcsfs", "gcsfs"), ("s3fs", "s3fs"), ("adlfs", "adlfs")):
    if re.search(rf"(^|\s|\[|,){dist}\b", declared):
        mods.append(m)
    else:
        print(f"note: {dist} not declared in build/requirements — import check skipped")
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as exc:  # noqa: BLE001
        bad.append(f"{m}: {exc!r}")
if bad:
    sys.exit("python smoke test failed:\n  " + "\n  ".join(bad))
import importlib.metadata as md
print("pandas", md.version("pandas"), "SQLAlchemy", md.version("sqlalchemy"), "dbt-core", md.version("dbt-core"))
try:
    import pyodbc
except ImportError:
    pyodbc = None
if pyodbc and "ODBC Driver 18 for SQL Server" not in pyodbc.drivers():
    sys.exit(f"pyodbc does not see msodbcsql18: {pyodbc.drivers()}")
print("pyodbc", pyodbc.version if pyodbc else "-", pyodbc.drivers() if pyodbc else "")
PY
# Informational: report (not fail on) metadata conflicts.
RUN python -m pip check || echo "pip check reported the conflicts above"

# ── Stage 1b: Superset venv (own pins; only when the BI component ships it) ─
FROM base AS superset
COPY build/requirements /tmp/requirements
RUN --mount=type=secret,id=build_ca,required=false \
    --mount=type=cache,target=/root/.cache/uv \
    with-build-trust bash -c '\
    set -euo pipefail; mkdir -p /opt/venvs; \
    if [[ ! -s /tmp/requirements/superset.txt ]]; then \
      echo "build/requirements/superset.txt absent — Superset venv skipped"; exit 0; fi; \
    apt-get update -qq; apt-get install -y --no-install-recommends build-essential pkg-config libpq-dev libffi-dev; \
    python -m pip install -q uv; \
    uv venv /opt/venvs/superset; \
    UV_LINK_MODE=copy UV_HTTP_TIMEOUT=300 uv pip install --python /opt/venvs/superset/bin/python --compile-bytecode \
      -r /tmp/requirements/superset.txt; \
    /opt/venvs/superset/bin/python -c "import superset, importlib.metadata as m; print(\"superset\", m.version(\"apache-superset\"))"'

# ── Stage 1c: px-proxy venv ──────────────────────────────────────────────
FROM base AS egress
COPY build/image/requirements-egress.txt /tmp/requirements-egress.txt
RUN --mount=type=secret,id=build_ca,required=false \
    --mount=type=cache,target=/root/.cache/uv \
    with-build-trust bash -c '\
    set -euo pipefail; python -m pip install -q uv; uv venv /opt/venvs/egress; \
    UV_LINK_MODE=copy uv pip install --python /opt/venvs/egress/bin/python --compile-bytecode -r /tmp/requirements-egress.txt; \
    /opt/venvs/egress/bin/python -c "import mitmproxy, aiohttp, yaml, certifi; print(\"mitmproxy ok\")"'

# ── Stage 2: code-server + extensions ────────────────────────────────────
FROM base AS ide
ARG CODE_SERVER_VERSION=4.140.0
RUN --mount=type=secret,id=build_ca,required=false \
    with-build-trust bash -c '\
    set -euo pipefail; ARCH=$(dpkg --print-architecture); \
    curl -fsSL --retry 3 -o /tmp/cs.deb \
      "https://github.com/coder/code-server/releases/download/v${CODE_SERVER_VERSION}/code-server_${CODE_SERVER_VERSION}_${ARCH}.deb"; \
    dpkg -i /tmp/cs.deb; rm /tmp/cs.deb; code-server --version; \
    python -m pip install -q pyyaml'
COPY build/image/code-server-extensions.yaml /tmp/extensions.yaml
RUN --mount=type=secret,id=build_ca,required=false \
    with-build-trust python - <<'PY'
import subprocess, sys, yaml
exts = yaml.safe_load(open("/tmp/extensions.yaml"))["extensions"]
failed = []
for e in exts:
    ext_id, required = (e, True) if isinstance(e, str) else (e["id"], e.get("required", True))
    r = subprocess.run(["code-server", "--extensions-dir", "/opt/code-server/extensions",
                        "--user-data-dir", "/tmp/cs-build", "--install-extension", ext_id, "--force"],
                       capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode != 0:
        (failed.append(ext_id) if required else print(f"optional extension {ext_id} skipped"))
if failed:
    sys.exit(f"required code-server extensions failed: {failed}")
PY
RUN rm -rf /tmp/extensions.yaml /tmp/cs-build && chmod -R a+rX /opt/code-server

# ── Stage 3: CLI tools (docker + compose for Studio; Claude Code) ────────
FROM base AS clitools
ARG DOCKER_CLI_VERSION=29.8.1
ARG COMPOSE_VERSION=v2.39.4
ARG WITH_CLAUDE=1
RUN --mount=type=secret,id=build_ca,required=false \
    with-build-trust bash -c '\
    set -euo pipefail; ARCH=$(dpkg --print-architecture); \
    case "$ARCH" in amd64) C=x86_64;; arm64) C=aarch64;; *) echo "unsupported $ARCH"; exit 1;; esac; \
    mkdir -p /out/bin /out/cli-plugins; \
    curl -fsSL --retry 3 "https://download.docker.com/linux/static/stable/${C}/docker-${DOCKER_CLI_VERSION}.tgz" \
      | tar -xz -C /tmp docker/docker; \
    install -m 755 /tmp/docker/docker /out/bin/docker; \
    curl -fsSL --retry 3 -o /out/cli-plugins/docker-compose \
      "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${C}"; \
    chmod 755 /out/cli-plugins/docker-compose; \
    /out/bin/docker --version; /out/cli-plugins/docker-compose version'
# Claude Code (Anthropic's native installer, arch-aware). Optional: a failed
# download leaves it out instead of failing the image.
RUN --mount=type=secret,id=build_ca,required=false \
    with-build-trust bash -c '\
    set -uo pipefail; [[ "${WITH_CLAUDE}" == 1 ]] || exit 0; export HOME=/tmp/cc; mkdir -p "$HOME"; \
    if curl -fsSL --retry 3 https://claude.ai/install.sh -o /tmp/cc/install.sh && bash /tmp/cc/install.sh stable; then \
      install -m 755 "$(readlink -f /tmp/cc/.local/bin/claude)" /out/bin/claude && /out/bin/claude --version; \
    else echo "WARNING: Claude Code CLI not installed"; fi; rm -rf /tmp/cc; exit 0'
# OpenCode CLI (pinned release binary, arch-aware). Optional like Claude Code:
# a failed download leaves it out instead of failing the image. It uses the
# appliance's Google ADC (Vertex) and the harness gateway — see
# build/services/gcp-adc.sh and work_templates/common/.config/opencode/.
ARG OPENCODE_VERSION=v1.19.3
RUN --mount=type=secret,id=build_ca,required=false \
    with-build-trust bash -c '\
    set -uo pipefail; case "$(dpkg --print-architecture)" in amd64) A=x64;; arm64) A=arm64;; *) exit 0;; esac; \
    if curl -fsSL --retry 3 -o /tmp/oc.tgz \
         "https://github.com/anomalyco/opencode/releases/download/${OPENCODE_VERSION}/opencode-linux-${A}.tar.gz" \
       && tar -xzf /tmp/oc.tgz -C /tmp opencode; then \
      install -m 755 /tmp/opencode /out/bin/opencode && /out/bin/opencode --version; \
    else echo "WARNING: OpenCode CLI not installed"; fi; rm -f /tmp/oc.tgz /tmp/opencode; exit 0'

# ── Stage 3b: JDBC drivers for Spark (baked in: runtime egress is allowlisted) ─
FROM base AS sparkjars
ARG PGJDBC_VERSION=42.7.13
ARG PGJDBC_SHA256=6e0e4cc2d8cae902084f8a2b18728b073a6fd9d1f87c9d8bff8f298c18185b93
RUN --mount=type=secret,id=build_ca,required=false \
    with-build-trust bash -c '\
    set -euo pipefail; mkdir -p /out/spark-jars; \
    curl -fsSL --retry 3 -o /out/spark-jars/postgresql-${PGJDBC_VERSION}.jar \
      "https://repo1.maven.org/maven2/org/postgresql/postgresql/${PGJDBC_VERSION}/postgresql-${PGJDBC_VERSION}.jar"; \
    echo "${PGJDBC_SHA256}  /out/spark-jars/postgresql-${PGJDBC_VERSION}.jar" | sha256sum -c -; \
    ln -s postgresql-${PGJDBC_VERSION}.jar /out/spark-jars/postgresql.jar'

# ── Stage 4: final ───────────────────────────────────────────────────────
FROM base AS final
RUN groupadd --gid 1000 analysts \
 && useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home analyst

# Headless JRE 21 for PySpark (local mode; no GUI/AWT libs, no recommends).
# /usr/lib/jvm/java-21 is an arch-independent alias for JAVA_HOME.
RUN --mount=type=secret,id=build_ca,required=false \
    with-build-trust bash -c '\
    set -euo pipefail; mkdir -p /usr/share/man/man1; apt-get update -qq; \
    apt-get install -y --no-install-recommends openjdk-21-jre-headless; \
    rm -rf /var/lib/apt/lists/*; \
    ln -sfn "/usr/lib/jvm/java-21-openjdk-$(dpkg --print-architecture)" /usr/lib/jvm/java-21; \
    /usr/lib/jvm/java-21/bin/java -version'

COPY --from=python   /usr/local /usr/local
COPY --from=python   /opt/appliance /opt/appliance
COPY --from=superset /opt/venvs /opt/venvs
COPY --from=egress   /opt/venvs/egress /opt/venvs/egress
COPY --from=ide      /usr/lib/code-server /usr/lib/code-server
COPY --from=ide      /opt/code-server /opt/code-server
COPY --from=clitools /out/bin/ /usr/local/bin/
COPY --from=clitools /out/cli-plugins/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
COPY --from=sparkjars /out/spark-jars /opt/spark-jars
COPY build/image/spark /opt/spark-conf
# Claude Code organisation policy (no telemetry, no auto-update, no bypass mode)
COPY build/claude/managed-settings.json /etc/claude-code/managed-settings.json
RUN ln -sf /usr/lib/code-server/bin/code-server /usr/bin/code-server \
 && ln -sf /usr/local/lib/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose \
 && install -d -o 1000 -g 1000 \
      /trust /data /data/egress /data/logs /data/pyuser /data/lake /data/hf_cache /data/mlops \
      /home/analyst/work /home/analyst/.ssh /home/analyst/.mitmproxy \
      /home/analyst/.local/share/code-server /etc/nginx/tls /tmp/nginx \
 && chown -R 1000:1000 /home/analyst && chmod 700 /home/analyst/.ssh

# Application code for runs without bind mounts; compose bind-mounts the same paths.
COPY build/services       /opt/services
COPY build/studio         /opt/studio
COPY build/workbench      /opt/workbench
COPY build/ui-kit         /opt/ui-kit
COPY build/px-proxy       /opt/px-proxy
COPY build/common         /opt/common
COPY build/seed           /opt/seed
COPY work_templates       /opt/templates
COPY dbt_project          /opt/templates/dbt_project
COPY docs                 /opt/docs
COPY build/nginx/nginx.conf /etc/nginx/nginx.conf
COPY build/nginx/conf.d   /etc/nginx/conf.d
COPY build/nginx/snippets /etc/nginx/snippets
COPY build/nginx/routes.d /etc/nginx/routes.d
COPY build/entrypoint.sh  /usr/local/bin/entrypoint.sh

# (application code last: these layers are small and change often)
RUN chmod 755 /usr/local/bin/entrypoint.sh \
 && chmod -R a+rX /opt/services /opt/studio /opt/workbench /opt/ui-kit /opt/px-proxy /opt/common /opt/seed \
      /opt/templates /opt/docs /etc/nginx /opt/spark-jars /opt/spark-conf \
 && find /opt/services /opt/studio /opt/workbench /opt/px-proxy /opt/common /opt/seed /opt/templates \
      -name __pycache__ -prune -exec rm -rf {} +

# Runtime environment (no proxy, no secrets, no CA baked in).
ENV USER_HANDLE=analyst \
    HOME=/home/analyst \
    PYTHONUSERBASE=/data/pyuser \
    PYTHONPATH=/opt/common:/opt/seed:/opt/connectors \
    PATH="/data/pyuser/bin:${PATH}" \
    LAKE_DIR=/data/lake \
    CLAUDE_CONFIG_DIR=/home/analyst/work/.claude \
    DISABLE_AUTOUPDATER=1 \
    CODE_SERVER_PORT=8080 \
    JAVA_HOME=/usr/lib/jvm/java-21 \
    SPARK_CONF_DIR=/opt/spark-conf \
    SPARK_JARS_DIR=/opt/spark-jars \
    SPARK_LOCAL_IP=127.0.0.1 \
    SPARK_DRIVER_MEMORY=2g \
    PYSPARK_PYTHON=python3

USER analyst
WORKDIR /home/analyst
RUN jupyter kernelspec list | grep -qE '^\s+python3\s' \
 && code-server --version >/dev/null && docker compose version && dbt --version && nginx -v \
 && /opt/venvs/egress/bin/python -c "import mitmproxy" \
 && python -c "import bsp_common, bsp_seed; print('bsp_common', 'bsp_seed', 'ok')" \
 && java -version 2>&1 | head -1 && test -s /opt/spark-jars/postgresql.jar \
 && python -c "from pyspark.sql import SparkSession; s = SparkSession.builder.master('local[1]').getOrCreate(); \
print('pyspark', s.version, 'rows', s.range(3).count()); s.stop()" \
 && rm -rf /tmp/spark-* /tmp/hsperfdata_analyst

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["start"]
EXPOSE 8888 8002 8080
