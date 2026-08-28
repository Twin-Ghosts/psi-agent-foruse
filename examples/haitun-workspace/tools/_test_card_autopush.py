"""数据驱动推卡判定(decide_push)的离线单测。

Run: PYTHONPATH=. python -m pytest _test_card_autopush.py -q -o addopts= -o testpaths=
  or: PYTHONPATH=. python _test_card_autopush.py
"""

from __future__ import annotations

import os
import sys
import typing
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _card_autopush as ap


class TestDecidePush(unittest.TestCase):
    RULE: typing.ClassVar = {
        "when": {"field": "状态", "equals": "待审批"},
        "template": "review-card",
        "to_field": "审批人",
        "values": {"owner_name": "负责人", "title": "任务"},
    }

    def test_match_returns_plan_with_mapped_values(self):
        fields = {"状态": "待审批", "审批人": "ou_boss", "负责人": "张三", "任务": "季度复盘"}
        plan = ap.decide_push(fields, [self.RULE])
        self.assertIsNotNone(plan)
        self.assertEqual(plan.template, "review-card")
        self.assertEqual(plan.to, "ou_boss")
        self.assertEqual(plan.values, {"owner_name": "张三", "title": "季度复盘"})

    def test_no_match_returns_none(self):
        fields = {"状态": "进行中", "审批人": "ou_boss"}
        self.assertIsNone(ap.decide_push(fields, [self.RULE]))

    def test_matched_but_no_recipient_skips(self):
        # 命中条件但收件人字段为空 → 不推(不乱发)
        fields = {"状态": "待审批", "审批人": "", "负责人": "张三"}
        self.assertIsNone(ap.decide_push(fields, [self.RULE]))

    def test_first_matching_rule_wins(self):
        rules = [
            {"when": {"field": "状态", "equals": "已完成"}, "template": "t1", "to_field": "审批人"},
            {"when": {"field": "状态", "equals": "待审批"}, "template": "t2", "to_field": "审批人"},
        ]
        plan = ap.decide_push({"状态": "待审批", "审批人": "ou_x"}, rules)
        self.assertEqual(plan.template, "t2")

    def test_in_operator(self):
        rule = {"when": {"field": "优先级", "in": ["P0", "P1"]}, "template": "t", "to_field": "负责人"}
        self.assertIsNotNone(ap.decide_push({"优先级": "P0", "负责人": "ou_a"}, [rule]))
        self.assertIsNone(ap.decide_push({"优先级": "P2", "负责人": "ou_a"}, [rule]))

    def test_present_operator(self):
        rule = {"when": {"field": "驳回原因", "present": True}, "template": "t", "to_field": "负责人"}
        self.assertIsNotNone(ap.decide_push({"驳回原因": "格式问题", "负责人": "ou_a"}, [rule]))
        self.assertIsNone(ap.decide_push({"驳回原因": "", "负责人": "ou_a"}, [rule]))

    def test_array_field_cell_text(self):
        # 收件人是人员字段(数组),取 name/text
        rule = {"when": {"field": "状态", "equals": "待审批"}, "template": "t", "to_field": "审批人"}
        fields = {"状态": "待审批", "审批人": [{"name": "ou_boss"}]}
        self.assertEqual(ap.decide_push(fields, [rule]).to, "ou_boss")

    def test_malformed_inputs(self):
        self.assertIsNone(ap.decide_push(None, [self.RULE]))
        self.assertIsNone(ap.decide_push({"状态": "待审批"}, []))
        self.assertIsNone(ap.decide_push({"状态": "待审批", "审批人": "x"}, ["notadict"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
