from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel

_EMPTY_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}, "required": []}


@dataclass
class Tool:
    func: Callable[..., Any]
    schema: dict[str, Any]


TOOLS: dict[str, Tool] = {}


def _schema_from_model(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    raw.pop("title", None)
    raw.pop("$defs", None)
    raw.pop("definitions", None)
    props = raw.get("properties", {})
    for spec in props.values():
        spec.pop("title", None)
        any_of = spec.get("anyOf")
        if isinstance(any_of, list):
            non_null = [b for b in any_of if b.get("type") != "null"]
            if len(non_null) == 1:
                spec.pop("anyOf")
                spec.update(non_null[0])
        if "default" in spec and spec["default"] is None:
            spec.pop("default")
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
        )
        return func

    return decorator
