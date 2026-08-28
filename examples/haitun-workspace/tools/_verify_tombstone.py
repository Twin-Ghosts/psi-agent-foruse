"""墓碑绕过端到端验证:方案 §四步骤4「多轮改分」最后一跳。

用**真实的 Channel 墓碑代码**(psi_agent.channel.feishu._card_store,本机可独立加载,
不依赖 lark_channel)验证 DSL 引擎的「轮次+1 换新 action」真能绕开飞书 multi_use 墓碑:

  1. 发一张 multi_use 卡,存 snapshot
  2. 第一次点 review_score_r0 → claimed(命中)
  3. 再点同一个 review_score_r0 → already_consumed(被墓碑拒)=复现原始 bug
     「评分选完无法修改」——不重建、动作名不变,第二次点被墓碑吞
  4. 引擎重建到 round 1 → 动作名变 review_score_r1 → 点它 → claimed(不被拒)
     = 修复生效:轮次+1 换新名,绕开墓碑,改分成功

action_id 全部取自真 _card_dsl 引擎渲染的卡片,不是手写常量。

跑法: python -X utf8 _verify_tombstone.py
"""

# 本文件是墓碑绕过验证脚本,靠 print 输出验证过程与结论——T201 是其本职,豁免。
# ruff: noqa: T201
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types

import anyio

# ── 加载真实 DSL 引擎(stub 运行时依赖)────────────────────────────────────────
_TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _TOOLS)
_i = types.ModuleType("_todo_card_impl")
_i._UNDO_ROUNDS = 20
_i._build_card_from_state = lambda s: {"schema": "2.0"}
_i._tick_action_id = lambda a, b: f"t{a}_{b}"
_i._untick_action_id = lambda a, b: f"u{a}_{b}"
sys.modules.setdefault("_todo_card_impl", _i)
_p = types.ModuleType("_runtime_paths")
_p.agent_dir = lambda: os.path.join(_TOOLS, "..")
sys.modules.setdefault("_runtime_paths", _p)
import _card_dsl as dsl  # noqa: E402

# ── 加载真实墓碑代码(绕过 feishu/__init__.py 的 lark_channel 导入)──────────────
_SRC = os.path.join(_TOOLS, "..", "..", "..", "src")
sys.path.insert(0, os.path.abspath(_SRC))
import psi_agent._appdata  # noqa: E402,F401  (确保依赖可导入)

_name = "psi_agent.channel.feishu._card_store"
_spec = importlib.util.spec_from_file_location(
    _name, os.path.join(os.path.abspath(_SRC), "psi_agent", "channel", "feishu", "_card_store.py")
)
cs = importlib.util.module_from_spec(_spec)
sys.modules[_name] = cs
_spec.loader.exec_module(cs)


REVIEW = '<card title="评价" template="blue"><score min="1" max="5" bind-record="rec"/></card>'


def _score_action_id(round_: int) -> str:
    """从真引擎渲染的卡里取 score 首按钮的 action_id(轮次 round_)。"""
    out = dsl.render_card(REVIEW, round_=round_)
    for el in out["card"]["body"]["elements"]:
        if el.get("tag") == "column_set":
            return el["columns"][0]["elements"][0]["behaviors"][0]["value"]["action_id"]
    raise AssertionError("no score button")


async def main() -> int:
    failures = []

    def expect(label: str, got: str, want: str) -> None:
        ok = got == want
        print(f"[{'PASS' if ok else 'FAIL'}] {label}: status={got!r} (期望 {want!r})")
        if not ok:
            failures.append(label)

    with tempfile.TemporaryDirectory() as appdata:
        mid = "om_verify_tombstone"
        # 真引擎给出的 action_id:r0 首按钮、r1 首按钮
        aid_r0 = _score_action_id(0)
        aid_r1 = _score_action_id(1)
        print(f"引擎渲染的 action_id: round0={aid_r0!r}  round1={aid_r1!r}")
        assert aid_r0 != aid_r1, "轮次+1 后 action_id 必须变化"
        print()

        # 发一张 multi_use 卡(评价卡就是 multi_use),存真实 snapshot
        card = dsl.render_card(REVIEW)["card"]
        await cs.save_card_snapshot(mid, card, appdata, action_handlers={aid_r0: "h", aid_r1: "h"}, multi_use=True)

        # 场景A:复现原始 bug —— 不重建,连点两次同一个 r0 动作名
        c1 = await cs.pop_card_snapshot(mid, appdata, action_id=aid_r0)
        expect("第1次点 r0(首次打分)", c1.status, "claimed")
        c2 = await cs.pop_card_snapshot(mid, appdata, action_id=aid_r0)
        expect("再点同一个 r0(不重建→改分)——应被墓碑拒", c2.status, "already_consumed")

        # 场景B:修复 —— 引擎重建到 round1,动作名换成 r1,点它
        c3 = await cs.pop_card_snapshot(mid, appdata, action_id=aid_r1)
        expect("重建到 r1 后点 r1(轮次+1 换新名)——应命中不被拒", c3.status, "claimed")

    print()
    if failures:
        print(f"结论:{len(failures)} 项未通过 —— {failures}")
        return 1
    print("结论:PASS —— 真实墓碑代码证明:同名重点被拒(原始bug),轮次+1换名绕开墓碑(修复生效)。")
    print("      方案 §四步骤4「多轮改分」的墓碑绕过,用生产 Channel 代码端到端验证通过。")
    return 0


if __name__ == "__main__":
    sys.exit(anyio.run(main))
