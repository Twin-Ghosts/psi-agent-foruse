# ruff: noqa: RUF002, RUF003  # 中文全角标点是刻意排版,非歧义字符
"""boss 整体统计卡发送工具（T5）—— 遍历 mentor 台账 → 全量合并 → 统计 → 渲染 → 私聊发卡。

对接 ``company-todo-audit`` 的 boss 段（14:30 闭环判定后给 boss 推全公司
汇总）：把本周期出现过的每个 mentor 的台账 base 解析出来（ensure 幂等拿
app_token → cycle_table 拿本周期表），逐个读回本周期表全部行，**全量合并**
后统一走 ``_boss_overview_impl.build_boss_stats``（T5 统计口径纯函数，唯一
权威口径）算出全局指标/团队维度/逾期 TOP，经 ``card-dsl`` 的
``boss-overview-card`` 模板渲染成纯只读统计卡，私聊发给 boss 本人。

五条铁律（与 SKILL 一致，写代码时固化）：
- **统计必须现场读**：每次调用都重新拉所有 mentor 本周期表全行再统计，
  禁止复用记忆里的数字或接受调用方传入的统计结果。
- **统一口径，禁止逐表统计再相加**：全量行合并后一次遍历分组计数（见
  ``_boss_overview_impl`` 模块 docstring），团队行数字之和 == 全局数字。
- **不静默缺队**：任一 mentor 的台账解析或读表失败 → 整卡失败并列出全部
  mentor_errors（boss 卡标题是「全公司」，缺一个团队就是误导，宁缺勿发）。
  读回 0 行是合法空团队（零值团队行占位），不是错误。
- **只读卡**：渲染出的卡片不含任何按钮/回调（``handlers`` 恒为空），
  boss 卡是「看」的，不是「点」的。
- **测试模式**：设置了 ``PSI_BOSS_CARD_TEST_RECEIVE_ID``（或 T4 的
  ``PSI_REPORT_CARD_TEST_RECEIVE_ID``）或传了 ``test_receive_id`` 时，
  卡片发给测试者本人代替 boss，严禁发到真实 boss 手上。

读台账身份：``user_key``/``identity`` 传入时 tenant 优先、被拒自动回退该
用户身份（``_core._invoke`` 默认行为）；ensure 的建库/授权写操作沿用
sync 同一套身份约定。
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any

import _boss_overview_impl as _boss
import _feishu_impl as _core
from _card_dsl import render_template
from _feishu.mentor_ledger import mentor_ledger_ensure_impl
from feishu_ledger_cycle_table import feishu_mentor_ledger_cycle_table
from feishu_mentor_report_send import _fetch_cycle_rows, _stat_fields

# 测试模式：设了该环境变量时,统计卡发给该 open_id(测试者本人)代替真实 boss,
# 严禁把统计卡发到真实 boss 手上。正式运行不设此变量 → 发真 boss。
# 每次调用实时读取（不缓存模块级常量），部署时可热切换测试/正式。
_TEST_ENV_VAR = "PSI_BOSS_CARD_TEST_RECEIVE_ID"
# T4 mentor 卡的测试环境变量作为兜底：同一测试环境一条变量可同时改投两张卡。
_FALLBACK_TEST_ENV_VAR = "PSI_REPORT_CARD_TEST_RECEIVE_ID"


def _mentor_error(msg: str, mentor_name: str = "") -> dict[str, str]:
    return {"mentor": mentor_name, "error": msg}


def _read_error(res: dict[str, Any]) -> str:
    return str(res.get("message") or res.get("error") or res)


async def feishu_boss_overview_send(
    boss_open_id: str = "",
    cycle_date: str = "",
    mentors_json: str = "",
    folder_token: str = "",
    template_app_token: str = "",
    tree_url: str = "",
    trend: str = "",
    top_n: int = 5,
    test_receive_id: str = "",
    user_key: str = "",
    identity: str = "",
) -> str:
    """给 boss 私聊发送本周期全公司 TODO 总览卡（纯只读）。

    Args:
        boss_open_id: 收卡 boss 的 open_id（必填）。
        cycle_date: 周期日期 YYYY-MM-DD（必填，定位各台账本周期表）。
        mentors_json: 必填 JSON 数组——本周期出现过的 mentor 名单，
            每项 ``{"open_id": "...", "name": "..."}``；name 必填（卡面
            团队名），open_id 建议传（ensure 需要，缺失时报该团队错误）。
        folder_token: mentor 台账所在共享云盘文件夹 token（ensure 用，必填）。
        template_app_token: 台账模板 base 的 app_token（ensure 用；空 =
            按内置 schema 直接新建——报表场景通常已建好，传空也能跑）。
        tree_url: 公司工作树 wiki 根页链接（必填，卡面「打开公司工作树」）。
        trend: 可选——全公司完成率趋势文案（如 "65%→70%→74%（近6周期）"），
            数据不足时留空，模板渲染为 "—"。
        top_n: 逾期 TOP 条数（默认 5，必须 ≥1）。
        test_receive_id: 测试模式收卡人 open_id，优先于环境变量
            ``PSI_BOSS_CARD_TEST_RECEIVE_ID``；二者皆空时发真实 boss。
        user_key: 调用者 open_id（读台账身份回退用）。
        identity: "user"/"bot"——ensure 建库/授权用（沿用 sync 约定）。

    Returns:
        JSON 字符串：ok / boss_open_id / receive_id / test_override /
        team_count / row_count / mentor_errors / counts（统计结构化结果）/
        message_id（发送成功时）。
    """
    boss_open_id = boss_open_id.strip()
    cycle_date = cycle_date.strip()
    folder_token = folder_token.strip()
    template_app_token = template_app_token.strip()
    tree_url = tree_url.strip()
    if not boss_open_id:
        return json.dumps({"ok": False, "error": "boss_open_id is required"}, ensure_ascii=False)
    if not cycle_date:
        return json.dumps({"ok": False, "error": "cycle_date is required (YYYY-MM-DD)"}, ensure_ascii=False)
    if not folder_token:
        return json.dumps(
            {"ok": False, "error": "folder_token is required (the shared drive folder for mentor ledgers)"},
            ensure_ascii=False,
        )
    if not tree_url:
        return json.dumps(
            {"ok": False, "error": "tree_url is required (the company work-tree wiki link)"},
            ensure_ascii=False,
        )

    try:
        top_n = int(top_n)
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "error": "top_n must be an integer"}, ensure_ascii=False)
    if top_n < 1:
        return json.dumps({"ok": False, "error": "top_n must be >= 1"}, ensure_ascii=False)

    mentors: list[dict[str, str]] = []
    if mentors_json.strip():
        try:
            parsed = json.loads(mentors_json)
        except ValueError as exc:
            err = f"mentors_json is not valid JSON: {exc}"
            return json.dumps({"ok": False, "error": err}, ensure_ascii=False)
        if not isinstance(parsed, list):
            return json.dumps({"ok": False, "error": "mentors_json must be a JSON array"}, ensure_ascii=False)
        for item in parsed:
            if not isinstance(item, dict):
                return json.dumps(
                    {"ok": False, "error": "each mentors_json item must be an object {open_id, name}"},
                    ensure_ascii=False,
                )
            mentors.append(
                {
                    "open_id": str(item.get("open_id") or "").strip(),
                    "name": str(item.get("name") or "").strip(),
                }
            )
    if not mentors:
        return json.dumps(
            {"ok": False, "error": "mentors_json must not be empty (boss card needs the mentor roster)"},
            ensure_ascii=False,
        )

    # ── 1. 逐个 mentor：ensure → 本周期表 → 读全行（全量合并）────────────────
    merged_rows: list[dict[str, Any]] = []
    mentor_errors: list[dict[str, str]] = []
    for mentor in mentors:
        name, oid = mentor["name"], mentor["open_id"]
        if not name:
            mentor_errors.append(_mentor_error("mentor name is required", name))
            continue
        ensured = await mentor_ledger_ensure_impl(
            mentor_open_id=oid,
            mentor_name=name,
            folder_token=folder_token,
            template_app_token=template_app_token,
            boss_open_id=boss_open_id,
            user_key=user_key,
            identity=identity,
        )
        if not ensured.get("ok"):
            mentor_errors.append(_mentor_error(_read_error(ensured), name))
            continue
        app_token = str(ensured.get("app_token") or "").strip()
        if not app_token:
            mentor_errors.append(_mentor_error("ensure returned no app_token", name))
            continue

        cycle_res = json.loads(
            await feishu_mentor_ledger_cycle_table(app_token, cycle_date, user_key, identity)
        )
        if not cycle_res.get("ok"):
            mentor_errors.append(_mentor_error(_read_error(cycle_res), name))
            continue
        table_id = str(cycle_res.get("table_id") or "").strip()
        if not table_id:
            mentor_errors.append(_mentor_error("cycle table returned no table_id", name))
            continue

        records, err = await _fetch_cycle_rows(app_token, table_id, user_key)
        if err is not None:
            mentor_errors.append(_mentor_error(_read_error(err), name))
            continue
        for rec in records:
            fields = rec.get("fields", {})
            if not isinstance(fields, dict):
                continue
            merged_rows.append({**_stat_fields(fields), "_team": name})

    if mentor_errors:
        return json.dumps(
            {"ok": False, "error": "mentor ledger read failed", "mentor_errors": mentor_errors},
            ensure_ascii=False,
            default=str,
        )

    # ── 2. 统计（统一口径：build_boss_stats）────────────────────────────────
    stats = _boss.build_boss_stats(
        merged_rows,
        mentors=mentors,
        today=datetime.date.today(),
        top_n=top_n,
    )

    # ── 3. 渲染模板（boss-overview-card，纯只读）───────────────────────────
    rendered = render_template(
        "boss-overview-card",
        values_json=json.dumps(
            {
                "template": stats["template"],
                "cycle_label": cycle_date[5:] if len(cycle_date) >= 10 else cycle_date,
                "global_summary": stats["global_summary"],
                "overdue_top": stats["overdue_top"],
                "trend": trend.strip() or "—",
                "tree_url": tree_url,
            },
            ensure_ascii=False,
        ),
        context_json=json.dumps({"teams": stats["teams"]}, ensure_ascii=False),
    )
    if not rendered.get("ok"):
        return json.dumps(
            {"ok": False, "error": rendered.get("error") or "dsl render failed"},
            ensure_ascii=False,
        )
    card, handlers = rendered["card"], rendered["handlers"]
    if handlers:
        # boss 卡是纯只读的,模板编译不该产出任何回调;若未来模板加了按钮,
        # 这里要同步补 business_context 与映射,而不是静默发一张会出事的卡。
        return json.dumps({"ok": False, "error": "boss overview card must be read-only"}, ensure_ascii=False)

    # ── 4. 私聊发卡（测试模式覆盖收卡人）───────────────────────────────────
    env_test = os.environ.get(_TEST_ENV_VAR, "").strip() or os.environ.get(_FALLBACK_TEST_ENV_VAR, "").strip()
    receive_id = (test_receive_id.strip() or env_test or boss_open_id).strip()
    test_override = receive_id != boss_open_id
    business_context = {
        "kind": "company_todo_boss_overview",
        "boss_open_id": boss_open_id,
        "cycle_date": cycle_date,
        "mentor_count": len(mentors),
        "tree_url": tree_url,
    }
    try:
        res = await _core.send_card_impl(
            receive_id=receive_id,
            card_json=json.dumps(card, ensure_ascii=False),
            receive_id_type="open_id",
            user_key=user_key,
            business_context_json=json.dumps(business_context, ensure_ascii=False),
            action_handlers_json="{}",
        )
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{e!r}"}, ensure_ascii=False)
    if not res.get("ok"):
        return json.dumps(
            {"ok": False, "error": res.get("message") or res.get("error") or res},
            ensure_ascii=False,
            default=str,
        )

    result: dict[str, Any] = {
        "ok": True,
        "boss_open_id": boss_open_id,
        "receive_id": receive_id,
        "test_override": test_override,
        "team_count": len(stats["teams"]),
        "row_count": len(merged_rows),
        "mentor_errors": [],
        "counts": stats["counts"],
    }
    message_id = res.get("message_id")
    if isinstance(message_id, str) and message_id:
        result["message_id"] = message_id
    return json.dumps(result, ensure_ascii=False, default=str)
