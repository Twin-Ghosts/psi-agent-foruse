"""bind-field 通用回写:把卡片交互的选中值写回多维表格对应字段。

背景:评价卡的「点分→写 mentor打分 列」原本是 _review_card_impl 里为评分卡手写的。
本模块把它抽象成通用能力——任何交互元素在 DSL 里声明 bind-record + bind-field,
点击后由这里统一把「选中值」写进「该记录的该字段」,不必每个卡型各写一份落账代码。

选中值的来源(实卡实测的回调契约,见 _card_dsl 各 helper 注释):
  - score:   value["score"]        (整数分)
  - date:    action["option"]      ("2026-09-02 +0800" → 取日期部分)
  - select:  action["option"]      (被选 option 的 value)
  - 其它 button 不回写(除非显式带 bind_field + 某个值)

分两层(可测/需运行时):
  - extract_writeback(payload) → (record_id, field, value) | None   纯函数,离线可测
  - write_back_from_callback(...)                                   调 update_bitable_record_impl,需完整运行时
"""

from __future__ import annotations

from typing import Any


def _picked_value(payload: dict[str, Any], value: dict[str, Any]) -> Any:
    """按回调契约取选中值:score 读 value.score,date/select 读顶层 action.option。"""
    action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
    tag = action.get("tag") or ""
    option = action.get("option")
    if tag == "date_picker" and isinstance(option, str) and option:
        # 飞书回传形如 "2026-09-02 +0800",取日期部分。
        return option.split()[0]
    if tag == "select_static" and option is not None:
        return option
    # score 或其它:value 里的 score
    score = value.get("score")
    if isinstance(score, int):
        return score
    # 兜底:若 action.option 存在也用它(未知交互类型)。
    return option


def extract_writeback(payload: dict[str, Any]) -> tuple[str, str, Any] | None:
    """从回调 payload 抽出 (record_id, field, picked_value);不该回写时返回 None。

    回写的前提:value 里同时有 bind_field(DSL 声明)+ record_id(bind-record)+ 拿得到选中值。
    缺任一 → None(该交互只回调、不落库,保持既有行为)。
    """
    if not isinstance(payload, dict):
        return None
    action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
    value = action.get("value") if isinstance(action.get("value"), dict) else {}
    field = str(value.get("bind_field") or "").strip()
    record_id = str(value.get("record_id") or "").strip()
    if not field or not record_id:
        return None
    picked = _picked_value(payload, value)
    if picked is None or (isinstance(picked, str) and not picked):
        return None
    return record_id, field, picked


async def write_back_from_callback(
    payload: dict[str, Any],
    app_token: str,
    table_id: str,
    user_key: str = "",
    identity: str = "",
) -> dict[str, Any]:
    """通用回写:把回调的选中值写进 bind-record 记录的 bind-field 字段。

    可靠性加固(#19,呼应 91403 教训):写之前先核实字段存在、记录存在,把
    「记录缺失」「字段缺失」「真写失败」分开明确上报,不静默产错。
    需完整运行时(调 _feishu_impl → lark_channel);抽取逻辑见 extract_writeback。
    """
    import _feishu_impl as _f  # noqa: PLC0415 (延迟导入:仅完整运行时需要)

    parsed = extract_writeback(payload)
    if parsed is None:
        return {"ok": True, "skipped": "no bind-field / record_id / value in callback"}
    record_id, field, picked = parsed

    # ① 字段存在性预检:字段名拼错会静默写失败,先查字段表。
    fields_res = await _f.list_bitable_fields_impl(app_token, table_id)
    if not fields_res.get("ok"):
        return {"ok": False, "stage": "list_fields", "error": fields_res.get("error") or fields_res}
    names = {f.get("field_name") for f in (fields_res.get("data") or {}).get("items", []) if isinstance(f, dict)}
    if names and field not in names:
        return {"ok": False, "stage": "field_check", "error": f"字段 {field!r} 不在表里 —— 现有字段: {sorted(names)}"}

    # ② 写回(记录存在性由 update 的返回码判定:RecordIdNotFound/91403 明确区分,
    #    不预先 search——search 条件受限时可能空、反而误判;直接写更准)。
    res = await _f.update_bitable_record_impl(
        app_token, table_id, record_id, f'{{"{field}": {_json_scalar(picked)}}}', user_key, identity
    )
    if not res.get("ok"):
        err = str(res.get("error") or res.get("message") or res)
        # 记录不存在 → 明确区分(91403 / RecordIdNotFound),不与其它写失败混为一谈。
        if "RecordIdNotFound" in err or "91403" in err:
            return {"ok": False, "stage": "record_missing", "record_id": record_id, "error": err}
        return {"ok": False, "stage": "update", "error": err}
    return {"ok": True, "record_id": record_id, "field": field, "value": picked}


def _json_scalar(val: Any) -> str:
    import json  # noqa: PLC0415

    return json.dumps(val, ensure_ascii=False)
