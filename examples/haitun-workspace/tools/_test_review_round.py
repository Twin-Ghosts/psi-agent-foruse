"""Regression test for the review-card round-bump (六环「轮次+1」).

Bug (found 2026-08-27 adversarial review, confirmed in code):
    ``_render_review_card`` never received a round, so every rebuild rendered
    round 0. Because the review card is ``multi_use=True`` and the Channel dedups
    by ``value.action`` (``review_score_r0`` for *all* five score buttons), the
    second score click on the same card hit the already-consumed tombstone and
    was silently dropped — 改分 never took effect.

Fix:
    ``_render_review_card`` takes ``round_``; the three rebuild paths (score /
    comment / reject) render ``current_round + 1``, so the new buttons carry
    fresh ``review_score_r{n}`` action names and clear the tombstone.

This test stubs every heavy runtime dep so the round logic runs in isolation,
drives the real handlers with fake callbacks, and asserts the rebuilt card's
``value.action`` advanced by exactly one round each time.

Run:  PYTHONPATH=. python -m pytest _test_review_round.py -q -o addopts=""
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import unittest

# ── stub runtime deps so importing _review_card_impl needs no lark/network ──
_captured: dict[str, str] = {}


def _install_stubs() -> None:
    # card-dsl engine stub deps (same shape the other suites use)
    impl = types.ModuleType("_todo_card_impl")
    impl._UNDO_ROUNDS = 20
    impl._build_card_from_state = lambda state: {"schema": "2.0", "_state": state}
    impl._tick_action_id = lambda i, r: f"todo_tick_{i}_r{r}"
    impl._untick_action_id = lambda i, r: f"todo_untick_{i}_r{r}"
    impl._parse_card_state = lambda *a, **k: None
    impl._prepare_row_transition = lambda *a, **k: ("skipped", None)
    sys.modules["_todo_card_impl"] = impl

    paths = types.ModuleType("_runtime_paths")
    paths.agent_dir = lambda: os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    sys.modules["_runtime_paths"] = paths

    # _feishu_impl: capture edit_card_impl's card JSON; _invoke is a no-op OK.
    f = types.ModuleType("_feishu_impl")

    async def _edit_card_impl(message_id, card_json, user_key=""):
        _captured["message_id"] = message_id
        _captured["card_json"] = card_json
        return {"ok": True}

    async def _invoke(req, prefer="tenant", user_key=""):
        return {"ok": True, "data": {}}

    f.edit_card_impl = _edit_card_impl
    f._invoke = _invoke
    sys.modules["_feishu_impl"] = f

    api = types.ModuleType("_feishu_api_impl")

    async def _call_api_impl(*a, **k):
        return {"ok": True}

    api.call_api_impl = _call_api_impl
    sys.modules["_feishu_api_impl"] = api

    # _feishu.bitable helpers used at import time
    feishu_pkg = types.ModuleType("_feishu")
    feishu_pkg.__path__ = []  # mark as package
    sys.modules.setdefault("_feishu", feishu_pkg)
    bitable = types.ModuleType("_feishu.bitable")
    bitable._as_field_map = lambda d: d
    bitable._build_update_record_request = lambda *a, **k: {"_req": a}
    sys.modules["_feishu.bitable"] = bitable

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


_install_stubs()
# 合并后 _test_card_dsl_strict 先于本文件收集并已导入真实 _review_card_impl(绑定真实
# _feishu_impl)。这里必须强制重载:踢掉 sys.modules 里已缓存的 _review_card_impl 再
# import,让它绑定本文件安装的 stub(_captured edit_card_impl)——否则 handler 里的
# _f.edit_card_impl 会指向真实飞书网络调用(顺序互扰,单独跑不出现)。
# 注意:不能动 _card_dsl——strict 套件(真实运行时)的 G03 测试依赖它保持真实,
# _build_todo_card 在函数内延迟 import 它,执行期再换 stub 版会让 G03 断言错乱。
sys.modules.pop("_review_card_impl", None)
import _review_card_impl  # noqa: E402


def _score_action_of(card_json: str) -> str:
    """Pull the score buttons' shared ``value.action`` out of a rebuilt card."""
    card = json.loads(card_json)
    for el in card["body"]["elements"]:
        if el.get("tag") == "column_set":
            for col in el.get("columns", []):
                for sub in col.get("elements", []):
                    if sub.get("tag") == "button":
                        v = sub["behaviors"][0]["value"]
                        if isinstance(v, str):
                            v = json.loads(v)
                        if str(v.get("action", "")).startswith("review_score_r"):
                            return v["action"]
    raise AssertionError("no score button found in rebuilt card")


def _click(action: str, round_: int, score: int | None = None) -> str:
    """Build a fake score-click callback carrying the current round."""
    value = {
        "action": f"{action}_r{round_}",
        "round": round_,
        "record_id": "recABC",
        "title": "写方案",
        "owner_name": "黄子建",
    }
    if score is not None:
        value["score"] = score
    return json.dumps({"message_id": "om_test_1", "action": {"value": value}}, ensure_ascii=False)


class RoundBumpTest(unittest.TestCase):
    def setUp(self) -> None:
        _captured.clear()

    def test_render_accepts_round_and_bumps_action_name(self) -> None:
        card, handlers = _review_card_impl._render_review_card(
            record_id="recABC",
            title="t",
            owner_name="o",
            owner_open_id="",
            cycle_date="",
            task_guid="",
            selected_score=0,
            comment_value="",
            round_=3,
        )
        self.assertEqual(_score_action_of(json.dumps(card, ensure_ascii=False)), "review_score_r3")
        # every round up to _MAX_ROUNDS is still pre-registered, so r3 has a handler
        self.assertIn("review_score_r3", handlers)

    def test_round_of_value_reads_int_then_action(self) -> None:
        self.assertEqual(_review_card_impl._round_of_value({"round": 4}), 4)
        self.assertEqual(_review_card_impl._round_of_value({"round": "2"}), 2)
        self.assertEqual(_review_card_impl._round_of_value({"action": "review_score_r5"}), 5)
        self.assertEqual(_review_card_impl._round_of_value({"round": True}), 0)  # bool must not read as 1
        self.assertEqual(_review_card_impl._round_of_value({}), 0)

    def test_score_click_rebuilds_next_round(self) -> None:
        # first click arrives on round 0 → rebuild must be round 1
        asyncio.run(_review_card_impl._handle_score_select(_click("review_score", 0, score=3)))
        self.assertEqual(_score_action_of(_captured["card_json"]), "review_score_r1")
        # second click (改分) now arrives on round 1 → rebuild round 2 (fresh action, no tombstone)
        asyncio.run(_review_card_impl._handle_score_select(_click("review_score", 1, score=5)))
        self.assertEqual(_score_action_of(_captured["card_json"]), "review_score_r2")

    def test_reject_click_rebuilds_next_round(self) -> None:
        asyncio.run(_review_card_impl._handle_review_reject(_click("review_reject", 2)))
        self.assertEqual(_score_action_of(_captured["card_json"]), "review_score_r3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
