"""bind-field 通用回写的离线单测:抽取逻辑(extract_writeback)。

回调 payload 形状取自第 8 轮实卡日志(date_picker/select_static 的 action.option、
button 的 value.score),确保抽取契约与飞书真实回传一致。写回本身需完整运行时,
不在此测(见 _card_writeback.write_back_from_callback)。

Run: PYTHONPATH=. python -m pytest _test_card_writeback.py -q -o addopts= -o testpaths=
  or: PYTHONPATH=. python _test_card_writeback.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _card_writeback as wb


def _cb(tag: str, value: dict, option=None) -> dict:
    """构造一条 card.action.trigger payload(实卡日志同形状)。"""
    action: dict = {"tag": tag, "value": value}
    if option is not None:
        action["option"] = option
    return {"action": action}


class TestExtractWriteback(unittest.TestCase):
    def test_score_reads_value_score(self):
        # 点分:value 带 score + bind_field + record_id
        p = _cb("button", {"action": "review_score_r0", "record_id": "recX", "bind_field": "评分", "score": 4})
        self.assertEqual(wb.extract_writeback(p), ("recX", "评分", 4))

    def test_date_reads_action_option_date_part(self):
        # 日期:action.option = "2026-09-02 +0800",取日期部分
        v = {"action": "pick_r0", "record_id": "recX", "bind_field": "截止日期"}
        p = _cb("date_picker", v, option="2026-09-02 +0800")
        self.assertEqual(wb.extract_writeback(p), ("recX", "截止日期", "2026-09-02"))

    def test_select_reads_action_option(self):
        # 下拉:action.option = 被选 option 的 value
        p = _cb("select_static", {"action": "pick_r0", "record_id": "recX", "bind_field": "驳回原因"}, option="content")
        self.assertEqual(wb.extract_writeback(p), ("recX", "驳回原因", "content"))

    def test_no_bind_field_skips(self):
        # 没声明 bind-field → 不回写(只回调,保持既有行为)
        p = _cb("button", {"action": "review_score_r0", "record_id": "recX", "score": 4})
        self.assertIsNone(wb.extract_writeback(p))

    def test_no_record_id_skips(self):
        p = _cb("button", {"action": "x_r0", "bind_field": "评分", "score": 4})
        self.assertIsNone(wb.extract_writeback(p))

    def test_no_value_skips(self):
        # 有 bind_field/record_id 但拿不到选中值(date 没 option)→ 不回写
        p = _cb("date_picker", {"action": "pick_r0", "record_id": "recX", "bind_field": "日期"})
        self.assertIsNone(wb.extract_writeback(p))

    def test_malformed_payload_skips(self):
        self.assertIsNone(wb.extract_writeback(None))
        self.assertIsNone(wb.extract_writeback({}))
        self.assertIsNone(wb.extract_writeback({"action": "notdict"}))

    def test_score_zero_is_falsy_but_int(self):
        # score=0 是合法整数(虽然业务上分数从1起),契约上仍应抽出而非当空跳过
        p = _cb("button", {"action": "x_r0", "record_id": "recX", "bind_field": "分", "score": 0})
        # _picked_value: score 非 int? 是 int。但 0 会被 "picked is None" 放过、非空字符串检查也过
        got = wb.extract_writeback(p)
        self.assertEqual(got, ("recX", "分", 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
