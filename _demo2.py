"""组长↔组员互动演示 v2（表格化组长卡；所有收发对象都是用户本人）。

机器人「12」：发表格化组长卡给用户 → 用户点某行「提醒TA」→ 组员卡发给用户
（组员 open_id 重定向到用户）→ 用户点「已收到/已修正」→ 记录并原地更新。
"""

from __future__ import annotations

import asyncio
import json
import os

from lark_channel import FeishuChannel, PolicyConfig

import feishu_member_todo_card as MC
import feishu_mentor_check_reminder as M

APP_ID = os.environ["PSI_FEISHU_APP_ID"]
APP_SECRET = os.environ["PSI_FEISHU_APP_SECRET"]
USER = os.environ.get("PSI_FEISHU_USER_OPEN_ID", "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")  # 所有收发对象都是你
TODO_URL = os.environ.get("PSI_FEISHU_TODO_URL", "https://example.feishu.cn/wiki/xxxxxxxxxxxxxxxxxxxxxxxxxxxx")
ROOT = "examples/haitun-workspace"


def _mk(event):
    op = getattr(event, "operator", None)
    op_id = getattr(op, "open_id", "") or ""
    msg_id = getattr(event, "message_id", "") or ""
    action = getattr(event, "action", None)
    value = getattr(action, "value", None)
    if value is None:
        raw = getattr(event, "raw", None)
        ev = raw.get("event") if isinstance(raw, dict) else None
        ra = ev.get("action") if isinstance(ev, dict) else None
        value = ra.get("value") if isinstance(ra, dict) else None
    return value, {"action": {"value": value}, "message_id": msg_id, "operator": {"open_id": op_id}}, op_id


async def main() -> None:
    channel = FeishuChannel(app_id=APP_ID, app_secret=APP_SECRET,
                            policy=PolicyConfig(require_mention=False))

    async def on_card_action(event) -> None:
        try:
            value, payload, op_id = _mk(event)
            act = value.get("action") if isinstance(value, dict) else ""
            print(f"[CLICK] {act} :: {value}", flush=True)
            if act == "notify_member":
                out = await MC.feishu_member_todo_card(
                    receive_id=USER, member_name=value.get("member_name", ""),
                    todo_list_url=TODO_URL, from_mentor=value.get("mentor_name", ""),
                    with_feedback=True, agent_root=ROOT)
                print("[→组员卡已发(给你)]", out, flush=True)
            elif act in ("member_ack", "member_fixed"):
                out = await MC.feishu_member_todo_card(
                    card_action_json=json.dumps(payload, ensure_ascii=False),
                    user_key=op_id, todo_list_url=TODO_URL, agent_root=ROOT)
                print("[组员反馈已记录]", out, flush=True)
            elif act == "mentor_check_done":
                out = await M.feishu_mentor_check_reminder(
                    card_action_json=json.dumps(payload, ensure_ascii=False),
                    user_key=op_id, todo_list_url=TODO_URL, agent_root=ROOT)
                print("[组长卡已确认]", out, flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[ERR] {e!r}", flush=True)

    channel.on("cardAction", on_card_action)
    await channel.start_background()
    print("[READY] bot 12 connected", flush=True)

    r = await M.feishu_mentor_check_reminder(
        mentor_open_id=USER, mentor_name=os.environ.get("PSI_FEISHU_MENTOR_NAME", "组长"),
        todo_list_url=TODO_URL, agent_root=ROOT)
    print("[表格化组长卡 SENT]", r, flush=True)

    await asyncio.sleep(540)
    print("[END]", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
