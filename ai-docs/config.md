# config

`config.py` — pydantic v2 `BaseModel` with `frozen=True`, `extra="ignore"`.

## Search order

1. `$METALCLAW_CONFIG` env var (absolute path).
2. `./config.yaml` in cwd (dev convenience; gitignored).
3. `$XDG_CONFIG_HOME/metalclaw/config.yaml` or `~/.config/metalclaw/config.yaml`.

`get_config()` is `@lru_cache(maxsize=1)`. Tests must call `config.reset_cache()` after mutating env (`tests/conftest.py:24`).

## Fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `vault_path` | `Path` | **required** | Expanded via `expanduser()`. |
| `memory_subdir` | `str` | `"Metalclaw/Memory"` | Joined to `vault_path` for `memory_dir` property. |
| `fastmail_api_token` | `str?` | None | `FASTMAIL_API_TOKEN` env override. |
| `telegram_bot_token` | `str?` | None | `TELEGRAM_BOT_TOKEN` env. Frontend skipped if unset. |
| `discord_bot_token` | `str?` | None | `DISCORD_BOT_TOKEN` env. Frontend skipped if unset. |
| `discord_chat_channels` | `tuple[int, ...]` | `()` | Channels with always-respond behavior. |
| `discord_heartbeat_channel` | `int?` | None | Single dest for all `discord-*` heartbeats. |
| `provider` | `"ollama"\|"litellm"` | `"ollama"` | Active LLM provider. |
| `ollama_url` | `str` | `"http://localhost:11434/api/chat"` | `OLLAMA_URL` env. |
| `model` | `str` | `"gemma4:latest"` | Ollama model. |
| `litellm_model` | `str` | `"bedrock/anthropic.claude-haiku-4-5"` | litellm-prefixed model id. |
| `aws_region` | `str?` | None | Threaded to litellm as `aws_region_name`. |
| `aws_profile` | `str?` | None | Threaded to litellm as `aws_profile_name`. |
| `escalation_enabled` | `bool` | `False` | Gates `escalate_to_big_model` + `/big`. |
| `escalation_provider` | provider literal | `"litellm"` | |
| `escalation_model` | `str?` | None | Defaults to per-provider model in `_resolve_and_check`. |
| `heartbeat_enabled` | `bool` | `True` | |
| `heartbeat_interval_seconds` | `int` | `1800` | Clamped to `>=30` in `run()`. |
| `heartbeat_active_hours` | `tuple[int,int]?` | None | `[start, end]` 24h local; wraparound supported. |
| `vault_search_excludes` | `tuple[str, ...]` | `()` | Glob patterns relative to vault. |

## Env override map (`config.py:30`)

```python
_ENV_OVERRIDES = {
    "FASTMAIL_API_TOKEN": "fastmail_api_token",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "DISCORD_BOT_TOKEN": "discord_bot_token",
    "OLLAMA_URL": "ollama_url",
}
```

Env wins over yaml when set + non-empty. Cloud LLM credentials (`AWS_*`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, …) are **not** in this map — litellm/boto3 read them directly.

## Validators

- `_expand_vault_path` — `Path(str(v)).expanduser()`.
- `_coerce_chat_channels` — accepts list/tuple of int-coercible values.
- `_coerce_heartbeat_channel` — int or null.
- `_coerce_excludes` — tuple of strings.
- `_coerce_active_hours` — pair → `(int, int)`.
- `_resolve_and_check` (model-level):
  - `escalation_model = None` → fill with the per-provider default (`model` for ollama, `litellm_model` for litellm).
  - No api-key checks: cloud credentials live outside metalclaw's config.

## Path helpers

- `cfg.memory_dir → vault_path / memory_subdir` (property).
- `xdg_data_dir() → $XDG_DATA_HOME/metalclaw` (or `~/.local/share/metalclaw`); creates it.

## Adding a field

1. Add to `Config` with default + type.
2. Add validator if non-trivial coercion.
3. Add `_ENV_OVERRIDES` entry if env-overridable.
4. Update `config.example.yaml`.
5. Update `_resolve_and_check` if cross-field invariant needed.
6. Add test in `tests/test_config.py`.

## Failure modes

- Missing `vault_path` → `ValueError(f"vault_path missing from {path}. ...")`.
- `extra="ignore"` swallows typos silently — careful when adding fields. Tests should assert defaults aren't accidentally renamed.
- Bedrock model strings without AWS creds in the boto3 chain fail at first `litellm.completion()` call, not at config load — no eager validation.
