import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

log = logging.getLogger(__name__)

_EMPTY_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}, "required": []}


@dataclass
class Tool:
    func: Callable[..., Any]
    schema: dict[str, Any]
    args_model: type[BaseModel] | None = None


TOOLS: dict[str, Tool] = {}


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """Replace every ``{"$ref": "#/$defs/X"}`` with a deep copy of ``defs[X]``.

    Providers receive a single self-contained JSON schema per tool; pydantic's
    nested-model output uses ``$defs`` + ``$ref`` which many backends reject
    and which local models simply never see. Sibling keys on the ``$ref``
    node (e.g. ``description``) are preserved and override the definition's.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = defs.get(ref.removeprefix("#/$defs/"))
            if target is not None:
                merged = dict(_inline_refs(target, defs))
                merged.update({k: v for k, v in node.items() if k != "$ref"})
                return merged
        return {k: _inline_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def _clean_spec(spec: dict[str, Any]) -> None:
    any_of = spec.get("anyOf")
    if isinstance(any_of, list):
        non_null = [b for b in any_of if b.get("type") != "null"]
        if len(non_null) == 1:
            spec.pop("anyOf")
            spec.update(non_null[0])
    spec.pop("title", None)
    if "default" in spec and spec["default"] is None:
        spec.pop("default")
    for sub in spec.get("properties", {}).values():
        if isinstance(sub, dict):
            _clean_spec(sub)
    items = spec.get("items")
    if isinstance(items, dict):
        _clean_spec(items)


def _schema_from_model(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs = raw.pop("$defs", None) or raw.pop("definitions", None) or {}
    raw = _inline_refs(raw, defs)
    raw.pop("title", None)
    raw.pop("$defs", None)
    raw.pop("definitions", None)
    for spec in raw.get("properties", {}).values():
        _clean_spec(spec)
    raw.setdefault("required", [])
    return raw


def tool(
    *,
    description: str,
    args: type[BaseModel] | None = None,
):
    """Register a function as a callable tool. ``args`` is a pydantic model whose
    fields define the tool's JSON schema; pass ``None`` for zero-argument tools."""
    parameters = _schema_from_model(args) if args is not None else _EMPTY_PARAMETERS

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        TOOLS[func.__name__] = Tool(
            func=func,
            schema={
                "type": "function",
                "function": {
                    "name": func.__name__,
                    "description": description,
                    "parameters": parameters,
                },
            },
            args_model=args,
        )
        log.debug(
            "registry: tool registered name=%s (total=%d)", func.__name__, len(TOOLS)
        )
        return func

    return decorator
