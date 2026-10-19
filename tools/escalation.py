import logging
from typing import Any

from pydantic import BaseModel, Field

from config import get_config
from registry import tool

log = logging.getLogger(__name__)


class _EscalateArgs(BaseModel):
    query: str = Field(description="The user's question or task, restated.")
    reason: str = Field(description="Why you are escalating instead of answering.")


@tool(
    description=(
        "Escalate to a more capable cloud model. Use ONLY when you genuinely "
        "cannot answer or the task needs reasoning beyond your capability. "
        "Pass the user's question and a brief reason. Do NOT use for trivial "
        "requests."
    ),
    args=_EscalateArgs,
)
def escalate_to_big_model(query: str, reason: str) -> dict[str, Any]:
    cfg = get_config()
    log.info("escalate_to_big_model reason=%.200s query=%.200s", reason, query)
    if not cfg.escalation_enabled:
        log.warning("escalate_to_big_model called but escalation disabled")
        return {"status": "disabled", "message": "Escalation disabled in config."}

    # Lazy import: chat_loop -> registry -> tools (this package) is a cycle
    # at import time. Only resolves once the call actually fires.
    import chat_loop
    from providers import get_provider

    snapshot = chat_loop._active_session_messages.get()
    if snapshot is None:
        sub_messages: list[dict] = [{"role": "user", "content": query}]
    else:
        sub_messages = list(snapshot)
        sub_messages.append(
            {"role": "user", "content": f"[escalation: {reason}] {query}"}
        )

    big = get_provider(cfg.escalation_provider, model_override=cfg.escalation_model)
    log.info(
        "escalate_to_big_model -> %s:%s (snapshot=%s, %d messages)",
        cfg.escalation_provider,
        cfg.escalation_model,
        snapshot is not None,
        len(sub_messages),
    )
    reply = chat_loop._chat_with_provider(
        big, sub_messages, exclude_tools={"escalate_to_big_model"}
    )
    log.info("escalate_to_big_model done reply=%d chars", len(reply))
    return {
        "status": "ok",
        "model": f"{cfg.escalation_provider}:{cfg.escalation_model}",
        "reason": reason,
        "reply": reply,
    }
