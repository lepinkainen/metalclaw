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

`raw` shape is provider-specific. The chat loop treats history as opaque — only the active provider parses it.

## Factory (`providers/__init__.py:4`)

```python
get_provider(name, *, model_override=None) -> Provider
```

- `"ollama"` → reads `cfg.ollama_url`, `cfg.model` (or override).
- `"openai"` → requires `cfg.openai_api_key`; uses `cfg.openai_model`.
- `"anthropic"` → requires `cfg.anthropic_api_key`; uses `cfg.anthropic_model`.
- Raises `ValueError` on unknown name or missing api key.

## OllamaProvider (`providers/ollama.py`)

- Endpoint: `cfg.ollama_url` (default `http://localhost:11434/api/chat`).
- httpx client: module-level `_CLIENT = httpx.Client(timeout=120.0)`.
- Request: `{"model", "messages": [system?, *history], "tools": tools or None, "stream": False}`.
- Response: `response.json()["message"]`. If `tool_calls` arrives as a JSON string (some Ollama builds), `json.loads` it (`ollama.py:46`).
- `raw` = the entire `message` dict.
- `format_tool_results` → `[{"role":"tool", "name": call.name, "content": result_json}, ...]`.

## OpenAIProvider (`providers/openai_provider.py`)

- Uses official `openai.OpenAI(api_key=...)` SDK.
- `chat.completions.create(model, messages, tools=tools or omitted)`.
- Reconstructs `raw["tool_calls"]` with stringified `arguments` to round-trip cleanly.
- `format_tool_results` → `{"role":"tool", "tool_call_id": call.id, "content": result_json}` per result.

## AnthropicProvider (`providers/anthropic_provider.py`)

- Uses official `anthropic.Anthropic(api_key=...)` SDK.
- `_MAX_TOKENS = 4096`.
- `_to_anthropic_tools` translates registry schema (`type=function, function={...}`) to Anthropic's `{name, description, input_schema}`.
- `system` is a **separate kwarg**, not in `messages`.
- Response blocks: iterates `resp.content`; collects `text` and `tool_use` blocks. `raw = {"role":"assistant","content": raw_blocks}`.
- `format_tool_results` returns a **single** entry: `[{"role":"user", "content": [{"type":"tool_result","tool_use_id","content"}, ...]}]`. **Different shape** from Ollama/OpenAI.

## Adding a provider

1. Create `providers/<name>.py` exposing class with `.name` and the two methods.
2. Add config fields (`<name>_api_key`, `<name>_model`).
3. Add branch in `get_provider`.
4. Update `Config._resolve_and_check` provider key/model maps if the provider needs an api key.
5. Update `Provider` Literal in `config.py:39`.
6. Add tests in `tests/test_providers.py`.
