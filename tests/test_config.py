import pytest

from relay.config import load_config


def test_defaults():
    cfg = load_config({})
    assert cfg.initial == "blue"
    assert set(cfg.targets) == {"blue", "green"}
    assert cfg.drain_seconds == 5.0


def test_json_targets_and_validation():
    cfg = load_config({"RELAY_TARGETS": '{"a": "http://x:1", "b": "http://y:2"}', "RELAY_INITIAL_TARGET": "b"})
    assert cfg.initial == "b"
    assert cfg.targets["a"].base_url == "http://x:1"

    with pytest.raises(ValueError):
        load_config({"RELAY_TARGETS": "a=http://x:1", "RELAY_INITIAL_TARGET": "nope"})
    with pytest.raises(ValueError):
        load_config({"RELAY_TARGETS": "a"})
    with pytest.raises(ValueError):
        load_config({"RELAY_TARGETS": "a=http://x:1,a=http://y:2"})
    with pytest.raises(ValueError):
        load_config({"RELAY_DRAIN_SECONDS": "-1"})


def test_bad_generation_history_limit():
    with pytest.raises(ValueError):
        load_config({"RELAY_MAX_CLOSED_GENERATIONS": "0"})


def test_probe_path_is_normalized():
    assert load_config({"RELAY_PROBE_PATH": "ready"}).probe_path == "/ready"
    assert load_config({"RELAY_PROBE_PATH": "/healthz"}).probe_path == "/healthz"


def test_protection_defaults_off_without_keys():
    cfg = load_config({})
    assert cfg.protect_downloads is False
    assert cfg.admin_key is None
    assert cfg.grant_secret is None


def test_protection_requires_a_key():
    with pytest.raises(ValueError):
        load_config({"RELAY_PROTECT_DOWNLOADS": "1"})
    with pytest.raises(ValueError):
        load_config({"RELAY_PROTECT_DOWNLOADS": "maybe"})


def test_admin_key_enables_derived_grant_secret():
    cfg = load_config({"RELAY_PROTECT_DOWNLOADS": "1", "RELAY_ADMIN_KEY": "k"})
    assert cfg.protect_downloads is True
    assert cfg.admin_key == "k"
    # The signing secret is derived, never the admin key string itself.
    assert cfg.grant_secret and cfg.grant_secret != "k"


def test_explicit_grant_secret_and_keyless_verification():
    cfg = load_config({"RELAY_PROTECT_DOWNLOADS": "1", "RELAY_GRANT_SECRET": "s"})
    assert cfg.grant_secret == "s"
    assert cfg.admin_key is None  # verification works, issuance stays disabled
    cfg = load_config({"RELAY_ADMIN_KEY": "k"})  # key without protection is fine
    assert cfg.protect_downloads is False
    assert cfg.grant_secret is not None
