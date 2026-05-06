import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml


def _config_path() -> Path:
    override = os.environ.get("METALCLAW_CONFIG")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(xdg) / "metalclaw" / "config.yaml"


@dataclass(frozen=True)
class Config:
    vault_path: Path
    memory_subdir: str
    fastmail_api_token: str | None
    ollama_url: str
    model: str

    @property
    def memory_dir(self) -> Path:
        return self.vault_path / self.memory_subdir


_DEFAULTS = {
    "memory_subdir": "Metalclaw/Memory",
    "ollama_url": "http://localhost:11434/api/chat",
    "model": "gemma4:latest",
}


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return data


@lru_cache(maxsize=1)
def get_config() -> Config:
    path = _config_path()
    raw = _load_yaml(path)

    vault_path_str = raw.get("vault_path")
    if not vault_path_str:
        raise ValueError(
            f"vault_path missing from {path}. "
            "Copy config.example.yaml to that location and edit."
        )

    fastmail_token = os.environ.get("FASTMAIL_API_TOKEN") or raw.get("fastmail_api_token")
    ollama_url = os.environ.get("OLLAMA_URL") or raw.get("ollama_url") or _DEFAULTS["ollama_url"]

    return Config(
        vault_path=Path(vault_path_str).expanduser(),
        memory_subdir=raw.get("memory_subdir") or _DEFAULTS["memory_subdir"],
        fastmail_api_token=fastmail_token,
        ollama_url=ollama_url,
        model=raw.get("model") or _DEFAULTS["model"],
    )


def reset_cache() -> None:
    """Clear cached config — used by tests after mutating env."""
    get_config.cache_clear()
