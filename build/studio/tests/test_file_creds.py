"""Launchpad credentials kept by a side stack in its own secrets file (catalog app `cred_file`)."""

import credentials
import settings


def test_datahub_password_is_read_from_the_stack_secrets_file():
    f = settings.cfg.data / "datahub" / "secrets" / "datahub.env"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("DATAHUB_DB_PASSWORD=db\nDATAHUB_ADMIN_PASSWORD=s3cret-value\n")
    assert credentials.reveal("service", "file:datahub", "DATAHUB_ADMIN_PASSWORD") == "s3cret-value"


def test_unknown_file_credential_is_refused():
    try:
        credentials.reveal("service", "file:datahub", "DATAHUB_DB_PASSWORD")
    except credentials.CredentialError:
        pass
    else:
        raise AssertionError("only the env named in the catalog may be revealed")


def test_missing_file_explains_how_to_create_it():
    f = settings.cfg.data / "datahub" / "secrets" / "datahub.env"
    if f.exists():
        f.unlink()
    try:
        credentials.reveal("service", "file:datahub", "DATAHUB_ADMIN_PASSWORD")
    except credentials.CredentialError as e:
        assert "datahub-up" in str(e)
    else:
        raise AssertionError("expected CredentialError")
