import os
import stat

from app import config, vault


def test_roundtrip_and_key_file_permissions():
    token = vault.encrypt("bot123:secret")
    assert token.startswith("v1:")
    assert vault.decrypt(token) == "bot123:secret"
    assert vault.encrypt("x") != vault.encrypt("x")  # fresh nonce every time
    mode = stat.S_IMODE(os.stat(config.MASTER_KEY_FILE).st_mode)
    assert mode == 0o600


def test_env_key_overrides_file(monkeypatch):
    monkeypatch.setenv(config.MASTER_KEY_ENV, "ab" * 32)
    vault.reset_for_tests()
    try:
        assert vault.get_key() == bytes.fromhex("ab" * 32)
    finally:
        vault.reset_for_tests()
