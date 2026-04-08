from dataclasses import dataclass
from typing import Callable, Any


@dataclass
class Tool:
    func: Callable[..., Any]
    schema: dict[str, Any]


TOOLS: dict[str, Tool] = {}


def tool(*, description: str, parameters: dict[str, Any]):
    """Decorator that registers a function as a callable tool."""
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
