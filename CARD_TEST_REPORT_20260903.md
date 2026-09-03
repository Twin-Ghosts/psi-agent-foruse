# psi_card_warn 分支 · Card 代码全量测试报告

- 日期：2026-09-03
- 分支：`psi_card_warn`（commit `1d2319c`）
- 运行环境：Python 3.14.7（仓库 `.venv`），单测按各文件 docstring 的 unittest 直跑方式执行
- 结论：**12 个 card 相关测试文件全部通过（0 失败 / 0 错误），共 302 个用例**

## 一、测试范围与结果

| 测试文件 | 覆盖内容 | 用例数 | 结果 |
|---|---|---|---|
| `_test_card_dsl.py` | DSL 引擎：2.0 编译/校验/Action 六环/模板填充/XML 转义 + XSD vs 引擎一致性 | 64 | ✅ OK |
| `_test_card_dsl_spec.py` | DSL 引擎规格符合性 | 31 | ✅ passed |
| `_test_card_dsl_strict.py` | 严格验收（含 XSD 校验） | 46 | ✅ OK |
| `_test_card_dsl_hardening.py` | DSL 加固回归 | 33 | ✅ OK |
| `_test_card_dsl_fixes.py` | DSL 历史修复回归 | 13 | ✅ OK |
| `_test_card_writeback.py` | 卡片回写逻辑 | 8 | ✅ OK |
| `_test_card_autopush.py` | 卡片自动推送逻辑 | 8 | ✅ OK |
| `_test_review_round.py` | 评审轮次逻辑 | 4 | ✅ OK |
| `_test_report_stats.py` | T3 统计纯函数（文案/健康度取色/去重/请假豁免/分档/报错） | 25 | ✅ OK |
| `_test_report_cards.py` | mentor/boss 报表卡模板 + table/divider 编译 | 20 | ✅ OK |
| `_test_mentor_report_send.py` | T4 mentor 发卡工具（校验/分页/渲染/只读/测试模式/发送） | 19 | ✅ OK |
| `_test_boss_overview_send.py` | T5 boss 统计卡工具（合并统计/逾期 TOP/失败整卡失败/零值占位） | 31 | ✅ OK |

> `_test_card_dsl.py` / `_test_card_dsl_strict.py` 中的 XSD 一致性用例此前因环境缺 `xmlschema` 恒被 skip；本次以 `PYTHONPATH=/tmp/xmlschema_site` 注入该依赖后**真实跑通**，并由此暴露并修复了下方 XSD 缺陷。

## 二、本次修复（2 处）

### 1. `card.xsd`：重复元素定义 + 误删 `date`（测试长期被 skip 掩盖的真 bug）

- 现象：装上 `xmlschema` 后 `xmlschema.XMLSchema(xsd)` 抛 `duplicated value ('col',)`，修复后又抛 `global element 'date' not found`。
- 根因：XSD 中存在**新旧两组** `col` / `table` 全局元素定义（后者为残留旧版，属性集更简、无 `width` / `max_rows` / `more` / `empty` 默认）。XSD 不允许同名全局元素二次声明，导致整份 schema 无法被标准解析器加载——此前因缺 `xmlschema` 从未暴露。
- 修复：删除旧版重复 `col`/`table` 块，保留完整版（含 `width`、`max_rows`、`more`、`empty`）；补回清理过程中连带删除的 `date`（日期选择器）全局元素定义。
- 验证：`TestXSDvsEngine` 用例由 skip → 真实通过（`_test_card_dsl.py` 64/64、`_test_card_dsl_strict.py` 46/46，均无 skip）。

### 2. `feishu_boss_overview_send.py`：逾期天数基准日硬编码 `date.today()`（时间敏感测试漂移）

- 现象：`_test_boss_overview_send.py` 的 `test_card_header_global_and_team_table` 失败——期望 `逾期5天`，实际 `逾期11天`。
- 根因：工具内部 `build_boss_stats(..., today=datetime.date.today())` 硬编码真实当天；而测试 mock 数据固定为 2026-08-28 周期（截止 8-23）。测试在 8-28 当天验收通过，之后任何一天运行都会随真实日期漂移（9-03 跑即 11 天）。
- 修复：工具新增可选参数 `today_iso`（YYYY-MM-DD，仅测试注入用），不传时回退环境变量 `PSI_BOSS_CARD_TODAY`，再回退真实今天——**生产调用保持留空，口径不变**；测试 `_run()` 固定注入 `today_iso="2026-08-28"` 使断言确定。
- 验证：31/31 OK。

## 三、运行方式

```bash
# 进入工具目录（测试与被测模块同目录、PYTHONPATH=.）
cd examples/haitun-workspace/tools

# XSD 一致性用例需要 xmlschema（可选：不装则相关用例 skip，其余照常）
# PYTHONPATH=/tmp/xmlschema_site 为其注入路径

PYTHONPATH=/tmp/xmlschema_site:. python _test_boss_overview_send.py
PYTHONPATH=/tmp/xmlschema_site:. python _test_report_stats.py
# ... 每个 _test_*.py 同理（unittest 直跑，勿用 pytest 收集器，本仓 addopts 有坑）
```

## 四、修改文件清单

| 文件 | 修改 |
|---|---|
| `examples/haitun-workspace/skills/card-dsl/card.xsd` | 删除重复 `col`/`table` 全局定义；补回 `date` 定义 |
| `examples/haitun-workspace/tools/feishu_boss_overview_send.py` | `today_iso` 参数 + `PSI_BOSS_CARD_TODAY` 环境变量兜底 |
| `examples/haitun-workspace/tools/_test_boss_overview_send.py` | `_run()` 注入 `today_iso="2026-08-28"` |

## 五、备注（生产接线提醒，非本次代码缺陷）

- A/B/C 三段提醒卡工具（`feishu_todo_fill_reminder` / `feishu_todo_spec_check` / `feishu_mentor_check_reminder` / `feishu_member_todo_card`）当前**无配套单测**（warn 分支仅 T3/T4/T5 报表卡有测试）；C 卡交互闭环（要求打卡→成员卡→已完成→签收→打回）已按马晨柯 2026-09-03 走查结论定稿（主板表格版式 + 每行可复用按钮，multi_use + 每行唯一 action），尚未固化为工具内实现。建议下一步补齐 A/B/C 工具单测并落盘 C 卡 v4 逻辑。
