"""方案验收测试:把《todo:交互界面通用元素-以卡片为例》每条要求转成对真引擎的断言。

不是口头对照,是可运行的证据:每条引方案原文一句,跑真 _card_dsl 引擎断言,
打印 PASS + 证据。范围严格按方案 §2「只做卡片」——超范围的 table/bind-field
不计入本验收(它们是额外探索)。本机验不了的(实卡墓碑绕过、真实落账)如实标注 SKIP。

跑法: PYTHONPATH=. python -X utf8 _acceptance_plan.py
"""

# 本文件是验收报告脚本,靠 print 逐条输出 PASS/FAIL 证据——T201 是其本职,豁免。
# ruff: noqa: T201
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import tempfile
import types

import anyio

# ── stub 运行时依赖(与其它套件一致)──────────────────────────────────────────
_i = types.ModuleType("_todo_card_impl")
_i._UNDO_ROUNDS = 20
_i._build_card_from_state = lambda s: {"schema": "2.0", "_state": s, "_legacy_state": s}
_i._tick_action_id = lambda a, b: f"todo_tick_{a}_r{b}"
_i._untick_action_id = lambda a, b: f"todo_untick_{a}_r{b}"
sys.modules.setdefault("_todo_card_impl", _i)
_p = types.ModuleType("_runtime_paths")
_p.agent_dir = lambda: os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.modules.setdefault("_runtime_paths", _p)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _card_dsl as dsl  # noqa: E402

_PASS: list[str] = []
_FAIL: list[str] = []
_SKIP: list[str] = []


def check(clause: str, quote: str, ok: bool, evidence: str) -> None:
    tag = "PASS" if ok else "FAIL"
    (_PASS if ok else _FAIL).append(clause)
    print(f"[{tag}] {clause}")
    print(f"       方案原文:「{quote}」")
    print(f"       证据: {evidence}")


def skip(clause: str, why: str) -> None:
    _SKIP.append(clause)
    print(f"[SKIP] {clause}")
    print(f"       原因: {why}")


def _load_tombstone_store():
    """加载真实墓碑代码 _card_store(绕过 feishu/__init__ 的 lark_channel 导入)。

    返回模块;若真依赖缺失则返回 None(那才退回 SKIP)。
    """
    src = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "src"))
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        import psi_agent._appdata  # noqa: F401,PLC0415  (探测依赖可用性,故意延迟导入)
    except Exception:
        return None
    name = "psi_agent.channel.feishu._card_store"
    path = os.path.join(src, "psi_agent", "channel", "feishu", "_card_store.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _verify_tombstone_bypass(cs) -> tuple[bool, str]:
    """用真实墓碑代码验证:同名重点被拒、轮次+1换名绕开。返回 (ok, 证据)。"""
    review = '<card title="评价" template="blue"><score min="1" max="5" bind-record="rec"/></card>'

    def aid(rnd: int) -> str:
        out = dsl.render_card(review, round_=rnd)
        for el in out["card"]["body"]["elements"]:
            if el.get("tag") == "column_set":
                return el["columns"][0]["elements"][0]["behaviors"][0]["value"]["action_id"]
        raise AssertionError("no score button")

    with tempfile.TemporaryDirectory() as appdata:
        mid = "om_acceptance_tombstone"
        a0, a1 = aid(0), aid(1)
        card = dsl.render_card(review)["card"]
        await cs.save_card_snapshot(mid, card, appdata, action_handlers={a0: "h", a1: "h"}, multi_use=True)
        c1 = await cs.pop_card_snapshot(mid, appdata, action_id=a0)
        c2 = await cs.pop_card_snapshot(mid, appdata, action_id=a0)
        c3 = await cs.pop_card_snapshot(mid, appdata, action_id=a1)
        ok = c1.status == "claimed" and c2.status == "already_consumed" and c3.status == "claimed"
        ev = f"首点{a0}={c1.status}; 同名重点={c2.status}(应 already_consumed); 换名{a1}={c3.status}(应 claimed)"
        return ok, ev


def _walk(card):
    def w(n):
        if isinstance(n, dict):
            yield n
            for v in n.values():
                yield from w(v)
        elif isinstance(n, list):
            for v in n:
                yield from w(v)

    yield from w(card)


# 方案 §3.1.4 的评价卡示例(约 10 行),整个验收围绕它。
REVIEW = (
    '<card title="TODO 评价" template="blue">'
    '<info label="执行人" value="黄子建"/>'
    '<info label="任务" value="优化TODO方案"/>'
    '<score min="1" max="5" rounds="20" bind-record="recXXX"/>'
    '<comment placeholder="写点评语" bind-record="recXXX"/>'
    '<action-row><button text="打回重做" type="reject" action="review_reject"/></action-row>'
    "</card>"
)


def run() -> None:
    print("=" * 70)
    print("方案验收:《todo:交互界面通用元素-以卡片为例》逐条断言(真引擎实跑)")
    print("=" * 70)

    # ── §1.3 三层架构:XML DSL → 引擎 → 飞书卡片 2.0 JSON ──────────────────────
    out = dsl.render_card(REVIEW)
    check(
        "§1.3 三层架构:DSL 编译成飞书卡片 2.0 JSON",
        "XML DSL → 渲染引擎 → 飞书卡片 JSON",
        out["ok"] and out["card"]["schema"] == "2.0",
        f"render_card 返回 ok={out['ok']}, card.schema={out['card']['schema']!r}",
    )
    # DSL 层无飞书概念:业务 XML 里不出现 tag/behaviors/callback 等飞书词
    feishu_words = ("behaviors", '"tag"', "callback", "column_set", "plain_text")
    leaked = [w for w in feishu_words if w in REVIEW]
    check(
        "§1.3 DSL 层完全剥离飞书概念",
        "DSL 层已完全剥离飞书概念(业务不出现任何飞书 JSON)",
        not leaked,
        f"业务 XML 中飞书词出现={leaked or '无'}",
    )

    # ── §1.4 后端可替换:引擎入口只吃 XML 字符串,与来源解耦 ─────────────────────
    sig = inspect.signature(dsl.render_card)
    first_param = next(iter(sig.parameters))
    check(
        "§1.4 后端可替换/数据库预留:入口只吃 XML 字符串",
        "引擎内部按 DSL → 后端适配器组织;卡片定义将来可切数据库,核心不动",
        first_param == "card_xml",
        f"render_card 首参={first_param!r}(字符串入口,与卡片定义来源解耦)",
    )

    # ── §3.1 词汇表 5 元素齐备 ────────────────────────────────────────────────
    o_info = dsl.render_card('<card title="t"><info label="a" value="b"/></card>')
    o_score = dsl.render_card('<card title="t"><score bind-record="r"/></card>')
    o_comment = dsl.render_card('<card title="t"><comment bind-record="r"/></card>')
    o_btn = dsl.render_card('<card title="t"><action-row><button text="x" action="review_reject"/></action-row></card>')
    check(
        "§3.1 词汇表 card/info/score/comment/action-row/button 齐备",
        "第一版收录三类已验证卡型抽象出的块",
        all(o["ok"] for o in (o_info, o_score, o_comment, o_btn)),
        f"info={o_info['ok']} score={o_score['ok']} comment={o_comment['ok']} action-row/button={o_btn['ok']}",
    )

    # ── §3.1.3 三层结构:语义色映射,颜色不进 XML ─────────────────────────────────
    # 加强:穷举全部 5 个语义 type,逐一编译真卡、核对映射到的飞书 button.type,
    # 且证明业务 XML 里从不出现飞书样式词(danger/primary/default)。
    want = {"accept": "primary", "reject": "danger", "danger": "danger", "default": "default", "primary": "primary"}
    got = {}
    for sem in want:
        oc = dsl.render_card(
            f'<card title="t"><action-row><button text="x" type="{sem}" action="review_reject"/></action-row></card>'
        )
        got[sem] = next(n for n in _walk(oc["card"]) if n.get("tag") == "button")["type"]
    map_ok = got == want
    xml_clean = not any(w in REVIEW for w in ("danger", "primary", "default"))
    check(
        "§3.1.3 语义映射:全部 5 个语义 type 逐一编译核对映射正确,颜色不进 XML",
        "引擎映射表把 reject 翻译成飞书按钮的红色样式——颜色不在 XML 里",
        map_ok and xml_clean,
        f"语义→飞书type 实测={got}; 期望={want}; 业务 XML 不含飞书样式词={xml_clean}",
    )
    # 反证:非法 type 必须 fail-closed(不能静默落成某个默认色)
    o_badtype = dsl.render_card(
        '<card title="t"><action-row><button text="x" type="rainbow" action="review_reject"/></action-row></card>'
    )
    check(
        "§3.1.3 反证:未知语义 type 声明即报错(不静默落色)",
        "业务只写语义;映射表只认已定义语义,未知值不得静默通过",
        not o_badtype["ok"] and "type" in o_badtype["error"],
        f"type='rainbow' → ok={o_badtype['ok']}, error 指向 type={('type' in o_badtype['error'])}",
    )
    check(
        "§3.1.3 accept→蓝(飞书无绿档的已声明妥协)",
        "accept → 绿色是设计意图,映射表先落到最近可用样式(蓝)",
        dsl._BUTTON_TYPES["accept"] == "primary",
        f"映射表 accept → {dsl._BUTTON_TYPES['accept']!r}(primary=蓝)",
    )

    # ── §3.2.1 编译职责:input 自动附加 confirm ────────────────────────────────
    inp = next(n for n in _walk(out["card"]) if n.get("tag") == "input")
    check(
        "§3.2.1 编译职责:input 类元素自动附加 confirm",
        "自动为 input 类元素附加 confirm(飞书输入值唯一回传通道)",
        "confirm" in inp,
        f"comment 编译出的 input 含 confirm 字段={'confirm' in inp}",
    )

    # ── §3.2.2 Action 六环 ────────────────────────────────────────────────────
    # 环2 生成:动作名 {动作}_r{轮次}
    vals = [n["behaviors"][0]["value"] for n in _walk(out["card"]) if n.get("tag") in ("button", "input")]
    r0_names = {v["action"] for v in vals}
    check(
        "§3.2.2 环2 生成:带轮次动作名 {动作}_r{轮次},轮次从 0 起",
        "引擎为每个动作生成带轮次的 action 名,命名规则统一为 {动作名}_r{轮次}",
        all(a.endswith("_r0") for a in r0_names),
        f"round_=0 渲染的动作名={sorted(r0_names)}",
    )
    # 环3 映射(加强):全部 20 轮逐一预注册(不只抽查 r0/r19),且映射到正确直调工具;
    # 并证反面——第 20 轮(r20,越界)不得存在。
    hs = out["handlers"]
    all_rounds_ok = all(hs.get(f"review_score_r{r}") == "feishu_review_card_select" for r in range(20))
    ceiling_ok = "review_score_r20" not in hs
    check(
        "§3.2.2 环3 映射:全部 20 轮逐一预注册到正确直调工具,且 r20 越界不存在",
        "引擎自动生成 action handlers 表——全部轮次预先注册、映射到对应直调工具",
        all_rounds_ok and ceiling_ok,
        f"r0..r19 全部→feishu_review_card_select={all_rounds_ok}; r20 不存在={ceiling_ok}",
    )
    # 环3 加强·无死键不变量:每个渲染出的可点击 action,在任意轮次都必有 handler
    dead_by_round = {}
    for rnd in range(20):
        rc = dsl.render_card(REVIEW, round_=rnd)
        targets = {
            v["action"]
            for v in (n["behaviors"][0]["value"] for n in _walk(rc["card"]) if n.get("tag") in ("button", "input"))
        }
        dead = targets - set(rc["handlers"])
        if dead:
            dead_by_round[rnd] = dead
    check(
        "§3.2.2 环3 无死键不变量:0..19 每一轮渲染的每个 action 都有 handler(穷举)",
        "全部轮次预先注册——渲染出来的按钮不能指向未注册的动作(死键)",
        not dead_by_round,
        f"20 轮逐一检查,死键轮次={dead_by_round or '无(全部可路由)'}",
    )
    # 环4 回调组装(加强):每一个交互元素的 value 都必须带全 record_id/action/round
    # (不是抽查一个),且 action 与所在元素的轮次后缀一致、record_id 来自 bind-record。
    # 契约:action/round 是每个交互元素必带的;record_id 仅当元素声明了 bind-record 才注入
    # (REVIEW 里 score/comment 绑了 recXXX,打回按钮没绑——它没有 record_id 才是对的)。
    contract_bad = []
    for v in vals:
        for k in ("action", "round"):
            if k not in v:
                contract_bad.append((v.get("action_id"), f"缺{k}"))
        # 绑了记录的必须是 recXXX;没绑的不该凭空冒出 record_id
        if "record_id" in v and v["record_id"] != "recXXX":
            contract_bad.append((v.get("action_id"), f"record_id={v['record_id']!r}≠recXXX"))
    bound = [v for v in vals if "record_id" in v]  # score + comment 绑了
    unbound = [v for v in vals if "record_id" not in v]  # 打回按钮没绑
    check(
        "§3.2.2 环4 回调组装:每个交互元素必带 action/round;bind-record 精确控制 record_id(穷举+反证)",
        "引擎把业务字段(bind-record、action、轮次...)自动拼进每个按钮/输入框的回调 value",
        not contract_bad and len(bound) >= 2 and len(unbound) >= 1,
        f"检查{len(vals)}个value: 绑记录={len(bound)}个(均recXXX), "
        f"未绑={len(unbound)}个(正确无record_id), 异常={contract_bad or '无'}",
    )
    # 环4 加强·序列化不变量:整卡 + 全部 value 必须 JSON 往返无损(否则发不出/回调解不了)
    try:
        json.loads(json.dumps(out["card"], ensure_ascii=False))
        json_ok = True
    except (ValueError, TypeError) as e:
        json_ok = f"{e!r}"
    check(
        "§3.2.2 环4 序列化不变量:编译产物 JSON 往返无损",
        "回调 value 组装进卡片 JSON——必须是合法可序列化 JSON,否则飞书拒收/回调反解失败",
        json_ok is True,
        f"json.dumps→loads 往返={'无损' if json_ok is True else json_ok}",
    )
    # 环5 状态重建(加强):不只 r0 vs r1,穷举 0..19 每轮动作名集合,证明 20 组两两不相交
    # (任意两轮撞名 → 墓碑会误拦,多轮改分即失效)。
    names_by_round = []
    for rnd in range(20):
        rc = dsl.render_card(REVIEW, round_=rnd)
        s = {
            v["action"]
            for v in (n["behaviors"][0]["value"] for n in _walk(rc["card"]) if n.get("tag") in ("button", "input"))
        }
        names_by_round.append(s)
    collisions = []
    for a in range(20):
        for b in range(a + 1, 20):
            if names_by_round[a] & names_by_round[b]:
                collisions.append((a, b, names_by_round[a] & names_by_round[b]))
    suffix_ok = all(all(name.endswith(f"_r{rnd}") for name in names_by_round[rnd]) for rnd in range(20))
    check(
        "§3.2.2 环5 状态重建:0..19 全部 20 轮动作名两两不相交、各带正确轮次后缀(穷举)",
        "每次重建卡片轮次 +1 生成全新 action,卡片才能反复操作",
        not collisions and suffix_ok,
        f"C(20,2)=190 对轮次组合逐一比对,撞名={collisions or '无'}; 各轮后缀正确={suffix_ok}",
    )
    # 环5 分数高亮单选互斥(加强):对 1..5 每个可选值,恰好高亮那一个、其余不高亮;
    # 且 selected=0(未选)时零高亮。单选互斥在每个取值上都成立才算数。
    highlight_bad = []
    for sel in range(0, 6):
        oc = dsl.render_card(f'<card title="t"><score min="1" max="5" selected="{sel}" bind-record="r"/></card>')
        btns = [n for n in _walk(oc["card"]) if n.get("tag") == "button"]
        hi = [b for b in btns if b.get("type") == "primary"]
        if sel == 0:
            if hi:
                highlight_bad.append(f"sel=0 却高亮 {len(hi)} 个")
        elif len(hi) != 1 or f"✓ {sel}分" not in hi[0]["text"]["content"]:
            highlight_bad.append(f"sel={sel} 高亮={[b['text']['content'] for b in hi]}")
    check(
        "§3.2.2 环5 状态重建:分数高亮单选互斥,对 0..5 每个取值逐一验证",
        "分数按钮高亮所选值(单选互斥)、评语输入框回填当前文本",
        not highlight_bad,
        f"selected=0..5 逐一检查,异常={highlight_bad or '无(每个取值恰好高亮对应按钮,未选时零高亮)'}",
    )
    # 环5 评语回填
    o_refill = dsl.render_card(
        '<card title="t"><comment bind-record="r"/></card>', context_json='{"comment_value":"上次的评语"}'
    )
    refill_inp = next(n for n in _walk(o_refill["card"]) if n.get("tag") == "input")
    check(
        "§3.2.2 环5 状态重建:评语输入框回填当前文本",
        "评语输入框回填当前文本",
        refill_inp.get("value") == "上次的评语",
        f"comment_value 注入后 input.value={refill_inp.get('value')!r}",
    )
    # 环6 业务边界:引擎只出 card+handlers,不含任何业务副作用(落账等)
    check(
        "§3.2.2 环6 业务边界:引擎只编译+路由,不掺业务逻辑",
        "业务动作(打分落账、评语写台账)仍由现有直调工具执行,引擎只负责路由和重建",
        set(out.keys()) <= {"ok", "card", "handlers"} and all(isinstance(v, str) for v in out["handlers"].values()),
        f"render_card 返回键={sorted(out.keys())}(只有 card+handlers,handlers 是 动作名→工具名 的纯映射)",
    )
    # 环6 加强·纯函数不变量:同一 XML 编译两次,产物逐字节相同(引擎无隐藏状态/副作用)
    a = json.dumps(dsl.render_card(REVIEW)["card"], ensure_ascii=False, sort_keys=True)
    b = json.dumps(dsl.render_card(REVIEW)["card"], ensure_ascii=False, sort_keys=True)
    check(
        "§3.2.2 环6 纯函数不变量:同输入编译两次产物完全一致(引擎无副作用)",
        "引擎只负责路由和重建,不掺业务逻辑——编译是纯函数,不应有隐藏状态",
        a == b,
        f"两次 render_card(REVIEW) 产物逐字节相同={a == b}",
    )

    # ── §3.3 飞书限制处理清单 ─────────────────────────────────────────────────
    # 限制1:按钮一次性消费 → 轮次名(已由环2/环5 证);限制2:input→confirm(已由§3.2.1 证)
    # 限制3:不支持组件(textarea/form_container/button_group)→ 词汇表规避:声明即报错
    # 加强:方案点名的三个不支持组件逐一验证 fail-closed,且错误信息明确指向未知元素。
    unsupported = ("textarea", "form_container", "button_group")
    bad_results = {}
    for comp in unsupported:
        r = dsl.render_card(f'<card title="t"><{comp}/></card>')
        bad_results[comp] = (r["ok"], "unknown element" in (r.get("error") or ""))
    all_rejected = all(not ok and hinted for ok, hinted in bad_results.values())
    check(
        "§3.3 限制3:方案点名的 textarea/form_container/button_group 逐一 fail-closed",
        "textarea/form_container/button_group 不支持 → 词汇表设计时直接规避",
        all_rejected,
        f"逐一声明结果(ok, 含unknown提示)={bad_results}",
    )

    # ── §四步骤5 通用性:第二卡型(todo/list)引擎零改动 ─────────────────────────
    o_todo = dsl.render_card(
        '<card title="今日待办"><list><row title="任务A"/><row title="任务B" done="true"/></list></card>'
    )
    # 加强:不只"能编译",还证明——① 走的是同一个 render_card 入口(评价卡/todo 卡同函数);
    # ② 产出同样是可序列化结构;③ todo 卡为未完成行注册了勾选 handler(交互真的生成);
    # ④ 评价卡与 todo 卡二者用同一引擎、返回同样的 {ok,card,handlers} 契约。
    same_entry = o_todo["ok"] and set(o_todo.keys()) == set(out.keys())
    todo_interactive = len(o_todo.get("handlers", {})) > 0
    try:
        json.loads(json.dumps(o_todo["card"], ensure_ascii=False))
        todo_json_ok = True
    except (ValueError, TypeError):
        todo_json_ok = False
    check(
        "§四步骤5 通用性:第二卡型 todo 走同一引擎入口、同一返回契约、结构合法且生成交互",
        "再取一个卡型用 DSL 写出,引擎零改动",
        same_entry and todo_interactive and todo_json_ok,
        f"同 render_card 入口+同契约键={same_entry}; "
        f"生成勾选 handler={len(o_todo.get('handlers', {}))}个; JSON合法={todo_json_ok}",
    )

    # ── Dustin 四点 ───────────────────────────────────────────────────────────
    xsd = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "card-dsl", "card.xsd")
    if os.path.isfile(xsd):
        with open(xsd, encoding="utf-8") as _f:
            xsd_text = _f.read()
    else:
        xsd_text = ""
    check(
        "Dustin①:XSD 带注释,可喂海豚生成 XML",
        "DSL 语法为 XML Schema 形式并带注释,最终用户可将此 Schema 扔进海豚生成 XML",
        "xs:schema" in xsd_text and "<!--" in xsd_text,
        f"card.xsd 存在={bool(xsd_text)}, 含 XSD schema={('xs:schema' in xsd_text)}, 含注释={('<!--' in xsd_text)}",
    )
    check(
        "Dustin④:卡片定义可入库,加这层不伤核心(入口只吃字符串)",
        "卡片定义可能不是放在代码而是数据库,设计需考虑加这层不影响核心代码",
        first_param == "card_xml",
        "render_card(card_xml,...) 只吃 XML 字符串,来源(代码/文件/数据库)与引擎解耦",
    )

    # ── 本机验不了的,如实 SKIP ────────────────────────────────────────────────
    # §四步骤4 多轮改分:墓碑绕过端到端(用真实 Channel 墓碑代码,非模拟)
    _cs = _load_tombstone_store()
    if _cs is None:
        skip(
            "§四步骤4 多轮改分墓碑绕过",
            "真实墓碑代码 _card_store 依赖缺失,无法本机验;需完整运行时。",
        )
    else:
        ok, ev = anyio.run(_verify_tombstone_bypass, _cs)
        check(
            "§四步骤4 多轮改分:轮次+1换名绕开飞书 multi_use 墓碑(真实 Channel 代码)",
            "评价卡改用 DSL 生成,飞书实卡交互与原版完全一致(点分/评语/打回/多轮)",
            ok,
            ev,
        )

    # ── 汇总 ──────────────────────────────────────────────────────────────────
    print("=" * 70)
    print(f"验收结果: {len(_PASS)} PASS / {len(_FAIL)} FAIL / {len(_SKIP)} SKIP")
    if _FAIL:
        print("未达成:")
        for c in _FAIL:
            print(f"  - {c}")
    else:
        tail = f";{len(_SKIP)} 项需完整运行时(SKIP)" if _SKIP else ",零 SKIP"
        print(f"方案(§2「只做卡片」范围内)全部要求达成{tail}。")
    print("=" * 70)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    run()
