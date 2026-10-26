import pytest

from docker_health_guardian.config.loader import ConfigError, load_config


class TestLoadConfig:
    def test_missing_file_returns_all_defaults(self):
        cfg = load_config("does/not/exist.yaml")
        assert cfg.guardian.polling_interval_seconds == 15.0
        assert cfg.remediation.enabled is False

    def test_loads_values_from_yaml(self, tmp_path):
        config_file = tmp_path / "guardian.yaml"
        config_file.write_text(
            "guardian:\n  polling_interval_seconds: 30\n  mode: remediation\n"
        )
        cfg = load_config(str(config_file))
        assert cfg.guardian.polling_interval_seconds == 30
        assert cfg.guardian.mode == "remediation"

    def test_invalid_yaml_syntax_raises_config_error(self, tmp_path):
        config_file = tmp_path / "guardian.yaml"
        config_file.write_text("guardian: [unbalanced\n")
        with pytest.raises(ConfigError):
            load_config(str(config_file))

    def test_non_mapping_yaml_raises_config_error(self, tmp_path):
        config_file = tmp_path / "guardian.yaml"
        config_file.write_text("- just\n- a\n- list\n")
        with pytest.raises(ConfigError):
            load_config(str(config_file))

    def test_invalid_threshold_raises_config_error(self, tmp_path):
        config_file = tmp_path / "guardian.yaml"
        config_file.write_text("detection:\n  cpu_threshold_percent: 150\n")
        with pytest.raises(ConfigError):
            load_config(str(config_file))

    def test_negative_interval_raises_config_error(self, tmp_path):
        config_file = tmp_path / "guardian.yaml"
        config_file.write_text("guardian:\n  polling_interval_seconds: -5\n")
        with pytest.raises(ConfigError):
            load_config(str(config_file))

    def test_contradictory_remediation_settings_raise_config_error(self, tmp_path):
        config_file = tmp_path / "guardian.yaml"
        config_file.write_text(
            "remediation:\n  enabled: true\n  dry_run: false\n  allowed_actions: []\n"
        )
        with pytest.raises(ConfigError):
            load_config(str(config_file))

    def test_email_secrets_come_only_from_env_not_yaml(self, tmp_path, monkeypatch):
        config_file = tmp_path / "guardian.yaml"
        config_file.write_text(
            "notifications:\n"
            "  email:\n"
            "    enabled: true\n"
            "    smtp_host: smtp.example.test\n"
            "    from_addr: guardian@example.test\n"
            "    to_addrs: [ops@example.test]\n"
        )
        monkeypatch.setenv("GUARDIAN_SMTP_PASSWORD", "from-env-secret")
        cfg = load_config(str(config_file))
        assert cfg.notifications.email.smtp_password == "from-env-secret"

    def test_webhook_secret_comes_from_env(self, tmp_path, monkeypatch):
        config_file = tmp_path / "guardian.yaml"
        config_file.write_text(
            "notifications:\n  webhook:\n    enabled: true\n    url: http://localhost/hook\n"
        )
        monkeypatch.setenv("GUARDIAN_WEBHOOK_SECRET", "webhook-secret")
        cfg = load_config(str(config_file))
        assert cfg.notifications.webhook.secret == "webhook-secret"

    def test_malformed_notification_settings_rejected(self, tmp_path):
        config_file = tmp_path / "guardian.yaml"
        config_file.write_text("notifications:\n  email:\n    enabled: true\n")
        with pytest.raises(ConfigError):
            load_config(str(config_file))
