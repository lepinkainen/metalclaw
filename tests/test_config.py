import yaml
import pytest

import config


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("METALCLAW_CONFIG", str(path))
    monkeypatch.delenv("FASTMAIL_API_TOKEN", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    config.reset_cache()
    yield path
    config.reset_cache()


def _write(path, **fields):
    path.write_text(yaml.safe_dump(fields))


def test_loads_vault_path_and_defaults(tmp_path, cfg_file):
    _write(cfg_file, vault_path=str(tmp_path / "vault"))
    cfg = config.get_config()
    assert cfg.vault_path == tmp_path / "vault"
    assert cfg.memory_subdir == "Metalclaw/Memory"
    assert cfg.ollama_url == "http://localhost:11434/api/chat"
    assert cfg.model == "gemma4:latest"
    assert cfg.fastmail_api_token is None


def test_yaml_overrides_defaults(tmp_path, cfg_file):
    _write(
        cfg_file,
        vault_path=str(tmp_path / "vault"),
        memory_subdir="Custom/Mem",
        ollama_url="http://example/api",
        model="gemma9",
        fastmail_api_token="from-yaml",
    )
    cfg = config.get_config()
    assert cfg.memory_subdir == "Custom/Mem"
    assert cfg.ollama_url == "http://example/api"
    assert cfg.model == "gemma9"
    assert cfg.fastmail_api_token == "from-yaml"


def test_env_fastmail_token_wins_over_yaml(tmp_path, cfg_file, monkeypatch):
    _write(cfg_file, vault_path=str(tmp_path / "vault"), fastmail_api_token="from-yaml")
    monkeypatch.setenv("FASTMAIL_API_TOKEN", "from-env")
    config.reset_cache()
    assert config.get_config().fastmail_api_token == "from-env"


def test_env_ollama_url_wins_over_yaml(tmp_path, cfg_file, monkeypatch):
    _write(cfg_file, vault_path=str(tmp_path / "vault"), ollama_url="http://yaml")
    monkeypatch.setenv("OLLAMA_URL", "http://env")
    config.reset_cache()
    assert config.get_config().ollama_url == "http://env"


def test_missing_vault_path_raises(cfg_file):
    cfg_file.write_text(yaml.safe_dump({"memory_subdir": "x"}))
    with pytest.raises(ValueError, match="vault_path"):
        config.get_config()


def test_memory_dir_property(tmp_path, cfg_file):
    _write(cfg_file, vault_path=str(tmp_path / "vault"), memory_subdir="A/B")
    assert config.get_config().memory_dir == tmp_path / "vault" / "A" / "B"


def test_cwd_config_used_when_no_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("METALCLAW_CONFIG", raising=False)
    monkeypatch.delenv("FASTMAIL_API_TOKEN", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"vault_path": str(tmp_path / "vault"), "model": "from-cwd"})
    )
    config.reset_cache()
    try:
        assert config.get_config().model == "from-cwd"
    finally:
        config.reset_cache()


def test_env_override_beats_cwd_config(tmp_path, monkeypatch):
    monkeypatch.delenv("FASTMAIL_API_TOKEN", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"vault_path": str(tmp_path / "cwd-vault"), "model": "from-cwd"})
    )
    env_cfg = tmp_path / "env-config.yaml"
    env_cfg.write_text(
        yaml.safe_dump({"vault_path": str(tmp_path / "env-vault"), "model": "from-env"})
    )
    monkeypatch.setenv("METALCLAW_CONFIG", str(env_cfg))
    config.reset_cache()
    try:
        assert config.get_config().model == "from-env"
    finally:
        config.reset_cache()
