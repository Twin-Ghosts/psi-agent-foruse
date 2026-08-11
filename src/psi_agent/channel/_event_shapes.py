"""Shared helpers for turning platform event payloads into plain data.

Both the live forwarding path (``channel/feishu/_agent_events``) and the
agent-facing self-check tool (``channel_event_check``) use these, so what an
agent sees when probing a mapper matches what actually happens at runtime.
"""

from __future__ import annotations

from typing import Any

_MAX_DEPTH = 12
_SUMMARY_MAX_KEYS = 24


def plainify(obj: Any, _depth: int = 0) -> Any:
    """Recursively convert SDK model objects into plain dicts/lists.

    Lark's generated models expose no ``dict()``/``model_dump()``/``to_dict()``,
    so a naive ``repr()`` fallback hides every field from ``map_event``. Walk
    ``__dict__`` instead, dropping private attributes and capping depth so a
    self-referencing object cannot loop forever.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if _depth >= _MAX_DEPTH:
        return repr(obj)
    if isinstance(obj, dict):
        return {str(k): plainify(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [plainify(v, _depth + 1) for v in obj]
    inner = getattr(obj, "__dict__", None)
    if isinstance(inner, dict):
        return {k: plainify(v, _depth + 1) for k, v in inner.items() if not k.startswith("_")}
    return repr(obj)


def describe_shape(value: Any, _depth: int = 0) -> str:
    """Render a compact key map of *value*, for diagnostics.

    Shows which keys exist and where nesting sits, without dumping user
    content: an agent that wrote ``event["chat_id"]`` when the field really
    lives at ``event["message"]["chat_id"]`` can see that from the shape alone.
    """
    if _depth > 4:
        return "…"
    if isinstance(value, dict):
        if not value:
            return "{}"
        keys = sorted(value)[:_SUMMARY_MAX_KEYS]
        parts = []
        for key in keys:
            child = value[key]
            if isinstance(child, dict) and child:
                parts.append(f"{key}{{{describe_shape(child, _depth + 1)}}}")
            elif isinstance(child, list) and child:
                parts.append(f"{key}[{describe_shape(child[0], _depth + 1)}]")
            elif child is None:
                parts.append(f"{key}=None")
            else:
                parts.append(key)
        if len(value) > len(keys):
            parts.append(f"…+{len(value) - len(keys)}")
        return ", ".join(parts)
    if isinstance(value, list):
        return f"[{describe_shape(value[0], _depth + 1)}]" if value else "[]"
    return type(value).__name__


def non_null_paths(value: Any, prefix: str = "", _depth: int = 0) -> list[str]:
    """List dotted paths that actually hold a scalar value.

    This is the "where do I read the field from" answer: a mapper author can
    compare the path they used against the paths the event really provides.
    """
    if _depth > 5:
        return []
    found: list[str] = []
    if isinstance(value, dict):
        for key in sorted(value):
            found.extend(non_null_paths(value[key], f"{prefix}.{key}" if prefix else key, _depth + 1))
    elif isinstance(value, list):
        if value:
            found.extend(non_null_paths(value[0], f"{prefix}[0]", _depth + 1))
    elif value is not None and value != "":
        found.append(prefix)
    return found
