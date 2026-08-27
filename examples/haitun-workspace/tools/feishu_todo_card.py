"""A TODO-list card: many rows, each tickable on its own, each linked to a Feishu task.

Distinct from ``feishu_message_send_card`` (one card, one answer) because a todo list is
consumed row by row. The single-use machinery still applies **per row**, so ticking one
row cannot be replayed while the other rows stay live.

The card is rendered through the card-DSL template (``skills/card-dsl/templates/
todo-card.xml``, see ``_todo_card_impl._build_todo_card``): the template is the single
source of truth for the card structure, and the row mechanics (per-row tick/untick
rounds, the self-contained ``card_state`` blob, 20-round handler pre-registration) live
in ``_todo_card_impl`` — the same machinery the tick/untick callbacks use, so send and
rebuild never drift.
"""

from __future__ import annotations

import json

import _feishu_impl as _f
from _todo_card_impl import _MAX_ITEMS, _build_todo_card


async def feishu_todo_card_send(
    receive_id: str,
    items_json: str,
    title: str = "今日 TODO",
    subtitle: str = "",
    shape: str = "circle",
    receive_id_type: str = "open_id",
    user_key: str = "",
) -> str:
    """Send one card listing a person's todos, each row ticked independently.

    Use this instead of ``feishu_message_send_card`` whenever the recipient must act on
    **several** items from one message (今日待办清单). Each row shows a shape marker, its
    title as a link to the matching Feishu task, an optional detail line, and its own
    「标记完成」button. Ticking a row rewrites that row as ``● ~~已完成~~`` and updates the
    card in place — the other rows keep working, which a normal card cannot do (its first
    click retires the whole card). A ticked row gains a 「撤销」button for up to 20
    tick/untick round-trips (see ``_todo_card_impl._UNDO_ROUNDS``), after which it locks
    in as done.

    Each row is dispatched to ``feishu_todo_card_tick``, so mark the underlying Feishu task
    complete there. Rows already marked ``done`` are rendered read-only and get no button.

    ``items_json`` is a JSON array (max 40) of objects::

        [{"title": "写周报", "task_guid": "abc-123", "detail": "周五 18:00 前",
          "shape": "square", "done": false, "link": "https://..."}]

    - ``title`` — the todo text (required; a blank one becomes "任务 N").
    - ``task_guid`` — the Feishu task this row links to, from
      ``POST /open-apis/task/v2/tasks``. Rendered as an applink; the task API's response
      carries no web URL, so do not wait for one.
    - ``link`` — an explicit URL that overrides the applink (use it for a doc instead).
    - ``shape`` — per-row shape: circle ○● / square □■ / diamond ◇◆ / triangle △▲ /
      star ☆★ / check ☐☑. Falls back to the card-level ``shape``.
    - ``detail`` — a second line under the title (deadline, acceptance criteria).
    - ``done`` — pre-completed rows render struck-through with no button.

    Create the Feishu tasks **before** calling this so每行都有 ``task_guid``; a row without
    one still ticks, it just is not clickable through to a task.

    Args:
        receive_id: Who gets the card — usually the doer's ``ou_...`` open_id.
        items_json: JSON array of todo objects, described above.
        title: Card header text.
        subtitle: A line above the progress counter (date, mentor, source table).
        shape: Default shape for rows that do not set their own.
        receive_id_type: Auto-detected from the id prefix; only set for a bare user_id.
        user_key: Send as this person instead of the bot. Omit for the bot's own identity.
    """
    if not isinstance(items_json, str):
        return "[Error] items_json must be a JSON string containing an array"
    try:
        raw_items = json.loads(items_json)
    except ValueError as exc:
        return f"[Error] items_json is not valid JSON: {exc}"
    if not isinstance(raw_items, list) or not raw_items:
        return "[Error] items_json must be a non-empty JSON array of todo objects"
    if len(raw_items) > _MAX_ITEMS:
        return f"[Error] too many todos ({len(raw_items)}); split into cards of at most {_MAX_ITEMS}"
    items = [item for item in raw_items if isinstance(item, dict)]
    if len(items) != len(raw_items):
        return "[Error] every item in items_json must be a JSON object"

    card, handlers = _build_todo_card(items=items, title=title, subtitle=subtitle, shape=shape)
    if not handlers:
        return "[Error] every todo is already done; nothing to send"
    result = await _f.send_card_impl(
        receive_id,
        json.dumps(card, ensure_ascii=False),
        receive_id_type,
        user_key or None,
        "{}",
        json.dumps(handlers, ensure_ascii=False),
        True,
    )
    return _f.dumps_result(result)
