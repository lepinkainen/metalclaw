import pytest
import yaml

import config

_ENV_VARS = (
    "FASTMAIL_API_TOKEN",
    "OLLAMA_URL",
    "DISCORD_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "WILMA_URL",
    "WILMA_USERNAME",
    "WILMA_PASSWORD",
    "WILMA_TOTP_SECRET",
)


@pytest.fixture
def clear_env(monkeypatch):
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def cfg_file(tmp_path, monkeypatch, clear_env):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("METALCLAW_CONFIG", str(path))
    config.reset_cache()
    yield path
    config.reset_cache()


@pytest.fixture
def write_config():
    def _write(path, **fields):
        data = {"vault_path": str(path.parent / "vault")}
        data.update(fields)
        path.write_text(yaml.safe_dump(data))

    return _write


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Keep history.db / state files out of the real XDG data dir."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Point every test at a minimal throwaway config.

    Without this, any test that reaches ``get_config()`` (the chat loop reads
    ``max_tool_iterations`` on every turn) silently depends on the developer's
    ``./config.yaml`` and fails on CI, where none exists. Tests that need
    specific fields use ``cfg_file`` + ``write_config``, which override the
    same env var; ``test_config`` deletes it where it tests the search order.
    """
    path = tmp_path / "_default-config.yaml"
    path.write_text(yaml.safe_dump({"vault_path": str(tmp_path / "_default-vault")}))
    monkeypatch.setenv("METALCLAW_CONFIG", str(path))
    config.reset_cache()
    yield
    config.reset_cache()


@pytest.fixture(autouse=True)
def _default_config(tmp_path, monkeypatch):
    """Give every test a minimal valid config so get_config() never reads
    the developer's real ~/.config/metalclaw/config.yaml (or fails on CI,
    where no config exists). Tests needing specific fields override by
    pointing METALCLAW_CONFIG elsewhere (e.g. via ``cfg_file``)."""
    path = tmp_path / "default-config.yaml"
    path.write_text(yaml.safe_dump({"vault_path": str(tmp_path / "vault")}))
    monkeypatch.setenv("METALCLAW_CONFIG", str(path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    config.reset_cache()
    yield
    config.reset_cache()
