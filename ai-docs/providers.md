# providers

## Canonical messages (`messages.py`)

```python
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None = None


@dataclass(frozen=True)
class UserMessage:
    content: str


@dataclass(frozen=True)
class AssistantMessage:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage | None(compare=False)


@dataclass(frozen=True)
class ToolResultMessage:
    call_id: str
    name: str
    content: str  # JSON-encoded


Message = UserMessage | AssistantMessage | ToolResultMessage
validate_exchange(
    assistant, results
)  # ValueError on missing / unexpected / duplicate call ids
```

Session history holds only these. `AssistantMessage.tool_calls` accepts any sequence and is stored as a tuple. `usage` is bookkeeping, never serialised, ignored by `==`.

## Protocol (`providers/base.py`)

```python
class Provider(Protocol):
    name: str

    def chat_once(
        history: Sequence[Message], tools: list[dict], system: str
    ) -> AssistantMessage: ...
```

Each implementation owns a private `_to_wire(history) -> list[dict]` (canonical → its wire shape) and parses its response into an `AssistantMessage`. No wire dict reaches the chat loop, so a session can switch provider between turns or escalate to a different provider with its full history, tool rounds included. Parsing normalises what it supports (litellm joins `text` content blocks) and raises on anything else (`ValueError` for unknown content blocks, `TypeError` for non-string content) rather than guessing.

`usage`: Ollama fills tokens from `prompt_eval_count`/`eval_count` (cost None); litellm from `resp.usage` + `completion_cost`. `chat_loop._chat_with_provider` persists it via `history.record_usage` after every `chat_once` (failures logged, never raised).

## Factory (`providers/__init__.py:4`)

```python
get_provider(name, *, model_override=None) -> Provider
```

Two branches:

- `"ollama"` → `OllamaProvider(url=cfg.ollama_url, model=model_override or cfg.model)`.
- `"litellm"` → `LiteLLMProvider(model=model_override or cfg.litellm_model, aws_region=cfg.aws_region, aws_profile=cfg.aws_profile)`.

Raises `ValueError` on unknown name.

## OllamaProvider (`providers/ollama.py`)

- Endpoint: `cfg.ollama_url` (default `http://localhost:11434/api/chat`).
- httpx client: module-level `_CLIENT = httpx.Client(timeout=120.0)`.
- Request: `{"model", "messages": [system?, *_to_wire(history)], "tools": tools or None, "stream": False}`.
- `_to_wire`: `UserMessage` → `{"role":"user","content"}`; `AssistantMessage` → `{"role":"assistant","content": text, "tool_calls": [{"function": {"name", "arguments": dict}}]?}` (no ids: Ollama does not echo them); `ToolResultMessage` → `{"role":"tool", "name", "content"}`.
- Response: `response.json()["message"]`. If tool-call `arguments` arrives as a JSON string (some Ollama builds), `json.loads` it. Missing tool-call ids get `ollama-<uuid12>` so results can be paired.

## LiteLLMProvider (`providers/litellm_provider.py`)

- Uses `litellm.completion()` — OpenAI-shaped request/response across all backends.
- `litellm.drop_params = True` (module-level): unsupported kwargs per underlying model are silently dropped, so the same provider class fronts a heterogeneous catalog (Bedrock Claude/Nova/Llama/Mistral, OpenAI GPT, Anthropic, Gemini).
- Model strings carry the routing prefix: `bedrock/anthropic.claude-haiku-4-5`, `openai/gpt-4o`, `anthropic/claude-haiku-4-5`, `gemini/gemini-1.5-pro`, etc.
- `aws_region` / `aws_profile` (optional) → threaded into completion kwargs as `aws_region_name` / `aws_profile_name`. AWS auth otherwise via boto3's standard credential chain (env, `~/.aws/credentials`, IAM role).
- Cloud LLM API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, …) read directly by litellm — not declared in `config.py`.
- `num_retries=2` covers transient `ThrottlingException` on Bedrock.
- `chat_once`:
  - System prompt placed as the first message in the list (litellm normalises Anthropic's separate-kwarg shape internally).
  - Tools passed through as-is (registry already in OpenAI tool-schema shape).
  - Tool-call `arguments` come back as JSON strings → parsed defensively (`json.loads`, falls back to `{}` on JSONDecodeError).
  - Content-block responses: `text` blocks are joined, any other block type raises `ValueError`.
- `_to_wire` (OpenAI envelope): `AssistantMessage` → `{"role":"assistant","content": text, "tool_calls": [{"id", "type":"function", "function": {"name", "arguments": json.dumps(args)}}]?}`; `ToolResultMessage` → `{"role":"tool", "tool_call_id", "content"}`.

## Adding a model

No code change. Set `litellm_model` in `config.yaml` (or `escalation_model`) to any litellm-supported string. Verify the underlying model supports tool calling before promoting it to a working slot — Bedrock Llama variants, e.g., have partial/no tool-call support.

## Adding a non-litellm provider

1. Create `providers/<name>.py` exposing a class with `.name`, `chat_once(history, tools, system)`, and a private `_to_wire(history)`. Parse the response into `messages.AssistantMessage`; never return wire dicts.
2. Add config field if a non-credential knob is needed (model name typically lives in `provider`-specific yaml field or as an extra method param).
3. Add branch in `get_provider`.
4. Update `Provider` Literal in `config.py:38`.
5. Add tests in `tests/`.
