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
    if name == "litellm":
        from providers.litellm_provider import LiteLLMProvider
        return LiteLLMProvider(
            model=model_override or cfg.litellm_model,
            aws_region=cfg.aws_region,
            aws_profile=cfg.aws_profile,
        )
    raise ValueError(f"unknown provider: {name!r}")


__all__ = ["AssistantMessage", "Provider", "ToolCall", "get_provider"]
