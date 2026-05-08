import pytest
import yaml

import config

_ENV_VARS = (
    "FASTMAIL_API_TOKEN",
    "OLLAMA_URL",
    "DISCORD_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
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
