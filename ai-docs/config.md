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
| `wilma_url` | `str?` | None | `WILMA_URL` env. e.g. `https://jarvenpaa.inschool.fi`. |
| `wilma_username` | `str?` | None | `WILMA_USERNAME` env. |
| `wilma_password` | `str?` | None | `WILMA_PASSWORD` env. Never logged. |
| `wilma_totp_secret` | `str?` | None | `WILMA_TOTP_SECRET` env. Base32 authenticator setup key; only if Wilma two-step login is on. |
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
| `show_tool_calls` | `bool` | `True` | Live tool-call status messages (Telegram/Discord) and CLI `/tools` initial state. Per-chat `/tools on\|off` overrides in memory. |
| `message_debounce_seconds` | `float` | `1.0` | Telegram/Discord: quiet period before buffered non-command messages become ONE turn. `0` disables. Slash commands never debounced. |
| `escalation_enabled` | `bool` | `False` | Gates `escalate_to_big_model` + `/big`. |
| `escalation_provider` | provider literal | `"litellm"` | |
| `escalation_model` | `str?` | None | Defaults to per-provider model in `_resolve_and_check`. |
| `heartbeat_enabled` | `bool` | `True` | |
| `heartbeat_interval_seconds` | `int` | `1800` | Clamped to `>=30` in `run()`. |
| `heartbeat_active_hours` | `tuple[int,int]?` | None | `[start, end]` 24h local; wraparound supported. |
| `heartbeat_failure_backoff_max_seconds` | `int` | `3600` | Cap for the per-action retry backoff after failed runs/deliveries (`heartbeat.backoff_seconds`). |
| `vault_search_excludes` | `tuple[str, ...]` | `()` | Glob patterns relative to vault. |
| `mcp_servers` | `tuple[McpServer, ...]` | `()` | External MCP servers; see below. Names must be unique. |

### `McpServer` (`extra="forbid"` — typo keys fail, unlike `Config`)

| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | `str` | **required** | `^[a-z][a-z0-9_]{0,15}$`; tool prefix is `<name>_`. |
| `transport` | `"http"\|"stdio"` | **required** | http = streamable HTTP. |
| `url` | `str?` | None | Required for http, forbidden for stdio. |
| `headers` | `dict[str,str]` | `{}` | http only. `${VAR}` expanded from env at connect time in `mcp_client._transport` (`string.Template`; `$$` escapes); missing var → that server marked down with an error naming the var (never the value); bot carries on. Never logged; not stored in Config. |
| `command` / `args` / `env` | | | stdio only (`command` required). `env` substituted like headers; child gets the SDK's allow-listed env plus these. |
| `include` | `tuple[str,...]?` | None | Allowlist of remote tool names (recommended). Unknown names → warning. |
| `exclude` | `tuple[str,...]` | `()` | Denylist; mutually exclusive with `include`. |
| `hint` | `str?` | None | Appended to the system prompt while the server has tools registered. |
| `enabled` | `bool` | `True` | |
| `startup_timeout_seconds` | `float` | `10` | Handshake + list_tools; waits overlap across servers. |
| `call_timeout_seconds` | `float` | `60` | Per tool call; httpx2 read timeout is set to this + 5. |

## Env override map (`config.py:30`)

```python
_ENV_OVERRIDES = {
    "FASTMAIL_API_TOKEN": "fastmail_api_token",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "DISCORD_BOT_TOKEN": "discord_bot_token",
    "OLLAMA_URL": "ollama_url",
    "WILMA_URL": "wilma_url",
    "WILMA_USERNAME": "wilma_username",
    "WILMA_PASSWORD": "wilma_password",
    "WILMA_TOTP_SECRET": "wilma_totp_secret",
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
  - `mcp_servers` names unique.
- `McpServer._check_transport` (url/command per transport; include xor exclude).

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
