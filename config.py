import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator


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


def xdg_data_dir() -> Path:
    """Return ``$XDG_DATA_HOME/metalclaw`` (or ``~/.local/share/metalclaw``), creating it."""
    xdg = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    p = Path(xdg) / "metalclaw"
    p.mkdir(parents=True, exist_ok=True)
    return p


_ENV_OVERRIDES = {
    "FASTMAIL_API_TOKEN": "fastmail_api_token",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "DISCORD_BOT_TOKEN": "discord_bot_token",
    "OLLAMA_URL": "ollama_url",
    "OPENAI_API_KEY": "openai_api_key",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
}

Provider = Literal["ollama", "openai", "anthropic"]


class Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    vault_path: Path
    memory_subdir: str = "Metalclaw/Memory"
    fastmail_api_token: str | None = None
    telegram_bot_token: str | None = None
    discord_bot_token: str | None = None
    discord_chat_channels: tuple[int, ...] = ()
    discord_heartbeat_channel: int | None = None
    ollama_url: str = "http://localhost:11434/api/chat"
    model: str = "gemma4:latest"
    provider: Provider = "ollama"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-haiku-4-5"
    escalation_enabled: bool = False
    escalation_provider: Provider = "anthropic"
    escalation_model: str | None = None
    heartbeat_enabled: bool = True
    heartbeat_interval_seconds: int = 1800
    heartbeat_active_hours: tuple[int, int] | None = None
    heartbeat_default_channel: str | None = None
    vault_search_excludes: tuple[str, ...] = ()
    allow_self_modification: bool = True

    @property
    def memory_dir(self) -> Path:
        return self.vault_path / self.memory_subdir

    @field_validator("vault_path", mode="before")
    @classmethod
    def _expand_vault_path(cls, v: Any) -> Path:
        return Path(str(v)).expanduser()

    @field_validator("discord_chat_channels", mode="before")
    @classmethod
    def _coerce_chat_channels(cls, v: Any) -> tuple[int, ...]:
        if v is None:
            return ()
        if isinstance(v, str) or not isinstance(v, (list, tuple)):
            raise ValueError("discord_chat_channels must be a list of integer channel IDs")
        try:
            return tuple(int(c) for c in v)
        except (TypeError, ValueError) as e:
            raise ValueError("discord_chat_channels must be a list of integer channel IDs") from e

    @field_validator("discord_heartbeat_channel", mode="before")
    @classmethod
    def _coerce_heartbeat_channel(cls, v: Any) -> int | None:
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError) as e:
            raise ValueError("discord_heartbeat_channel must be an integer channel ID") from e

    @field_validator("vault_search_excludes", mode="before")
    @classmethod
    def _coerce_excludes(cls, v: Any) -> tuple[str, ...]:
        if v is None:
            return ()
        if isinstance(v, str) or not isinstance(v, (list, tuple)):
            raise ValueError("vault_search_excludes must be a list of glob patterns")
        return tuple(str(e) for e in v)

    @field_validator("heartbeat_active_hours", mode="before")
    @classmethod
    def _coerce_active_hours(cls, v: Any) -> tuple[int, int] | None:
        if v is None:
            return None
        if not (isinstance(v, (list, tuple)) and len(v) == 2):
            raise ValueError("heartbeat_active_hours must be a [start_hour, end_hour] pair or null")
        return (int(v[0]), int(v[1]))

    @model_validator(mode="after")
    def _resolve_and_check(self) -> "Config":
        provider_models = {
            "ollama": self.model,
            "openai": self.openai_model,
            "anthropic": self.anthropic_model,
        }
        provider_keys = {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
        }

        def _missing(p: str) -> str:
            return (
                f"{p}_api_key missing — set {p.upper()}_API_KEY env or "
                f"{p}_api_key in config.yaml"
            )

        if self.provider in provider_keys and not provider_keys[self.provider]:
            raise ValueError(_missing(self.provider))
        if self.escalation_enabled:
            if self.escalation_provider in provider_keys and not provider_keys[self.escalation_provider]:
                raise ValueError(_missing(self.escalation_provider))

        if self.escalation_model is None:
            return self.model_copy(update={"escalation_model": provider_models[self.escalation_provider]})
        return self


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return data


def _merge_env(raw: dict[str, Any]) -> None:
    for env_name, key in _ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value:
            raw[key] = value


@lru_cache(maxsize=1)
def get_config() -> Config:
    path = _config_path()
    raw = _load_yaml(path)
    if not raw.get("vault_path"):
        raise ValueError(
            f"vault_path missing from {path}. "
            "Copy config.example.yaml to that location and edit."
        )
    _merge_env(raw)
    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        raise ValueError(str(e)) from e


def reset_cache() -> None:
    """Clear cached config — used by tests after mutating env."""
    get_config.cache_clear()
