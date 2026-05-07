from providers.base import AssistantMessage, Provider, ToolCall


def get_provider(name: str, *, model_override: str | None = None) -> Provider:
    """Build a Provider for the given name, reading credentials from config.

    `model_override` lets the caller (e.g. the escalation tool) pin a
    specific model regardless of the per-provider default.
    """
    from config import get_config

    cfg = get_config()
    if name == "ollama":
        from providers.ollama import OllamaProvider
        return OllamaProvider(url=cfg.ollama_url, model=model_override or cfg.model)
    if name == "openai":
        from providers.openai_provider import OpenAIProvider
        if not cfg.openai_api_key:
            raise ValueError("openai_api_key is not configured")
        return OpenAIProvider(api_key=cfg.openai_api_key, model=model_override or cfg.openai_model)
    if name == "anthropic":
        from providers.anthropic_provider import AnthropicProvider
        if not cfg.anthropic_api_key:
            raise ValueError("anthropic_api_key is not configured")
        return AnthropicProvider(
            api_key=cfg.anthropic_api_key,
            model=model_override or cfg.anthropic_model,
        )
    raise ValueError(f"unknown provider: {name!r}")


__all__ = ["AssistantMessage", "Provider", "ToolCall", "get_provider"]
