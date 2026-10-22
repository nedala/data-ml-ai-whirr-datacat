"""bsp_mlops — the ML Scientist appliance's end-to-end MLOps platform for the
DataPlatform churn use case.

Lifecycle (each stage is a module; notebooks and services are thin callers):

    data         integrate the warehouse churn snapshot (dbt mart or raw)
    eda          profiling, churn by segment / month, shift preview
    selection    feature sifting: missingness, variance, correlation/VIF, MI,
                 permutation importance, SHAP, stability selection
    splits       random / stratified / time-based / group hold-outs
    featurestore feature views (YAML), point-in-time training sets, online store
    pipeline     ColumnTransformer + model → MLflow, leaderboard, Optuna tuning
    registry     champion / challenger aliases, promote / rollback
    deploy       gateway admin client: single, blue/green, canary, shadow
    drift        PSI / KS / prediction / performance drift → mlops.drift_reports
    retrain      drift- or manually-triggered retrain + auto-promote rule
    simulator    replay traffic through the gateway (normal or shifted months)
    scoring      batch scores → mlops.scored_customers + outcome monitoring view
    freeze       immutable hashed bundle → artifactory (local or JFrog)
    demo         headless end-to-end proof:  python -m bsp_mlops.demo
"""

__version__ = "1.0.0"


def _ensure_libgomp() -> None:
    """LightGBM links against the system OpenMP runtime (libgomp.so.1). Older
    appliance images lack the `libgomp1` package (added to the Dockerfile);
    until the image is rebuilt, load the copy scikit-learn bundles under the
    soname LightGBM asks for, so `import lightgbm` works in every service."""
    import ctypes
    import glob
    import os
    import tempfile

    try:
        ctypes.CDLL("libgomp.so.1", mode=ctypes.RTLD_GLOBAL)
        return
    except OSError:
        pass
    try:
        import sysconfig

        hits = sorted(glob.glob(os.path.join(sysconfig.get_paths()["purelib"], "scikit_learn.libs", "libgomp-*.so*")))
        if not hits:
            return
        src = hits[0]
        old = os.path.basename(src).encode()
        new = b"libgomp.so.1"
        dst_dir = os.path.join(tempfile.gettempdir(), f"bsp_libgomp_{os.getuid()}")
        dst = os.path.join(dst_dir, "libgomp.so.1")
        if not os.path.exists(dst):
            data = open(src, "rb").read()
            if data.count(old) != 1 or len(new) > len(old):
                return
            os.makedirs(dst_dir, exist_ok=True)
            tmp = dst + f".{os.getpid()}"
            with open(tmp, "wb") as fh:
                fh.write(data.replace(old, new.ljust(len(old), b"\0")))   # patch DT_SONAME in place
            os.replace(tmp, dst)
        ctypes.CDLL(dst, mode=ctypes.RTLD_GLOBAL)
    except Exception:   # never block the package import over an optional accelerator
        pass


_ensure_libgomp()
