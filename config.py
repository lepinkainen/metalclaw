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


_VALID_PROVIDERS = ("ollama", "openai", "anthropic")


@dataclass(frozen=True)
class Config:
    vault_path: Path
    memory_subdir: str
    fastmail_api_token: str | None
    telegram_bot_token: str | None
    discord_bot_token: str | None
    discord_chat_channels: tuple[int, ...]
    discord_heartbeat_channel: int | None
    ollama_url: str
    model: str
    provider: str
    openai_api_key: str | None
    openai_model: str
    anthropic_api_key: str | None
    anthropic_model: str
    escalation_enabled: bool
    escalation_provider: str
    escalation_model: str | None
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
    "provider": "ollama",
    "openai_model": "gpt-4o-mini",
    "anthropic_model": "claude-haiku-4-5",
    "escalation_enabled": False,
    "escalation_provider": "anthropic",
    "escalation_model": None,
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


def _resolve_provider_model(provider: str, raw: dict, fallback_model: str) -> str | None:
    """Per-provider model lookup. None when no model is configured."""
    if provider == "ollama":
        return fallback_model
    if provider == "openai":
        return raw.get("openai_model") or _DEFAULTS["openai_model"]
    if provider == "anthropic":
        return raw.get("anthropic_model") or _DEFAULTS["anthropic_model"]
    return None


def _require_key_for(provider: str, openai_key: str | None, anthropic_key: str | None) -> None:
    if provider == "openai" and not openai_key:
        raise ValueError(
            "openai_api_key missing — set OPENAI_API_KEY env or openai_api_key in config.yaml"
        )
    if provider == "anthropic" and not anthropic_key:
        raise ValueError(
            "anthropic_api_key missing — set ANTHROPIC_API_KEY env or anthropic_api_key in config.yaml"
        )


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
    discord_token = os.environ.get("DISCORD_BOT_TOKEN") or raw.get("discord_bot_token")
    ollama_url = os.environ.get("OLLAMA_URL") or raw.get("ollama_url") or _DEFAULTS["ollama_url"]
    openai_key = os.environ.get("OPENAI_API_KEY") or raw.get("openai_api_key")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY") or raw.get("anthropic_api_key")

    provider = raw.get("provider") or _DEFAULTS["provider"]
    if provider not in _VALID_PROVIDERS:
        raise ValueError(f"provider must be one of {_VALID_PROVIDERS}, got {provider!r}")

    escalation_enabled = bool(raw.get("escalation_enabled", _DEFAULTS["escalation_enabled"]))
    escalation_provider = raw.get("escalation_provider") or _DEFAULTS["escalation_provider"]
    if escalation_provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"escalation_provider must be one of {_VALID_PROVIDERS}, got {escalation_provider!r}"
        )
    escalation_model = raw.get("escalation_model") or _resolve_provider_model(
        escalation_provider, raw, fallback_model=raw.get("model") or _DEFAULTS["model"]
    )

    _require_key_for(provider, openai_key, anthropic_key)
    if escalation_enabled:
        _require_key_for(escalation_provider, openai_key, anthropic_key)

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

    chat_channels_raw = raw.get("discord_chat_channels") or ()
    if isinstance(chat_channels_raw, (list, tuple)):
        try:
            discord_chat_channels = tuple(int(c) for c in chat_channels_raw)
        except (TypeError, ValueError) as e:
            raise ValueError(
                "discord_chat_channels must be a list of integer channel IDs"
            ) from e
    else:
        raise ValueError("discord_chat_channels must be a list of integer channel IDs")

    discord_heartbeat_channel_raw = raw.get("discord_heartbeat_channel")
    if discord_heartbeat_channel_raw is None:
        discord_heartbeat_channel: int | None = None
    else:
        try:
            discord_heartbeat_channel = int(discord_heartbeat_channel_raw)
        except (TypeError, ValueError) as e:
            raise ValueError(
                "discord_heartbeat_channel must be an integer channel ID"
            ) from e

    return Config(
        vault_path=Path(vault_path_str).expanduser(),
        memory_subdir=raw.get("memory_subdir") or _DEFAULTS["memory_subdir"],
        fastmail_api_token=fastmail_token,
        telegram_bot_token=telegram_token,
        discord_bot_token=discord_token,
        discord_chat_channels=discord_chat_channels,
        discord_heartbeat_channel=discord_heartbeat_channel,
        ollama_url=ollama_url,
        model=raw.get("model") or _DEFAULTS["model"],
        provider=provider,
        openai_api_key=openai_key,
        openai_model=raw.get("openai_model") or _DEFAULTS["openai_model"],
        anthropic_api_key=anthropic_key,
        anthropic_model=raw.get("anthropic_model") or _DEFAULTS["anthropic_model"],
        escalation_enabled=escalation_enabled,
        escalation_provider=escalation_provider,
        escalation_model=escalation_model,
        heartbeat_enabled=bool(heartbeat_enabled),
        heartbeat_interval_seconds=heartbeat_interval,
        heartbeat_active_hours=active_hours,
        vault_search_excludes=excludes,
    )


def reset_cache() -> None:
    """Clear cached config — used by tests after mutating env."""
    get_config.cache_clear()
