# providers

## Protocol (`providers/base.py`)

```python
@dataclass
class ToolCall: id: str; name: str; arguments: dict

@dataclass
class AssistantMessage:
    text: str
    tool_calls: list[ToolCall] = []
    raw: dict | list[dict] | None = None   # appended to history verbatim

class Provider(Protocol):
    name: str
    def chat_once(messages: list[dict], tools: list[dict], system: str) -> AssistantMessage: ...
    def format_tool_results(results: list[tuple[ToolCall, str]]) -> list[dict]: ...
```

`raw` shape is provider-specific. The chat loop treats history as opaque — only the active provider parses it. `chat_loop._session_providers` stamps each session and drops tainted history when the active provider switches mid-session (`chat_loop.py:248-277`).

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
- Request: `{"model", "messages": [system?, *history], "tools": tools or None, "stream": False}`.
- Response: `response.json()["message"]`. If `tool_calls` arrives as a JSON string (some Ollama builds), `json.loads` it (`ollama.py:46`).
- `raw` = the entire `message` dict.
- `format_tool_results` → `[{"role":"tool", "name": call.name, "content": result_json}, ...]`.

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
  - `raw` shape mirrors `OllamaProvider` / OpenAI envelope: `{"role": "assistant", "content": ..., "tool_calls": [...]?}`.
- `format_tool_results` → `[{"role":"tool", "tool_call_id": call.id, "content": result_json}, ...]` (OpenAI envelope).

## Adding a model

No code change. Set `litellm_model` in `config.yaml` (or `escalation_model`) to any litellm-supported string. Verify the underlying model supports tool calling before promoting it to a working slot — Bedrock Llama variants, e.g., have partial/no tool-call support.

## Adding a non-litellm provider

1. Create `providers/<name>.py` exposing class with `.name` and the two methods.
2. Add config field if a non-credential knob is needed (model name typically lives in `provider`-specific yaml field or as an extra method param).
3. Add branch in `get_provider`.
4. Update `Provider` Literal in `config.py:38`.
5. Add tests in `tests/`.
