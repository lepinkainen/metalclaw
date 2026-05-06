import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml


def _config_path() -> Path:
    """Search order: METALCLAW_CONFIG env, ./config.yaml in cwd, then XDG default."""
    override = os.environ.get("METALCLAW_CONFIG")
    if override:
        return Path(override).expanduser()
    cwd_path = Path.cwd() / "config.yaml"
    if cwd_path.exists():
        return cwd_path
    xdg = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(xdg) / "metalclaw" / "config.yaml"


@dataclass(frozen=True)
class Config:
    vault_path: Path
    memory_subdir: str
    fastmail_api_token: str | None
    telegram_bot_token: str | None
    ollama_url: str
    model: str
    heartbeat_enabled: bool
    heartbeat_interval_seconds: int
    heartbeat_active_hours: tuple[int, int] | None
    vault_search_excludes: tuple[str, ...]

    @property
    def memory_dir(self) -> Path:
        return self.vault_path / self.memory_subdir


_DEFAULTS = {
    "memory_subdir": "Metalclaw/Memory",
    "ollama_url": "http://localhost:11434/api/chat",
    "model": "gemma4:latest",
    "heartbeat_enabled": True,
    "heartbeat_interval_seconds": 1800,
    "heartbeat_active_hours": None,
    "vault_search_excludes": (),
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
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN") or raw.get("telegram_bot_token")
    ollama_url = os.environ.get("OLLAMA_URL") or raw.get("ollama_url") or _DEFAULTS["ollama_url"]

    active_hours_raw = raw.get("heartbeat_active_hours", _DEFAULTS["heartbeat_active_hours"])
    active_hours: tuple[int, int] | None
    if active_hours_raw is None:
        active_hours = None
    else:
        if not (isinstance(active_hours_raw, (list, tuple)) and len(active_hours_raw) == 2):
            raise ValueError("heartbeat_active_hours must be a [start_hour, end_hour] pair or null")
        active_hours = (int(active_hours_raw[0]), int(active_hours_raw[1]))

    heartbeat_enabled = raw.get("heartbeat_enabled", _DEFAULTS["heartbeat_enabled"])
    heartbeat_interval = int(
        raw.get("heartbeat_interval_seconds", _DEFAULTS["heartbeat_interval_seconds"])
    )

    excludes_raw = raw.get("vault_search_excludes", _DEFAULTS["vault_search_excludes"])
    if excludes_raw is None:
        excludes: tuple[str, ...] = ()
    elif isinstance(excludes_raw, (list, tuple)):
        excludes = tuple(str(e) for e in excludes_raw)
    else:
        raise ValueError("vault_search_excludes must be a list of glob patterns")

    return Config(
        vault_path=Path(vault_path_str).expanduser(),
        memory_subdir=raw.get("memory_subdir") or _DEFAULTS["memory_subdir"],
        fastmail_api_token=fastmail_token,
        telegram_bot_token=telegram_token,
        ollama_url=ollama_url,
        model=raw.get("model") or _DEFAULTS["model"],
        heartbeat_enabled=bool(heartbeat_enabled),
        heartbeat_interval_seconds=heartbeat_interval,
        heartbeat_active_hours=active_hours,
        vault_search_excludes=excludes,
    )


def reset_cache() -> None:
    """Clear cached config — used by tests after mutating env."""
    get_config.cache_clear()
