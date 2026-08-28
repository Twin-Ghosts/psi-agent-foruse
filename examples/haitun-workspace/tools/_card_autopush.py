"""数据驱动推卡:多维表格记录满足条件 → 自动渲染对应卡片、推给负责人。

闭合 todo 派发那条链的另一半:不只是「人点卡→写台账」(见 _card_writeback),
还要「台账变→推卡给人」。例:记录「状态」变成「待审批」→ 自动发审批卡给审批人。

分两层:
  - decide_push(fields, rules) → PushPlan | None   纯函数,判定+取模板参数,离线可测
  - (轮询/webhook 侦测记录变化 → 调 decide_push → render_template → 发卡)
    侦测与发送需完整运行时,不在本模块;本模块只做「判定+渲染参数」的可测核心。

规则形状(业务声明,一条 SOP 一组规则):
  {
    "when": {"field": "状态", "equals": "待审批"},   # 触发条件
    "template": "review-card",                        # 命中后渲染的模板
    "to_field": "审批人open_id",                      # 推给谁(取记录里这个字段)
    "values": {"owner_name": "负责人", ...}           # 模板参数 ← 记录字段映射
  }
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PushPlan:
    template: str
    to: str
    values: dict[str, Any]


def _cell_text(val: Any) -> str:
    """记录字段值 → 文本(与 _card_dsl._cell_text 同规则:数组取 text/name,对象取 text)。"""
    if val is None:
        return ""
    if isinstance(val, list):
        parts = [str(x.get("text") or x.get("name") or x) if isinstance(x, dict) else str(x) for x in val]
        return "、".join(p for p in parts if p)
    if isinstance(val, dict):
        return str(val.get("text") or val.get("name") or val)
    return str(val)


def _matches(fields: dict[str, Any], when: dict[str, Any]) -> bool:
    """判定记录是否满足触发条件。支持 equals / not_equals / in / present。"""
    field = when.get("field")
    if not field:
        return False
    actual = _cell_text(fields.get(field))
    if "equals" in when:
        return actual == str(when["equals"])
    if "not_equals" in when:
        return actual != str(when["not_equals"])
    if "in" in when and isinstance(when["in"], list):
        return actual in [str(x) for x in when["in"]]
    if when.get("present"):
        return bool(actual)
    return False


def decide_push(fields: dict[str, Any], rules: list[dict[str, Any]]) -> PushPlan | None:
    """给定一条记录的 fields 和规则集,返回第一条命中的推卡计划;都不命中返回 None。

    纯函数:不做 I/O,只判定「要不要推、推什么模板、推给谁、带什么参数」。
    """
    if not isinstance(fields, dict):
        return None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        when = rule.get("when") if isinstance(rule.get("when"), dict) else {}
        if not _matches(fields, when):
            continue
        template = str(rule.get("template") or "").strip()
        to = _cell_text(fields.get(rule.get("to_field") or "")).strip()
        if not template or not to:
            # 命中条件但缺模板/收件人 → 该规则不可执行,跳过(不静默乱推)。
            continue
        mapping = rule.get("values") if isinstance(rule.get("values"), dict) else {}
        values = {param: _cell_text(fields.get(src)) for param, src in mapping.items()}
        return PushPlan(template=template, to=to, values=values)
    return None
