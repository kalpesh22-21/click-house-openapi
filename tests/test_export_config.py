"""Tests for the EXPORT_* configuration surface (app/config.py).

Covers the names+defaults contract, the fail-loud completeness gate used by the
lifespan (export_s3_missing_fields), and the Vault credential group read
(EXPORT_S3_CREDS_PATH) — the same pattern SCRATCH_CH_CREDS_PATH follows.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings, load_settings_from_vault


def _settings(**overrides) -> Settings:
    base = {
        "clickhouse_host": "localhost",
        "clickhouse_user": "default",
        "employee_access_enabled": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


class TestExportDefaults:

    def test_export_settings_names_and_defaults(self):
        s = _settings()
        assert s.export_enabled is False
        assert s.export_store == "couchbase"
        assert s.export_s3_endpoint == ""
        assert s.export_s3_bucket == ""
        assert s.export_s3_region == "us-east-1"
        assert s.export_s3_prefix == "exports"
        assert s.export_s3_access_key == ""
        assert s.export_s3_secret_key == ""
        assert s.export_s3_creds_path == ""
        assert s.export_cb_conn_str == ""
        assert s.export_cb_bucket == ""
        assert s.export_cb_username == ""
        assert s.export_cb_password == ""
        assert s.export_cb_creds_path == ""
        assert s.export_cb_chunk_rows == 10_000
        assert s.export_max_rows == 100_000
        assert s.export_part_max_bytes == 10_485_760
        assert s.export_total_max_rows == 1_000_000
        assert s.export_total_max_bytes == 104_857_600
        assert s.export_max_execution_time == 120
        assert s.export_ttl_seconds == 14_400
        assert s.export_presign_ttl_seconds == 300

    def test_export_store_validated_loudly(self):
        import pydantic

        s = _settings(export_store=" S3 ")
        assert s.export_store == "s3"  # normalised
        with pytest.raises(pydantic.ValidationError):
            _settings(export_store="gcs")


class TestExportS3CompletenessGate:

    def test_all_missing_when_unconfigured(self):
        assert _settings(export_enabled=True).export_s3_missing_fields() == [
            "EXPORT_S3_ENDPOINT",
            "EXPORT_S3_BUCKET",
            "EXPORT_S3_ACCESS_KEY",
            "EXPORT_S3_SECRET_KEY",
        ]

    def test_partial_config_names_exactly_the_missing(self):
        s = _settings(
            export_enabled=True,
            export_s3_endpoint="https://s3.example.com",
            export_s3_access_key="AKID",
        )
        assert s.export_s3_missing_fields() == ["EXPORT_S3_BUCKET", "EXPORT_S3_SECRET_KEY"]

    def test_complete_config_passes(self):
        s = _settings(
            export_enabled=True,
            export_s3_endpoint="https://s3.example.com",
            export_s3_bucket="b",
            export_s3_access_key="k",
            export_s3_secret_key="s",
        )
        assert s.export_s3_missing_fields() == []

    def test_whitespace_only_counts_as_missing(self):
        s = _settings(export_enabled=True, export_s3_bucket="   ")
        assert "EXPORT_S3_BUCKET" in s.export_s3_missing_fields()


class TestExportVaultCreds:

    def test_vault_group_populates_s3_creds(self, monkeypatch):
        fake_vault = MagicMock()
        client = MagicMock()

        def _kv(key, path):
            values = {"EXPORT_S3_ACCESS_KEY": "from-vault-ak", "EXPORT_S3_SECRET_KEY": "from-vault-sk"}
            return MagicMock(get_secret_value=lambda: values[key])

        client.read_kv_secret.side_effect = _kv
        fake_vault.get_client_using_os_environ.return_value = client
        monkeypatch.setattr("app.config.vault", fake_vault)

        env = {
            "CLICKHOUSE_HOST": "localhost",
            "CLICKHOUSE_USER": "default",
            "EMPLOYEE_ACCESS_ENABLED": "false",
            "VAULT_ENABLED": "true",
            "EXPORT_S3_CREDS_PATH": "kv/export/s3",
        }
        with patch.dict(os.environ, env):
            settings = load_settings_from_vault()

        assert settings.export_s3_access_key == "from-vault-ak"
        assert settings.export_s3_secret_key == "from-vault-sk"

    def test_env_creds_used_when_no_vault_path(self, monkeypatch):
        env = {
            "CLICKHOUSE_HOST": "localhost",
            "CLICKHOUSE_USER": "default",
            "EMPLOYEE_ACCESS_ENABLED": "false",
            "EXPORT_S3_ACCESS_KEY": "env-ak",
            "EXPORT_S3_SECRET_KEY": "env-sk",
        }
        with patch.dict(os.environ, env):
            settings = load_settings_from_vault()
        assert settings.export_s3_access_key == "env-ak"
        assert settings.export_s3_secret_key == "env-sk"


class TestExportCbCompletenessGate:

    def test_all_missing_when_unconfigured(self):
        assert _settings(export_enabled=True).export_cb_missing_fields() == [
            "EXPORT_CB_CONN_STR",
            "EXPORT_CB_BUCKET",
            "EXPORT_CB_USERNAME",
            "EXPORT_CB_PASSWORD",
        ]

    def test_partial_config_names_exactly_the_missing(self):
        s = _settings(
            export_enabled=True,
            export_cb_conn_str="couchbase://cb.example.com",
            export_cb_bucket="agent_snapshots",
        )
        assert s.export_cb_missing_fields() == ["EXPORT_CB_USERNAME", "EXPORT_CB_PASSWORD"]

    def test_complete_config_passes(self):
        s = _settings(
            export_enabled=True,
            export_cb_conn_str="couchbase://cb.example.com",
            export_cb_bucket="agent_snapshots",
            export_cb_username="u",
            export_cb_password="p",
        )
        assert s.export_cb_missing_fields() == []

    def test_whitespace_only_counts_as_missing(self):
        s = _settings(export_enabled=True, export_cb_bucket="   ")
        assert "EXPORT_CB_BUCKET" in s.export_cb_missing_fields()


class TestExportStoreGateDispatch:
    """Only the ACTIVE store's identity is gated."""

    def test_default_store_is_couchbase(self):
        assert "EXPORT_CB_CONN_STR" in _settings(export_enabled=True).export_store_missing_fields()

    def test_s3_mode_gates_only_s3_fields(self):
        s = _settings(
            export_enabled=True,
            export_store="s3",
            export_s3_endpoint="https://s3.example.com",
            export_s3_bucket="b",
            export_s3_access_key="k",
            export_s3_secret_key="s",
        )
        # Couchbase identity is entirely absent — and NOT gated in s3 mode.
        assert s.export_store_missing_fields() == []

    def test_couchbase_mode_gates_only_cb_fields(self):
        s = _settings(
            export_enabled=True,
            export_store="couchbase",
            export_cb_conn_str="couchbase://cb.example.com",
            export_cb_bucket="agent_snapshots",
            export_cb_username="u",
            export_cb_password="p",
        )
        # S3 identity is entirely absent — and NOT gated in couchbase mode.
        assert s.export_store_missing_fields() == []


class TestExportCbVaultCreds:

    def test_vault_group_populates_cb_creds(self, monkeypatch):
        fake_vault = MagicMock()
        client = MagicMock()

        def _kv(key, path):
            values = {
                "COUCHBASE_USERNAME": "from-vault-u",
                "COUCHBASE_PASSWORD": "from-vault-p",
                "COUCHBASE_CONNECTION_STRING": "couchbases://cb.vault.example.com",
                "COUCHBASE_BUCKET_EXPORT": "export_bucket_from_vault",
            }
            return MagicMock(get_secret_value=lambda: values[key])

        client.read_kv_secret.side_effect = _kv
        fake_vault.get_client_using_os_environ.return_value = client
        monkeypatch.setattr("app.config.vault", fake_vault)

        env = {
            "CLICKHOUSE_HOST": "localhost",
            "CLICKHOUSE_USER": "default",
            "EMPLOYEE_ACCESS_ENABLED": "false",
            "VAULT_ENABLED": "true",
            "EXPORT_CB_CREDS_PATH": "kv/export/cb",
        }
        with patch.dict(os.environ, env):
            settings = load_settings_from_vault()

        assert settings.export_cb_username == "from-vault-u"
        assert settings.export_cb_password == "from-vault-p"
        assert settings.export_cb_conn_str == "couchbases://cb.vault.example.com"
        assert settings.export_cb_bucket == "export_bucket_from_vault"

    def test_vault_bucket_comes_from_export_key_not_plain_bucket(self, monkeypatch):
        """COUCHBASE_BUCKET_EXPORT wins; COUCHBASE_BUCKET in the same record is ignored."""
        fake_vault = MagicMock()
        client = MagicMock()

        def _kv(key, path):
            values = {
                "COUCHBASE_USERNAME": "u",
                "COUCHBASE_PASSWORD": "p",
                "COUCHBASE_CONNECTION_STRING": "couchbases://cb.vault.example.com",
                "COUCHBASE_BUCKET": "plain_bucket_not_for_export",
                "COUCHBASE_BUCKET_EXPORT": "export_bucket_from_vault",
            }
            return MagicMock(get_secret_value=lambda: values[key])

        client.read_kv_secret.side_effect = _kv
        fake_vault.get_client_using_os_environ.return_value = client
        monkeypatch.setattr("app.config.vault", fake_vault)

        env = {
            "CLICKHOUSE_HOST": "localhost",
            "CLICKHOUSE_USER": "default",
            "EMPLOYEE_ACCESS_ENABLED": "false",
            "VAULT_ENABLED": "true",
            "EXPORT_CB_CREDS_PATH": "kv/export/cb",
        }
        with patch.dict(os.environ, env):
            settings = load_settings_from_vault()

        assert settings.export_cb_bucket == "export_bucket_from_vault"

    def test_env_cb_creds_used_when_no_vault_path(self, monkeypatch):
        env = {
            "CLICKHOUSE_HOST": "localhost",
            "CLICKHOUSE_USER": "default",
            "EMPLOYEE_ACCESS_ENABLED": "false",
            "EXPORT_CB_USERNAME": "env-u",
            "EXPORT_CB_PASSWORD": "env-p",
        }
        with patch.dict(os.environ, env):
            settings = load_settings_from_vault()
        assert settings.export_cb_username == "env-u"
        assert settings.export_cb_password == "env-p"
