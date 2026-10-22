"""Artifact freezing — an immutable, hashed, self-describing bundle.

    bsp_churn-v3-20261018T101500Z/
      model/                    the MLflow model exactly as registered (MLmodel, model.pkl,
                                conda.yaml, requirements.txt, signature, input example)
      data/training_snapshot.parquet   the point-in-time training rows (+ test snapshot)
      features/features.yaml    feature-view definitions; registry.json with definition hashes
      features/model_features.json
      metrics/run.json          MLflow params, metrics, tags of the training run
      env/requirements.lock     pip freeze of the serving environment at freeze time
      env/training_requirements.lock   the lock recorded with the training run (when present)
      env/platform.json         python / OS
      config/training_config.json      the exact training recipe (family, params, features, windows,
                                seed, data snapshot / code / environment hashes)
      lineage/lineage.json      run → data snapshot → feature views → code → parent version →
                                journey → release gates → approvals → reproductions
      release/promotions.json   every promotion / approval / rollback record of this version
                                (+ the audit-log rows that name it)
      deploy/Dockerfile, deploy/cloudrun-service.yaml, deploy/vertex_model_upload.sh,
      deploy/README.md          a portable serving image + Cloud Run / Vertex AI specs (generated,
                                not executed by the appliance)
      pipelines/source/         the bsp_mlops package + feature definitions that trained it, and
      pipelines/reproduce.sh    the commands that retrain / re-verify it outside any UI
      manifest.json             every file's sha256 + size, run id, registry version, aliases,
                                dataset window + hash, feature-view versions, bundle digest
    + <bundle>.tar.gz           deterministic tarball (sorted, mtime 0) — its sha256 is THE id
    + oci/                      optional OCI image layout (index.json, blobs/sha256/…) so the
                                bundle can be pushed to any OCI registry with `oras` / `crane`

push → an Artifactory: `LocalArtifactory` (${DATA_ROOT}/mlops/artifactory — content
addressed, read-only files, refuses to overwrite different content) or
`JFrogArtifactory` (ARTIFACTORY_URL + ARTIFACTORY_TOKEN, REST PUT with checksum
headers) when configured. The registry version gets `frozen=true` +
`frozen_sha256`, and mlops.frozen_artifacts records it.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import stat
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, db

SCHEMA = "bsp.frozen-model/v1"
REPO = os.environ.get("ARTIFACTORY_REPO", "bsp-ml-models")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _pip_freeze() -> str:
    from importlib import metadata

    pkgs = sorted({f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions() if d.metadata["Name"]},
                  key=str.lower)
    return "\n".join(pkgs) + "\n"


def deterministic_tar(src: Path, dest: Path) -> Path:
    def norm(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = 0, 0, 0, "", ""
        ti.mode = 0o755 if ti.isdir() else 0o644
        return ti

    import gzip

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for p in [src] + sorted(src.rglob("*")):
            tf.add(p, arcname=str(Path(src.name) / p.relative_to(src)) if p != src else src.name, recursive=False,
                   filter=norm)
    with open(dest, "wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", mtime=0, filename="") as gz:
        gz.write(raw.getvalue())
    return dest


def build_bundle(version: str | None = None, alias: str = "champion", out_dir: Path | None = None, oci: bool = True,
                 verbose: bool = True) -> dict:
    import mlflow

    from . import registry
    from .featurestore import definitions_path, freshness
    from .pipeline import training_set

    config.ensure_dirs()
    c = registry.client()
    if version is None:
        mv = registry.alias_version(alias)
        if mv is None:
            raise RuntimeError(f"no {alias} version to freeze")
        version = str(mv.version)
    mv = c.get_model_version(config.MODEL_NAME, version)
    run = c.get_run(mv.run_id)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{config.MODEL_NAME}-v{version}-{ts}"
    root = Path(out_dir or config.FROZEN_DIR) / name
    b = root / name
    for d in ("model", "data", "features", "metrics", "env", "config", "lineage", "release", "deploy", "pipelines"):
        (b / d).mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    # model, exactly as registered
    local = mlflow.artifacts.download_artifacts(f"models:/{config.MODEL_NAME}/{version}", dst_path=str(root / "_dl"))
    for p in Path(local).iterdir():
        (shutil.copytree if p.is_dir() else shutil.copy2)(p, b / "model" / p.name)
    shutil.rmtree(root / "_dl", ignore_errors=True)
    # data snapshot (point-in-time training rows)
    feats = registry.model_features(version)
    tw = run.data.params.get("train_window", "")
    a, z = (tw.split("..") + [None])[:2] if tw else (None, None)
    datasets = {}
    if a and z:
        tr = training_set((a + "-01", z + "-01"), feats)
        tr.to_parquet(b / "data" / "training_snapshot.parquet", index=False)
        datasets["training"] = {"window": tw, "rows": int(len(tr)), "columns": list(tr.columns)}
    te_w = run.data.params.get("test_window", "")
    if te_w:
        a2, z2 = te_w.split("..")
        te = training_set((a2 + "-01", z2 + "-01"), feats)
        te.to_parquet(b / "data" / "test_snapshot.parquet", index=False)
        datasets["test"] = {"window": te_w, "rows": int(len(te))}
    # feature definitions + registry state
    shutil.copy2(definitions_path(), b / "features" / "features.yaml")
    fr = freshness()
    (b / "features" / "registry.json").write_text(json.dumps(
        [{k: r[k] for k in ("feature_view", "version", "def_hash", "offline_table", "rows", "last_event")} for r in fr],
        indent=2, default=str))
    (b / "features" / "model_features.json").write_text(json.dumps({"features": feats}, indent=2))
    # metrics / params
    (b / "metrics" / "run.json").write_text(json.dumps({"run_id": run.info.run_id, "experiment_id": run.info.experiment_id,
                                                         "run_name": run.info.run_name, "params": run.data.params,
                                                         "metrics": run.data.metrics, "tags": {k: v for k, v in run.data.tags.items()
                                                                                              if not k.startswith("mlflow.log-model")}},
                                                        indent=2, default=str))
    # environment lock
    (b / "env" / "requirements.lock").write_text(_pip_freeze())
    export = export_extras(b, version, mv, run, feats)
    (b / "env" / "platform.json").write_text(json.dumps({"python": sys.version, "implementation": platform.python_implementation(),
                                                          "platform": platform.platform(), "machine": platform.machine()}, indent=2))
    # manifest
    files = {str(p.relative_to(b)): {"sha256": sha256_file(p), "size": p.stat().st_size} for p in _files(b)}
    digest = hashlib.sha256("".join(f"{v['sha256']}  {k}\n" for k, v in sorted(files.items())).encode()).hexdigest()
    manifest = {
        "schema": SCHEMA, "bundle": name, "created_at": datetime.now(timezone.utc).isoformat(), "created_by": config.user(),
        "model": {"name": config.MODEL_NAME, "version": version, "run_id": mv.run_id, "source": mv.source,
                  "aliases": [k for k, v in registry.aliases().items() if v == version], "model_kind": run.data.params.get("model_kind")},
        "mlflow": {"tracking_uri": config.MLFLOW_URI, "experiment_id": run.info.experiment_id},
        "metrics": {k: run.data.metrics.get(k) for k in ("valid_auc", "test_auc", "test_pr_auc", "test_logloss", "test_lift_at_10")},
        "datasets": {k: {**v, "sha256": files.get(f"data/{k}_snapshot.parquet", {}).get("sha256")} for k, v in datasets.items()},
        "feature_store": {"definitions_sha256": files["features/features.yaml"]["sha256"],
                          "views": [{"name": r["feature_view"], "version": r["version"], "def_hash": r["def_hash"]} for r in fr]},
        "environment": {"python": platform.python_version(), "lock_sha256": files["env/requirements.lock"]["sha256"]},
        "lineage": export["lineage_summary"], "release": export["release_summary"],
        "exportable": {"model": "model/ (MLflow format: MLmodel, pickle, conda.yaml, requirements.txt, signature)",
                       "pipeline": "pipelines/source + pipelines/reproduce.sh", "metadata": "metrics/, lineage/, release/, config/",
                       "serving": "deploy/Dockerfile (+ Cloud Run / Vertex AI specs)", "features": "features/features.yaml (Feast-compatible)"},
        "files": files, "content_digest": f"sha256:{digest}",
    }
    (b / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    tgz = deterministic_tar(b, root / f"{name}.tar.gz")
    tar_sha = sha256_file(tgz)
    (root / f"{name}.tar.gz.sha256").write_text(f"{tar_sha}  {tgz.name}\n")
    res = {"bundle": name, "dir": str(b), "tarball": str(tgz), "sha256": tar_sha, "size_bytes": tgz.stat().st_size,
           "content_digest": manifest["content_digest"], "files": len(files), "model_version": version, "run_id": mv.run_id,
           "manifest": manifest}
    if oci:
        res["oci"] = str(oci_layout(root, tgz, b / "manifest.json"))
    _readonly(root)
    res["seconds"] = round(time.time() - t0, 1)
    if verbose:
        print(f"  · froze v{version}: {len(files)} files, tar sha256 {tar_sha[:16]}…, {res['size_bytes'] / 1e6:.1f} MB "
              f"({res['seconds']}s)")
    return res


def export_extras(b: Path, version: str, mv, run, feats: list[str]) -> dict:
    """Everything beyond the model needed to run, audit or rebuild it elsewhere."""
    from . import lineage
    from .registry import client

    c = client()
    tags = run.data.tags
    # training recipe + training-time lock
    cfg = None
    for name, dest in (("config/training_config.json", b / "config" / "training_config.json"),
                       ("environment/requirements.lock", b / "env" / "training_requirements.lock")):
        try:
            got = c.download_artifacts(mv.run_id, name, str(b / "_tmp"))
            shutil.copy2(got, dest)
            if name.startswith("config/"):
                cfg = json.loads(Path(got).read_text())
        except Exception:
            pass
    shutil.rmtree(b / "_tmp", ignore_errors=True)
    if cfg is None:
        cfg = {"model_kind": run.data.params.get("model_kind"), "features": feats,
               "params": {k[3:]: v for k, v in run.data.params.items() if k.startswith("hp_")},
               "windows": {k: run.data.params.get(f"{k}_window") for k in ("train", "valid", "test")},
               "recency_boost": run.data.params.get("recency_boost"), "seed": run.data.params.get("seed", "7 (default)"),
               "note": "reconstructed from MLflow params — this run predates the recorded training config"}
        (b / "config" / "training_config.json").write_text(json.dumps(cfg, indent=2, default=str))
    # release records
    promos, audits = [], []
    try:
        promos = db.fetchall("select * from mlops.promotions where model_name=%s and model_version=%s order by id", (config.MODEL_NAME, version))
        audits = db.fetchall("select * from mlops.audit_log where target=%s order by id", (f"{config.MODEL_NAME}:v{version}",))
    except Exception:
        pass
    repros = []
    try:
        repros = db.fetchall("select id, created_at, parity, data_hash_match, code_match, env_match, repro_run_id from mlops.reproductions "
                             "where model_version=%s order by id", (version,))
    except Exception:
        pass
    (b / "release" / "promotions.json").write_text(json.dumps({"promotions": promos, "audit_log": audits}, indent=2, default=str))
    approved = [p for p in promos if p["to_stage"] == "production" and p["status"] in ("approved", "completed")]
    release_summary = {"promotions": len(promos), "approved_to_production": bool(approved),
                       "approved_by": approved[-1]["approved_by"] if approved else None,
                       "requested_by": approved[-1]["requested_by"] if approved else None,
                       "gates_passed": next((p["gates_passed"] for p in reversed(promos) if p.get("gates") is not None), None)}
    # lineage
    mv_tags = mv.tags or {}
    lin = {"model": {"name": config.MODEL_NAME, "version": version, "run_id": mv.run_id, "experiment_id": run.info.experiment_id,
                     "run_name": run.info.run_name, "source": mv.source, "artifact_sha256": mv_tags.get("bsp.artifact_sha256")},
           "data": {"source": "features.churn_base (feature-store point-in-time join)", "snapshot_sha256": tags.get("bsp.data_snapshot_sha256"),
                    "windows": cfg.get("windows")},
           "features": {"views": cfg.get("feature_views"), "model_features": feats},
           "code": {"code_sha256": tags.get("bsp.code_sha256"), "git_sha": tags.get("bsp.git_sha"),
                    "package_snapshot": "pipelines/source", "current_code_sha256": lineage.code_fingerprint()["code_sha256"]},
           "environment": {"training_lock_sha256": tags.get("bsp.env_lock_sha256"), "python": tags.get("bsp.python")},
           "parents": {"retrained_from_version": tags.get("parent_champion_version"), "retrain_id": tags.get("retrain_id"),
                       "tuning_parent_run": tags.get("parent_run")},
           "journey": {"journey_id": mv_tags.get("bsp.journey_id"), "journey_run_id": mv_tags.get("bsp.journey_run_id")},
           "release": {"gate_evidence_runs": sorted({p["evidence_run_id"] for p in promos if p.get("evidence_run_id")}),
                       "promotion_ids": [p["id"] for p in promos]},
           "reproductions": repros}
    (b / "lineage" / "lineage.json").write_text(json.dumps(lin, indent=2, default=str))
    # portable serving: image + Cloud Run / Vertex AI specs (generated here, executed by the platform team's CI)
    project = os.environ.get("GCP_ARTIFACT_PROJECT") or os.environ.get("GCP_PROJECT_ID") or "YOUR_PROJECT"
    region = os.environ.get("GCP_REGION") or "us-central1"
    image = f"{region}-docker.pkg.dev/{project}/{REPO}/{config.MODEL_NAME.replace('_', '-')}:v{version}"
    (b / "deploy" / "Dockerfile").write_text(f"""# Portable serving image for {config.MODEL_NAME} v{version} — built from this bundle only.
#   docker build -t {image} -f deploy/Dockerfile .
#   docker run -p 8080:8080 {image}      # POST /invocations  (MLflow scoring protocol)
FROM python:{platform.python_version_tuple()[0]}.{platform.python_version_tuple()[1]}-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
RUN useradd -u 1000 -m serve
WORKDIR /srv
COPY model/ /srv/model/
RUN pip install --no-cache-dir -r /srv/model/requirements.txt "mlflow-skinny=={_mlflow_version()}" gunicorn
ENV MLFLOW_ALLOW_PICKLE_DESERIALIZATION=true PORT=8080
USER serve
EXPOSE 8080
LABEL org.opencontainers.image.title="{config.MODEL_NAME}" org.opencontainers.image.version="v{version}" \
      bsp.mlflow_run_id="{mv.run_id}" bsp.artifact_sha256="{mv_tags.get('bsp.artifact_sha256', '')}"
CMD ["sh", "-c", "mlflow models serve -m /srv/model -h 0.0.0.0 -p $PORT --env-manager local"]
""")
    (b / "deploy" / "cloudrun-service.yaml").write_text(f"""# Cloud Run (knative) service for {config.MODEL_NAME} v{version}.
#   gcloud run services replace deploy/cloudrun-service.yaml --region {region}
# Identity: a dedicated service account (workload identity) — no key files; ingress internal + IAP / IAM invoker only.
apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: {config.MODEL_NAME.replace('_', '-')}-v{version}
  labels: {{model: {config.MODEL_NAME.replace('_', '-')}, version: v{version}}}
  annotations:
    run.googleapis.com/ingress: internal-and-cloud-load-balancing
spec:
  template:
    metadata:
      annotations:
        autoscaling.knative.dev/minScale: "1"
        autoscaling.knative.dev/maxScale: "10"
    spec:
      serviceAccountName: sa-churn-serving@{project}.iam.gserviceaccount.com
      containerConcurrency: 40
      timeoutSeconds: 30
      containers:
        - image: {image}
          ports: [{{containerPort: 8080}}]
          resources: {{limits: {{cpu: "1", memory: 1Gi}}}}
          env:
            - {{name: BSP_MODEL_VERSION, value: "{version}"}}
          startupProbe: {{httpGet: {{path: /ping}}, periodSeconds: 5, failureThreshold: 24}}
  traffic:
    - {{percent: 100, latestRevision: true}}     # canary: split percent across two revisions
""")
    (b / "deploy" / "vertex_model_upload.sh").write_text(f"""#!/usr/bin/env bash
# Register this bundle's serving image in Vertex AI Model Registry and deploy it to an endpoint.
# Generated by the appliance; run by the platform team's CI (Cloud Build) with workload identity.
set -euo pipefail
REGION={region}; PROJECT={project}; IMAGE={image}
gcloud ai models upload --region=$REGION --project=$PROJECT --display-name={config.MODEL_NAME}-v{version} \
  --container-image-uri=$IMAGE --container-predict-route=/invocations --container-health-route=/ping --container-ports=8080 \
  --version-aliases=v{version} --labels=mlflow_run={mv.run_id[:60]}
# gcloud ai endpoints deploy-model ENDPOINT_ID --region=$REGION --model=MODEL_ID --traffic-split=0=10,PREVIOUS=90   # canary
""")
    (b / "deploy" / "README.md").write_text(f"""# Serving {config.MODEL_NAME} v{version} outside the appliance

* `model/` is a standard MLflow model: `mlflow.pyfunc.load_model("model")` returns churn probabilities
  (`predict_proba`), input schema in `model/MLmodel` (signature) and `model/input_example.json`.
* `Dockerfile` builds a self-contained scoring image (MLflow scoring protocol, `POST /invocations`).
* `cloudrun-service.yaml` / `vertex_model_upload.sh` deploy that image on Cloud Run or a Vertex AI endpoint.
  Both are generated specs — the appliance does not execute them.
* Features for an id-only request come from the feature store; `features/features.yaml` holds the governed
  definitions (Feast-compatible) and `features/model_features.json` the model's input list.
""")
    # pipeline source snapshot + reproduce script
    src = Path(__file__).resolve().parent
    dst = b / "pipelines" / "source" / "bsp_mlops"
    for p in sorted(src.rglob("*")):
        if p.is_file() and (p.suffix in (".py", ".yaml") and "__pycache__" not in p.parts):
            q = dst / p.relative_to(src)
            q.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, q)
    (b / "pipelines" / "reproduce.sh").write_text(f"""#!/usr/bin/env bash
# Re-verify / rebuild {config.MODEL_NAME} v{version} without any UI (needs the warehouse + MLflow of the appliance,
# or the same features.churn_base snapshot: sha256 {tags.get('bsp.data_snapshot_sha256') or 'not recorded'}).
set -euo pipefail
export PYTHONPATH="$(dirname "$0")/source:${{PYTHONPATH:-}}"
python -m bsp_mlops.reproduce {version}                 # refit from config/training_config.json, compare metrics + predictions
python -m bsp_mlops.release gates {version}             # re-run the release gates against the current champion
""")
    return {"lineage_summary": {k: lin[k] for k in ("data", "code", "parents", "journey")} | {"artifact_sha256": mv_tags.get("bsp.artifact_sha256")},
            "release_summary": release_summary}


def _mlflow_version() -> str:
    try:
        import mlflow

        return mlflow.__version__
    except Exception:
        return "3.1.0"


def oci_layout(root: Path, layer: Path, cfg: Path) -> Path:
    """Minimal OCI image layout holding the bundle as one artifact layer."""
    o = root / "oci"
    blobs = o / "blobs" / "sha256"
    blobs.mkdir(parents=True, exist_ok=True)

    def put(data: bytes) -> tuple[str, int]:
        d = hashlib.sha256(data).hexdigest()
        (blobs / d).write_bytes(data)
        return d, len(data)

    ld = sha256_file(layer)
    shutil.copy2(layer, blobs / ld)
    cd, cs = put(cfg.read_bytes())
    man = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
           "artifactType": "application/vnd.bsp.model.bundle.v1+json",
           "config": {"mediaType": "application/vnd.bsp.model.manifest.v1+json", "digest": f"sha256:{cd}", "size": cs},
           "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": f"sha256:{ld}",
                       "size": layer.stat().st_size, "annotations": {"org.opencontainers.image.title": layer.name}}],
           "annotations": {"org.opencontainers.image.created": datetime.now(timezone.utc).isoformat()}}
    md, ms = put(json.dumps(man, indent=2).encode())
    (o / "oci-layout").write_text(json.dumps({"imageLayoutVersion": "1.0.0"}))
    (o / "index.json").write_text(json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                                              "manifests": [{"mediaType": "application/vnd.oci.image.manifest.v1+json",
                                                             "digest": f"sha256:{md}", "size": ms,
                                                             "annotations": {"org.opencontainers.image.ref.name": layer.stem}}]},
                                             indent=2))
    return o


def _readonly(root: Path) -> None:
    for p in root.rglob("*"):
        try:
            if p.is_file():
                p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass


def verify(bundle_dir: str | Path) -> dict:
    """Recompute every hash in manifest.json; returns {ok, mismatches}."""
    b = Path(bundle_dir)
    man = json.loads((b / "manifest.json").read_text())
    bad = [k for k, v in man["files"].items() if not (b / k).exists() or sha256_file(b / k) != v["sha256"]]
    extra = [str(p.relative_to(b)) for p in _files(b) if str(p.relative_to(b)) not in man["files"] and p.name != "manifest.json"]
    return {"ok": not bad and not extra, "mismatches": bad, "unexpected": extra, "checked": len(man["files"])}


# ── artifactory ───────────────────────────────────────────────────────────
class Artifactory:
    kind = "abstract"

    def put(self, path: str, file: Path, sha256: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def list(self, prefix: str = "") -> list[dict]:  # pragma: no cover - interface
        raise NotImplementedError


class LocalArtifactory(Artifactory):
    """File repository at ${DATA_ROOT}/mlops/artifactory/<repo>/<path> — same
    layout as a generic Artifactory repo; files are read-only and immutable."""
    kind = "local"

    def __init__(self, root: Path | None = None, repo: str = REPO) -> None:
        self.root = Path(root or config.ARTIFACTORY_DIR) / repo
        self.repo = repo

    def put(self, path: str, file: Path, sha256: str) -> str:
        dest = self.root / path
        if dest.exists():
            if sha256_file(dest) == sha256:
                return dest.as_uri()
            raise FileExistsError(f"immutable artifact {path} already exists with different content")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, dest)
        (dest.parent / (dest.name + ".sha256")).write_text(f"{sha256}  {dest.name}\n")
        for p in (dest, dest.parent / (dest.name + ".sha256")):
            p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        return dest.as_uri()

    def list(self, prefix: str = "") -> list[dict]:
        base = self.root / prefix
        if not base.exists():
            return []
        return [{"path": str(p.relative_to(self.root)), "size": p.stat().st_size,
                 "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
                for p in sorted(base.rglob("*")) if p.is_file() and not p.name.endswith(".sha256")]


class JFrogArtifactory(Artifactory):
    """JFrog Artifactory generic repo via REST (deploy artifact with checksum)."""
    kind = "jfrog"

    def __init__(self, url: str, token: str, repo: str = REPO) -> None:
        self.url, self.token, self.repo = url.rstrip("/"), token, repo

    def _headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.token}"}
        if len(self.token) < 80 and not self.token.startswith("ey"):  # legacy API key
            h = {"X-JFrog-Art-Api": self.token}
        return h

    def put(self, path: str, file: Path, sha256: str) -> str:
        import httpx

        target = f"{self.url}/{self.repo}/{path}"
        h = {**self._headers(), "X-Checksum-Sha256": sha256, "X-Checksum-Sha1": _sha1(file)}
        with open(file, "rb") as fh:
            r = httpx.put(target, content=fh.read(), headers=h, timeout=600)
        if r.status_code >= 300:
            raise RuntimeError(f"Artifactory PUT {target} → {r.status_code}: {r.text[:300]}")
        return target

    def list(self, prefix: str = "") -> list[dict]:
        import httpx

        r = httpx.get(f"{self.url}/api/storage/{self.repo}/{prefix}?list&deep=1", headers=self._headers(), timeout=60)
        r.raise_for_status()
        return [{"path": f["uri"].lstrip("/"), "size": f.get("size"), "modified": f.get("lastModified")}
                for f in r.json().get("files", [])]


def _sha1(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def artifactory() -> Artifactory:
    url, tok = os.environ.get("ARTIFACTORY_URL", "").strip(), os.environ.get("ARTIFACTORY_TOKEN", "").strip()
    if url and tok:
        return JFrogArtifactory(url, tok)
    return LocalArtifactory()


def freeze_and_push(version: str | None = None, alias: str = "champion", verbose: bool = True) -> dict:
    """Build the bundle, push tarball + manifest (+ sha256) to the
    artifactory, tag the registry version, record mlops.frozen_artifacts."""
    from . import registry

    res = build_bundle(version, alias, verbose=verbose)
    art = artifactory()
    root = Path(res["tarball"]).parent
    base = f"{config.MODEL_NAME}/{res['model_version']}"
    uri = art.put(f"{base}/{Path(res['tarball']).name}", Path(res["tarball"]), res["sha256"])
    man = Path(res["dir"]) / "manifest.json"
    art.put(f"{base}/{res['bundle']}.manifest.json", man, sha256_file(man))
    c = registry.client()
    for k, v in {"frozen": "true", "frozen_sha256": res["sha256"], "frozen_uri": uri, "frozen_bundle": res["bundle"]}.items():
        c.set_model_version_tag(config.MODEL_NAME, res["model_version"], k, v)
    db.ensure_schema()
    row = db.fetchone("""insert into mlops.frozen_artifacts (model_name, model_version, mlflow_run_id, bundle_name, sha256,
                         size_bytes, repository, uri, manifest, created_by) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                      (config.MODEL_NAME, res["model_version"], res["run_id"], res["bundle"], res["sha256"], res["size_bytes"],
                       art.kind, uri, db.jdump(res["manifest"]), config.user()))
    res.update(id=row["id"], uri=uri, repository=art.kind, verify=verify(res["dir"]))
    res.pop("manifest", None)
    if verbose:
        print(f"  · pushed to {art.kind} artifactory: {uri}  (verify: {'OK' if res['verify']['ok'] else 'FAILED'})")
    _ = root
    return res


def list_frozen(limit: int = 100) -> list[dict]:
    return db.fetchall("""select id, created_at, model_name, model_version, mlflow_run_id, bundle_name, sha256, size_bytes,
                          repository, uri, created_by, manifest->>'content_digest' as content_digest
                          from mlops.frozen_artifacts order by id desc limit %s""", (limit,))
