# ruff: noqa: RUF001  # 断言文案含全角标点,非歧义字符
"""Standalone unit tests for ``_report_stats.build_mentor_stats``.

Covers the T3 统计口径纯函数:卡面文案(人员/目标/完成/评价概况)、健康度取色
(red/blue/green)、跨人层级去重、请假豁免、未填判定、打分分档,以及行形状
错误/打分非数字的显式报错。口径说明见 ``_report_stats.py`` 模块 docstring。

Runs without the full psi-agent runtime.

Run:  PYTHONPATH=. python -m pytest _test_report_stats.py -q
  or: PYTHONPATH=. python _test_report_stats.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _report_stats import build_mentor_stats


def row(**kw):
    base = {"负责人": "张三", "层级": "todo1", "状态": "已闭环", "mentor打分": 4}
    base.update(kw)
    return base


class TestPeopleSummary(unittest.TestCase):
    def test_filled_leave_overdue_people(self):
        rows = [
            row(负责人="张三", 状态="已闭环"),
            row(负责人="张三", 状态="进行中"),
            row(负责人="李四", 状态="请假顺延"),
            row(负责人="王五", 状态="未闭环逾期"),
        ]
        out = build_mentor_stats(rows)
        self.assertEqual(out["people_summary"], "填报 3 人｜请假 1 人｜未按时填报 1 人")

    def test_person_multiple_rows_counts_once(self):
        rows = [row(负责人="张三"), row(负责人="张三", 状态="进行中")]
        out = build_mentor_stats(rows)
        self.assertEqual(out["counts"]["people"]["filled"], 1)

    def test_no_rows_empty_summaries(self):
        out = build_mentor_stats([])
        self.assertEqual(out["people_summary"], "暂无填报")
        self.assertEqual(out["goal_summary"], "暂无目标")
        self.assertEqual(out["done_summary"], "暂无明细")
        self.assertEqual(out["score_summary"], "暂无打分")

    def test_person_field_object_shapes(self):
        # 人员字段可能展开成 {"id": ...} / {"name": ...},都要能去重。
        rows = [
            row(负责人={"id": "ou_1"}),
            row(负责人={"name": "张三"}),
            row(负责人="李四"),
        ]
        out = build_mentor_stats(rows)
        self.assertEqual(out["counts"]["people"]["filled"], 3)


class TestGoalSummary(unittest.TestCase):
    def test_goal_kinds_counted_by_prefix(self):
        rows = [
            row(负责人="张三", 层级="大目标1"),
            row(负责人="张三", 层级="大目标2"),
            row(负责人="张三", 层级="小目标1"),
            row(负责人="张三", 层级="todo1"),
            row(负责人="张三", 层级="todo2"),
        ]
        out = build_mentor_stats(rows)
        self.assertEqual(out["goal_summary"], "大目标 2｜小目标 1｜TODO 2")

    def test_same_tag_across_people_is_two_goals(self):
        # 台账层级按「同一人内独立编号」,跨人的同名标签是两个目标。
        rows = [
            row(负责人="张三", 层级="大目标1"),
            row(负责人="李四", 层级="大目标1"),
        ]
        out = build_mentor_stats(rows)
        self.assertEqual(out["counts"]["goals"]["big"], 2)
        self.assertEqual(out["goal_summary"], "大目标 2")

    def test_duplicate_tag_same_person_counts_once(self):
        rows = [
            row(负责人="张三", 层级="大目标1"),
            row(负责人="张三", 层级="大目标1"),
        ]
        out = build_mentor_stats(rows)
        self.assertEqual(out["counts"]["goals"]["big"], 1)

    def test_unknown_level_ignored(self):
        rows = [row(层级="随便写"), row(层级="")]
        out = build_mentor_stats(rows)
        self.assertEqual(out["goal_summary"], "暂无目标")


class TestDoneSummary(unittest.TestCase):
    def test_status_buckets(self):
        rows = [
            row(状态="已闭环"),
            row(状态="已闭环"),
            row(状态="进行中"),
            row(状态="待开始"),
            row(状态="已交付"),
            row(状态="未闭环逾期"),
        ]
        out = build_mentor_stats(rows)
        self.assertEqual(out["done_summary"], "已闭环 2｜进行中 3｜逾期 1")

    def test_leave_rows_excluded_from_done(self):
        rows = [row(状态="请假顺延"), row(状态="已闭环")]
        out = build_mentor_stats(rows)
        self.assertEqual(out["done_summary"], "已闭环 1")
        self.assertEqual(out["counts"]["done"]["overdue"], 0)


class TestScoreSummary(unittest.TestCase):
    def test_avg_and_buckets_desc(self):
        rows = [row(mentor打分=5), row(mentor打分=5), row(mentor打分=4), row(mentor打分=3)]
        out = build_mentor_stats(rows)
        self.assertEqual(out["score_summary"], "平均分 4.2｜5分×2｜4分×1｜3分×1")
        # counts.scores.avg 存的是卡面显示口径(已按 score_round 舍入),不是原始均值。
        self.assertEqual(out["counts"]["scores"]["avg"], 4.2)

    def test_all_empty_is_no_score(self):
        rows = [row(mentor打分=""), row(mentor打分=None)]
        out = build_mentor_stats(rows)
        self.assertEqual(out["score_summary"], "暂无打分")
        self.assertIsNone(out["counts"]["scores"]["avg"])

    def test_zero_score_is_valid(self):
        rows = [row(mentor打分=0), row(mentor打分=5)]
        out = build_mentor_stats(rows)
        self.assertEqual(out["score_summary"], "平均分 2.5｜5分×1｜0分×1")

    def test_fractional_score_bucket(self):
        rows = [row(mentor打分=4.5), row(mentor打分=4.5)]
        out = build_mentor_stats(rows)
        self.assertIn("4.5分×2", out["score_summary"])

    def test_string_scores_parsed(self):
        rows = [row(mentor打分="5"), row(mentor打分="3")]
        out = build_mentor_stats(rows)
        self.assertEqual(out["score_summary"], "平均分 4.0｜5分×1｜3分×1")

    def test_non_numeric_score_raises(self):
        rows = [row(mentor打分="abc")]
        with self.assertRaises(ValueError):
            build_mentor_stats(rows)


class TestTemplateColor(unittest.TestCase):
    def test_overdue_is_red(self):
        rows = [row(状态="已闭环"), row(状态="未闭环逾期")]
        self.assertEqual(build_mentor_stats(rows)["template"], "red")

    def test_all_closed_is_green(self):
        rows = [row(状态="已闭环"), row(状态="已闭环")]
        self.assertEqual(build_mentor_stats(rows)["template"], "green")

    def test_active_is_blue(self):
        rows = [row(状态="进行中"), row(状态="已交付")]
        self.assertEqual(build_mentor_stats(rows)["template"], "blue")

    def test_leave_is_exempt_from_green(self):
        # 全员闭环 + 请假顺延:请假是豁免,仍算全员闭环 → green。
        rows = [row(状态="已闭环"), row(负责人="李四", 状态="请假顺延")]
        self.assertEqual(build_mentor_stats(rows)["template"], "green")

    def test_empty_rows_is_blue(self):
        self.assertEqual(build_mentor_stats([])["template"], "blue")

    def test_unfilled_expected_person_is_red(self):
        rows = [row(负责人="张三", 状态="已闭环")]
        out = build_mentor_stats(rows, expected_people=["张三", "李四"])
        self.assertEqual(out["template"], "red")
        self.assertEqual(out["counts"]["people"]["unfilled"], ["李四"])

    def test_expected_people_all_present_not_red(self):
        rows = [row(负责人="张三"), row(负责人="李四")]
        out = build_mentor_stats(rows, expected_people=["张三", "李四"])
        self.assertEqual(out["template"], "green")
        self.assertEqual(out["counts"]["people"]["unfilled"], [])


class TestShapeErrors(unittest.TestCase):
    def test_non_dict_row_raises(self):
        with self.assertRaises(TypeError) as ctx:
            build_mentor_stats([{"负责人": "张三"}, "bad"])
        self.assertIn("row 1", str(ctx.exception))
        self.assertIn("must be an object", str(ctx.exception))

    def test_rows_not_a_list_raises(self):
        with self.assertRaises(TypeError):
            build_mentor_stats({"负责人": "张三"})


if __name__ == "__main__":
    unittest.main()
