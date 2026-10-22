"""Vault: AES-256-GCM + scrypt format, lock/unlock, verify, legacy Fernet migration."""

import json
import os
import stat

from cryptography.fernet import Fernet

import cryptobox
import settings
from vault import VaultLocked, VaultStore, merge_legacy, store

PW = "correct horse battery staple"


def test_initialize_writes_v2_envelope(project):
    settings.reload()
    store.initialize(PW)
    doc = json.loads((settings.cfg.vault_dir / "vault.json").read_text())
    assert doc["v"] == 2 and doc["kdf"] == "scrypt" and (doc["n"], doc["r"], doc["p"]) == (32768, 8, 1)
    assert {"salt", "nonce", "ct"} <= set(doc)
    assert stat.S_IMODE(os.stat(settings.cfg.vault_dir / "vault.json").st_mode) == 0o600
    assert PW not in (settings.cfg.vault_dir / "vault.json").read_text()


def test_set_get_delete_persist():
    store.set("services.postgres.PG_PASS", "s3cret-value")
    store.set("services.vendor-api.VENDOR_API_KEY", "x")
    assert store.get("services.postgres.PG_PASS") == "s3cret-value"
    assert store.get("services.nope.X", "dflt") == "dflt"
    store.delete("services.vendor-api.VENDOR_API_KEY")
    assert store.get("services.vendor-api.VENDOR_API_KEY") is None
    # an independent store decrypts what was persisted
    other = VaultStore()
    other.unlock(PW)
    assert other.get("services.postgres.PG_PASS") == "s3cret-value"


def test_nonce_changes_every_write():
    before = json.loads((settings.cfg.vault_dir / "vault.json").read_text())["nonce"]
    store.set("meta.touch", 1)
    after = json.loads((settings.cfg.vault_dir / "vault.json").read_text())["nonce"]
    assert before != after


def test_lock_and_wrong_password():
    store.lock()
    assert store.status() == "locked"
    try:
        store.get("services")
        raise AssertionError("expected VaultLocked")
    except VaultLocked:
        pass
    try:
        store.unlock("wrong passphrase!!")
        raise AssertionError("expected DecryptError")
    except cryptobox.DecryptError:
        pass
    assert store.verify(PW) and not store.verify("nope nope nope")
    store.unlock(PW)
    assert store.verify(PW) and not store.verify("nope nope nope")


def test_tamper_detected():
    path = settings.cfg.vault_dir / "vault.json"
    doc = json.loads(path.read_text())
    raw = bytearray(cryptobox.b64d(doc["ct"]))
    raw[3] ^= 1
    bad = dict(doc, ct=cryptobox.b64e(bytes(raw)))
    try:
        cryptobox.unseal(bad, PW)
        raise AssertionError("tampering not detected")
    except cryptobox.DecryptError:
        pass


def test_change_password():
    store.change_password("another long passphrase")
    store.lock()
    store.unlock("another long passphrase")
    assert store.get("services.postgres.PG_PASS") == "s3cret-value"
    store.change_password(PW)


def test_auto_unlock_from_env_and_file(project):
    store.lock()
    os.environ["APPLIANCE_MASTER_PASSWORD"] = PW
    assert store.auto_unlock() and store.source == "env"
    del os.environ["APPLIANCE_MASTER_PASSWORD"]
    store.lock()
    pwfile = project / "mp.txt"
    pwfile.write_text(PW + "\n")
    os.environ["APPLIANCE_MASTER_PASSWORD_FILE"] = str(pwfile)
    assert store.auto_unlock()
    del os.environ["APPLIANCE_MASTER_PASSWORD_FILE"]


def test_merge_legacy_mapping():
    data = {"services": {"postgres": {"PG_PASS": "keep-me"}}}
    old = {
        "marimo": {"MARIMO_PASSWORD": "m1", "username": "analyst"},
        "jupyter": {"JUPYTER_TOKEN": "j1"},
        "vscode": {"CODE_PASSWORD": "c1"},
        "rustfs": {"RUSTFS_SECRET_KEY": "r1", "username": "admin"},
        "postgres": {"PG_PASS": "old-pg"},
        "metastore": {"META_PASS": "meta"},
        "sqlserver": {"ODBC_PASS": "Sql-1", "username": "sa"},
        "datahub": {"DATAHUB_SYSTEM_CLIENT_SECRET": "d1", "DATAHUB_FRONTEND_SECRET": "d2"},
        "warehouse2": {"WH2_PASSWORD": "snow"},
        "superset": {"SUPERSET_ADMIN_PASS": "changeme", "SUPERSET_SECRET_KEY": "changeme-superset-insecure"},
        "vendors": {"VENDOR_API_KEY": "", "note": 42},
    }
    notes = merge_legacy(data, old)
    s = data["services"]
    # v1 wizard keys land in the slot of the catalog service that owns the env
    assert s["appliance"] == {"JUPYTER_TOKEN": "j1"}
    assert s["code"] == {"CODE_PASSWORD": "c1"}
    assert s["postgres"] == {"PG_PASS": "keep-me"}, "existing v2 values win"
    assert set(s) == {"postgres", "appliance", "code"}, "no slots for services the catalog dropped"
    assert "superset" not in s, "placeholders are not migrated"
    assert "vendor-api" not in s, "empty / non-string values are skipped"
    # values with no catalog owner are preserved under legacy.<v1 key>
    leg = data["legacy"]
    assert leg["marimo"] == {"MARIMO_PASSWORD": "m1"}, "no username mapping for marimo"
    assert leg["rustfs"] == {"RUSTFS_SECRET_KEY": "r1", "RUSTFS_ACCESS_KEY": "admin"}, "username → its v1 user env"
    assert leg["metastore"] == {"META_PASS": "meta"}
    assert leg["sqlserver"] == {"ODBC_PASS": "Sql-1", "ODBC_USER": "sa"}
    assert leg["datahub"] == {"DATAHUB_SYSTEM_CLIENT_SECRET": "d1", "DATAHUB_FRONTEND_SECRET": "d2"}
    assert leg["warehouse2"] == {"WH2_PASSWORD": "snow"}
    assert set(leg) == {"marimo", "rustfs", "metastore", "sqlserver", "datahub", "warehouse2"}
    assert "JUPYTER_TOKEN → services.appliance" in notes and "warehouse2.WH2_PASSWORD kept under legacy.warehouse2" in notes
    assert not any("PG_PASS" in n for n in notes), "kept v2 value produces no note"


def test_legacy_files_migrated_on_unlock(project):
    vdir = settings.cfg.vault_dir
    key = Fernet.generate_key()
    (vdir / "master.key").write_bytes(key)
    (vdir / "secrets.enc").write_bytes(Fernet(key).encrypt(json.dumps(
        {"services": {"code": {"CODE_PASSWORD": "code-pw"}, "trino": {"TRINO_PASSWORD": "tr"}}}).encode()))
    assert store.get("services.code.CODE_PASSWORD") is None
    store.lock()
    notes = store.unlock(PW)
    assert any("migrated" in n for n in notes)
    assert store.get("services.code.CODE_PASSWORD") == "code-pw"
    assert store.get("services.trino") is None, "trino is no longer in the catalog"
    assert store.get("legacy.trino.TRINO_PASSWORD") == "tr", "but its value is preserved"
    assert any("legacy.trino" in n for n in notes)
    assert (vdir / "master.key.migrated").exists() and (vdir / "secrets.enc.migrated").exists()
    assert not (vdir / "master.key").exists()


def test_legacy_migrated_on_initialize(project):
    vdir = settings.cfg.vault_dir
    (vdir / "vault.json").unlink()
    key = Fernet.generate_key()
    (vdir / "master.key").write_bytes(key)
    (vdir / "secrets.enc").write_bytes(Fernet(key).encrypt(json.dumps({"services": {
        "minio": {"RUSTFS_SECRET_KEY": "m", "username": "minioadmin"},
        "superset": {"SUPERSET_ADMIN_PASS": "Sup-1-real"}}}).encode()))
    store.lock()
    notes = store.initialize("brand new passphrase")
    assert any("migrated" in n for n in notes)
    assert store.get("services.superset.SUPERSET_ADMIN_PASS") == "Sup-1-real"
    assert store.get("services.rustfs") is None
    assert store.get("legacy.minio") == {"RUSTFS_SECRET_KEY": "m", "RUSTFS_ACCESS_KEY": "minioadmin"}
    assert (vdir / "secrets.enc.migrated").exists() and not (vdir / "secrets.enc").exists()


def test_min_master_length():
    store.lock()
    (settings.cfg.vault_dir / "vault.json").unlink()
    try:
        store.initialize("short")
        raise AssertionError("short passphrase accepted")
    except ValueError:
        pass


def test_env_bootstrap_seeds_empty_shared_slots_once():
    import credentials  # noqa: F401 — registers the seed hook on store.on_open

    settings.reload()
    proj = settings.cfg.project
    key = '{\n  "type": "service_account",\n  "private_key": "-----BEGIN KEY-----\\nabc\\n-----END KEY-----\\n"\n}'
    (proj / "sa.json").write_text(key)
    (proj / ".env").write_text("GCP_SA_JSON_FILE=./sa.json\nM365_CLIENT_SECRET=from-env-123\n")
    settings.reload()
    store.lock()
    (settings.cfg.vault_dir / "vault.json").unlink(missing_ok=True)
    notes = store.initialize(PW)   # first-run setup seeds too
    assert store.get("services.shared.GCP_SA_JSON") == key.strip(), "multi-line JSON copied verbatim"
    assert store.get("services.shared.M365_CLIENT_SECRET") == "from-env-123"
    assert any("seeded from .env" in n for n in notes) and "from-env-123" not in " ".join(notes)
    # an edit made in Studio wins over .env from then on
    store.set("services.shared.M365_CLIENT_SECRET", "edited-in-studio")
    store.lock()
    store.unlock(PW)
    assert store.get("services.shared.M365_CLIENT_SECRET") == "edited-in-studio"



def test_auto_mode_key_file_unlocks_without_passphrase():
    """VAULT_MODE=auto: a fresh appliance creates vault/master.key (0600) and
    the vault opens from it — encrypted at rest, no passphrase anywhere."""
    import vault as vault_mod

    os.environ["VAULT_MODE"] = "auto"
    settings.reload()
    try:
        store.lock()
        (settings.cfg.vault_dir / "vault.json").unlink(missing_ok=True)
        (settings.cfg.vault_dir / "master.key").unlink(missing_ok=True)
        assert vault_mod.auto_mode()
        key = vault_mod.env_password()
        kf = settings.cfg.vault_dir / "master.key"
        assert len(key) >= 40 and kf.read_text() == key
        assert stat.S_IMODE(os.stat(kf).st_mode) == 0o600
        assert store.auto_unlock() and store.is_unlocked()
        store.set("services.postgres.PG_PASS", "kept-across-restarts")
        doc = (settings.cfg.vault_dir / "vault.json").read_text()
        assert "kept-across-restarts" not in doc, "still encrypted at rest"
        store.lock()
        assert store.auto_unlock() and store.get("services.postgres.PG_PASS") == "kept-across-restarts"
    finally:
        os.environ["VAULT_MODE"] = "passphrase"
        settings.reload()
