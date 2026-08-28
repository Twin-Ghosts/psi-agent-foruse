"""psi-card 测试题库 + 引擎自动验收（基于 638 行修正版 _card_dsl）。

用途三合一：
  1. 每题的「业务需求」= 可直接喂给海豚模型的自然语言（测 AI 生成 DSL）；
  2. 每题的 XML = 期望海豚产出的声明（这里代打，测引擎编译）；
  3. 每题的 check = 引擎编译后的验收断言（对/错怎么判）。

跑法：PYTHONPATH=. python _psicard_testbank.py
输出：逐题 PASS/FAIL + 汇总。既是引擎回归，也是实卡验收清单。
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any, Callable

# ── stub 运行时依赖（与单测同款）────────────────────────────────────────────
_imp = types.ModuleType("_todo_card_impl")
_imp._UNDO_ROUNDS = 20
_imp._build_card_from_state = lambda s: {"schema": "2.0", "_legacy_state": s}
_imp._tick_action_id = lambda i, r: f"todo_tick_{i}_r{r}"
_imp._untick_action_id = lambda i, r: f"todo_untick_{i}_r{r}"
sys.modules.setdefault("_todo_card_impl", _imp)
_paths = types.ModuleType("_runtime_paths")
_paths.agent_dir = lambda: os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.modules.setdefault("_runtime_paths", _paths)

import _card_dsl as d  # noqa: E402


def _walk_values(card: dict[str, Any]):
    """遍历卡片里所有回调 value dict。"""
    stack = [card]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            if n.get("type") == "callback" and isinstance(n.get("value"), dict):
                yield n["value"]
            stack.extend(n.values())
        elif isinstance(n, list):
            stack.extend(n)


class Q:
    """一道测试题。"""

    def __init__(
        self,
        qid: str,
        level: str,
        need: str,
        xml: str,
        check: Callable[[dict[str, Any]], bool],
        note: str = "",
        kind: str = "card",
        values: str = "{}",
        context: str = "{}",
        overrides: str = "{}",
    ) -> None:
        self.qid = qid
        self.level = level
        self.need = need  # 喂海豚的自然语言需求
        self.xml = xml  # 期望的 XML（代打）
        self.check = check  # 引擎产物验收
        self.note = note
        self.kind = kind  # card | template | expect_fail
        self.values = values
        self.context = context
        self.overrides = overrides  # 自定义 action → handler 映射

    def run(self) -> tuple[bool, str]:
        try:
            if self.kind == "template":
                out = d.render_template(
                    self.xml, values_json=self.values, context_json=self.context,
                    handler_overrides_json=self.overrides,
                )
            else:
                out = d.render_card(
                    self.xml, context_json=self.context, handler_overrides_json=self.overrides,
                )
        except Exception as e:  # noqa: BLE001
            return False, f"引擎抛异常 {e!r}"
        try:
            ok = self.check(out)
        except Exception as e:  # noqa: BLE001
            return False, f"验收断言抛异常 {e!r} | out={str(out)[:120]}"
        return ok, "" if ok else f"验收未过 | out={str(out)[:160]}"


BANK: list[Q] = []

# ══ L1 单元素基础（喂海豚：能不能把最简单的需求转对）══════════════════════
BANK += [
    Q(
        "L1-01", "L1",
        "做一张标题叫「周报」的卡片，上面写一行信息：负责人是张三。",
        '<card title="周报"><info label="负责人" value="张三"/></card>',
        lambda o: o["ok"] and o["card"]["header"]["title"]["content"] == "周报"
        and o["card"]["body"]["elements"][0]["content"].endswith("张三"),
        "最小卡：标题 + 一条 info",
    ),
    Q(
        "L1-02", "L1",
        "一张告警卡，标题「服务器告警」，卡头用红色（逾期/告警）。",
        '<card title="服务器告警" template="red"><info label="状态" value="CPU 95%"/></card>',
        lambda o: o["ok"] and o["card"]["header"]["template"] == "red",
        "template 语义色 red",
    ),
    Q(
        "L1-03", "L1",
        "一张评分卡，1到5分，绑定记录 rec001，默认选中 4 分。",
        '<card title="打分"><score min="1" max="5" bind-record="rec001" selected="4"/></card>',
        lambda o: o["ok"]
        and any(v.get("score") == 4 for v in _walk_values(o["card"]))
        and any("✓" in (c["elements"][0]["text"]["content"])
                for c in o["card"]["body"]["elements"][0]["columns"]),
        "score 高亮选中项",
    ),
    Q(
        "L1-04", "L1",
        "一张带评语输入框的卡，提示语「写点评语」，绑定 rec001。",
        '<card title="评语"><comment placeholder="写点评语" bind-record="rec001"/></card>',
        lambda o: o["ok"]
        and o["card"]["body"]["elements"][0]["tag"] == "input"
        and "confirm" in o["card"]["body"]["elements"][0],
        "comment 自动配 confirm 按钮",
    ),
    Q(
        "L1-05", "L1",
        "一张卡带一个「打回重做」按钮，语义是驳回。",
        '<card title="审批"><action-row><button text="打回重做" type="reject" action="review_reject"/></action-row></card>',
        lambda o: o["ok"]
        and o["card"]["body"]["elements"][0]["columns"][0]["elements"][0]["type"] == "danger",
        "reject→danger 语义映射",
    ),
]

# ══ L2 组合场景（真实 SOP 卡）════════════════════════════════════════════
BANK += [
    Q(
        "L2-01", "L2",
        "完整评价卡：标题「TODO 评价」蓝头，执行人黄子建、任务优化方案两行信息，"
        "1-5 分打分绑 recX 默认 4 分，评语框，一个打回重做按钮。",
        '<card title="TODO 评价" template="blue">'
        '<info label="执行人" value="黄子建"/>'
        '<info label="任务" value="优化方案"/>'
        '<score min="1" max="5" bind-record="recX" selected="4"/>'
        '<comment placeholder="写点评语" bind-record="recX"/>'
        '<action-row><button text="打回重做" type="reject" action="review_reject"/></action-row>'
        "</card>",
        lambda o: o["ok"] and len(o["card"]["body"]["elements"]) == 5
        and o["handlers"].get("review_score_r0") == "feishu_review_card_select"
        and o["handlers"].get("review_reject_r0") == "feishu_review_reject",
        "评价卡全要素 + handler 预注册",
    ),
    Q(
        "L2-02", "L2",
        "待办卡：标题「今日待办」，三行任务——写方案(未完成)、开会(已完成)、发周报(未完成)。",
        '<card title="今日待办"><list>'
        '<row title="写方案"/><row title="开会" done="true"/><row title="发周报"/>'
        "</list></card>",
        lambda o: o["ok"] and len(o["card"]["_legacy_state"]["rows"]) == 3
        and o["card"]["_legacy_state"]["rows"][1]["locked"] is True,
        "list 卡 + done 行只读",
        context='{"ledger_app_token":"tok","ledger_table_id":"tbl"}',
    ),
    Q(
        "L2-03", "L2",
        "多按钮行：一张卡上并排三个按钮——通过(accept)、驳回(reject)、忽略(default)，动作各不同。",
        '<card title="审批"><action-row>'
        '<button text="通过" type="accept" action="approve"/>'
        '<button text="驳回" type="reject" action="reject_it"/>'
        '<button text="忽略" type="default" action="ignore"/>'
        "</action-row></card>",
        lambda o: o["ok"]
        and [c["elements"][0]["type"] for c in o["card"]["body"]["elements"][0]["columns"]]
        == ["primary", "danger", "default"],
        "三按钮语义色 accept→primary/reject→danger/default→default（自定义 action 需 overrides）",
        context='{}',
        overrides='{"approve":"my_approve","reject_it":"my_reject","ignore":"my_ignore"}',
    ),
    Q(
        "L2-04", "L2",
        "自定义动作：一个「归档」按钮走业务自定义 handler（archive_it→归档工具），"
        "验证 overrides 能把自定义 action 接上。",
        '<card title="报表"><action-row><button text="归档" action="archive_it"/></action-row></card>',
        lambda o: o["ok"] and o["handlers"].get("archive_it_r0") == "archive_tool",
        "handler_overrides 接自定义 action",
        overrides='{"archive_it":"archive_tool"}',
    ),
]

# ══ L3 四项修复 + 对抗边界（应当 fail-closed 报错）═════════════════════════
BANK += [
    Q(
        "L3-A", "L3",
        "【A 多 list】一张卡放两个待办列表（误用），引擎应报错而非静默丢第二个。",
        '<card title="t"><list><row title="a"/></list><list><row title="b"/></list></card>',
        lambda o: o["ok"] is False and "list" in o["error"],
        "修复A：多 list 报错不吞行",
    ),
    Q(
        "L3-D", "L3",
        "【D 残留占位符】评价卡模板漏传 record_id，引擎应拒绝，不能把 {record_id} 当真 id 发出。",
        "review-card",
        lambda o: o["ok"] is False and "占位符" in o["error"],
        "修复D：残留占位符拦截",
        kind="template",
        values='{"owner_name":"甲","title":"活","delivered_at":"今","selected_score":0}',
    ),
    Q(
        "L3-E", "L3",
        "【E rounds 生效】评分卡声明 rounds=5，引擎应只预注册 5 轮 handler（而非硬编码 20）。",
        '<card title="t"><score min="1" max="3" rounds="5" bind-record="r"/></card>',
        lambda o: o["ok"] and len(o["handlers"]) == 5,
        "修复E：rounds 属性真生效",
    ),
    Q(
        "L3-B", "L3",
        "【B action_id 防撞】同一 action 两个按钮，两者 action_id 应不同（加序号区分）。",
        '<card title="t"><action-row>'
        '<button text="A" action="review_reject"/><button text="B" action="review_reject"/>'
        "</action-row></card>",
        lambda o: o["ok"]
        and len({v["action_id"] for v in _walk_values(o["card"])}) == 2,
        "修复B：同 action 双按钮 action_id 唯一",
    ),
    Q(
        "L3-01", "L3",
        "【坏输入】按钮不写 action（必填项漏了），引擎应明确报「缺 action」而非误报 handler。",
        '<card title="t"><action-row><button text="x" type="accept"/></action-row></card>',
        lambda o: o["ok"] is False and "action" in o["error"],
        "报错指向属性本身",
    ),
    Q(
        "L3-02", "L3",
        "【坏输入】未知元素 <widget>，引擎应报错并列出合法词汇表。",
        '<card title="t"><widget/></card>',
        lambda o: o["ok"] is False and "unknown element" in o["error"],
        "未知元素 fail-closed",
    ),
    Q(
        "L3-03", "L3",
        "【坏输入】空 action-row（无按钮），引擎应报错对齐 XSD。",
        '<card title="t"><action-row/></card>',
        lambda o: o["ok"] is False and "button" in o["error"],
        "空 action-row 报错",
    ),
    Q(
        "L3-04", "L3",
        "【注入】标题含 XML 特殊字符与引号，编译后不应破坏结构。",
        '<card title="t"><info label="人名" value="张&quot;三&quot;&amp;李&lt;四&gt;"/></card>',
        lambda o: o["ok"] and '张"三"&李<四>' in o["card"]["body"]["elements"][0]["content"],
        "转义酷刑：引号/&/<> 安全还原",
    ),
    Q(
        "L3-05", "L3",
        "【混用报错】list 卡里混入 score（第一版不支持），应报错。",
        '<card title="t"><list><row title="a"/></list><score bind-record="r"/></card>',
        lambda o: o["ok"] is False and "list" in o["error"],
        "list 不与 2.0 元素混用",
        context='{"ledger_app_token":"tok","ledger_table_id":"tbl"}',
    ),
]


def main() -> int:
    by_level: dict[str, list[tuple[str, bool, str]]] = {}
    npass = nfail = 0
    for q in BANK:
        ok, msg = q.run()
        by_level.setdefault(q.level, []).append((q.qid, ok, msg))
        if ok:
            npass += 1
        else:
            nfail += 1
    for level in sorted(by_level):
        print(f"\n══ {level} ══")
        for qid, ok, msg in by_level[level]:
            tag = "PASS" if ok else "FAIL"
            print(f"  [{tag}] {qid}" + (f"  →  {msg}" if not ok else ""))
    print(f"\n总计：{npass} PASS / {nfail} FAIL / 共 {len(BANK)} 题")
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())


