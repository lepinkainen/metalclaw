"""Watcher pipelines: cheap API poll → deterministic filters → model only when
something is left → deliver → acknowledge. One module per source; the registry
dispatches whole pipelines (never individual steps). See
``ai-docs/mail-pipeline-plan.md``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pipelines import mail
from pipelines.context import Ledger, Outcome, PipelineContext, Trace

Pipeline = Callable[[PipelineContext], Awaitable[Outcome]]

REGISTRY: dict[str, Pipeline] = {
    "mail": mail.run,
}

__all__ = ["REGISTRY", "Ledger", "Outcome", "Pipeline", "PipelineContext", "Trace"]
