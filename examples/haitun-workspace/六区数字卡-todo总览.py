#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全公司 TODO 总览卡（回退版 = v9 boss 总览卡样式）。

历史：
  v10（2026-08-29 14:22）在 v9 基础上新增「全员一览」31 人逐行，标题改
  「📋 全公司 TODO 总览 · 8.28 第16周期 · 31人」。
  马晨柯 2026-08-29 14:24 指示「总览回退到这一版」，回退到 v9 boss 样式：
    - 标题：📊 全公司 TODO 总览 · {latest} 第{cycle}周期
    - 三行灰底核心数字（①填报/在册/未按时 ②已闭环/进行中/逾期 ③团队/请假/考勤异常）
    - 团队维度 / 台账总览 / 已闭环 / 进行中 / 逾期 / 请假 / 考勤 / 趋势 → 全部走链接
    - 无「全员一览」逐行、无一句话结论
  实现：直接委托 v9 的 build_boss_v6（唯一权威构建函数），不复制逻辑。

产出：
    六区数字卡-todo总览.json   todo 总览卡（回退版，发卡用）
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

WS = Path(__file__).resolve().parent
sys.path.insert(0, str(WS / "mentor-cards"))

_V9 = importlib.util.spec_from_file_location(
    "v9cards", WS / "六区数字卡-真实卡片v9.py")
V9 = importlib.util.module_from_spec(_V9)
sys.modules["v9cards"] = V9
_V9.loader.exec_module(V9)

import build_cards as B  # noqa: E402


def build_todo_overview(people, latest, date_cols, exempt, join_map):
    """回退版 = v9 boss 总览卡（build_boss_v6 原样输出）。"""
    return V9.build_boss_v6(people, latest, date_cols, exempt, join_map)


def main() -> int:
    date_cols, people = B.load_people(None)
    if not date_cols:
        print("[err] 没有周期列", file=sys.stderr)
        return 1
    latest = date_cols[-1]
    leave_window, att_window = B.runtime_windows(latest)
    exempt = B.leave_exempt_names(leave_window)
    join_map = B.join_dates()

    card = build_todo_overview(people, latest, date_cols, exempt, join_map)
    dest = WS / "六区数字卡-todo总览.json"
    dest.write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[ok] todo 总览卡（回退版）→ {dest}")
    print(f"[data] 最新周期 {latest}（第 {len(date_cols)} 周期）· 数据截至 "
          f"{B.data_as_of()} · 考勤窗口 {att_window}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
