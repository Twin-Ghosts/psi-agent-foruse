# 安装流程改动 · CI 验证报告

分支：`caogao20260815`（fork `Twin-Ghosts/psi-agent-foruse`）
记录时间：2026-08-15

本报告如实记录本次改动的验证状态，分「已验证通过 / 有疑点未完全确认 / 未完成」三类，
不夸大。

---

## 一、本次改动内容

| 提交 | 内容 |
|---|---|
| `fb8d6001` | 安装向导加入《软件许可及服务协议》《隐私保护政策》同意页 + 双护栏 |
| `61c0f962` | 上游模型错误优雅呈现，修前端 fail to fetch |
| `c81cc06a` | 安装流程全量测试 + Windows 不支持的 unix-socket 测试加 skipif |
| 本次 lint 修复 | 修 57 个 lint 错误（见下），使其能过 CI lint 门槛 |

---

## 二、CI 门槛与验证结果

CI（`.github/workflows/ci.yml`）的 lint 阶段依次跑三项，全过才进测试：
`ruff check .` → `ruff format --check .` → `ty check .`

### ✅ 已在本机验证通过

- `ruff check .` → **All checks passed**
- `ruff format --check .` → **542 files already formatted**
- installer 全量测试 `tests/installer/`（20 项，含 ISCC 真编译）→ **20 passed**
- Windows 全量 pytest → **0 failed**（unix-socket 测试按平台 skip）

### ⚠️ 有疑点、本机无法 100% 确认

- `ty check .` 在**本机（Windows）**报 2 个 `os.killpg` 的 `unresolved-attribute`，
  位置在 `examples/haitun-workspace/tools/run_flow.py:1100/1108`。
  - 该文件**非本次改动**（最后修改是 PR #573，与本任务无关）。
  - `os.killpg` 是 POSIX API，Windows 无、Linux 有；函数名即 `_signal_posix_process_group`。
  - ty 未配 `python-platform`，按运行平台推断：**本机 Windows 才报，Linux CI 上 `os.killpg`
    存在，预期不报**。
  - **诚实边界**：此结论基于 ty 平台推断机制的分析，**未在 Linux 环境实测证明**。

### 说明：为何之前「本地测试 0 failed」不等于「过 CI」

本地 pytest 通过 ≠ 过 CI。CI 先跑 lint（ruff + ty），lint 不过根本走不到测试。
最初提交 `c81cc06a` 带 57 个 lint 错误（下节），CI 会在 lint 阶段直接失败——
本次 lint 修复即为解决此问题。

---

## 三、本次修复的 57 个 lint 错误

| 规则 | 数量 | 原因 | 修法 |
|---|---|---|---|
| RUF001/002/003 | ~33 | 中文注释/字符串用了全角标点 `，：；` | 改半角 |
| I001 | 11 | `from tests.conftest import` 插在 import 块中间，打乱 isort | ruff --fix 重排 |
| E402 | 8 | pytestmark 插在 import 之前，后续 import 被判「不在顶部」 | pytestmark 移到所有 import 之后 |
| F401 | 1 | installer 测试 import 了未用的 `tempfile` | 删除 |
| PLC0415 | 1 | `import os` 写在函数内 | 提到文件顶部 |
| SIM112 | 1 | 环境变量 `SystemRoot` 未大写 | 改 `SYSTEMROOT` |
| ty no-matching-overload | 2 | `ISCC_PATH: str \| None` 传入 subprocess.run | 加 `assert ISCC_PATH is not None` 收窄 |

修后本机 `ruff check .` / `ruff format --check .` 全过。

---

## 四、安装流程测试覆盖点（tests/installer/）

把真实踩过的坑固化为测试：
- `.iss` UTF-8 BOM（缺则中文乱码）
- Pascal `{ }` 注释不得嵌 `{...}`（否则编译语法错）
- `TNewCheckBox` 不用 WordWrap（否则 Unknown identifier）
- 静默安装 `WizardSilent()` 放行（否则同意门禁卡死自动更新）
- 编译期护栏（缺失/占位 haitun.exe 拒绝打包）
- 运行期护栏（ExeLooksValid + 主程序损坏中文提示，防系统 216）
- 「查看」按钮文件名 == `[Files]` dontcopy 打包名
- CustomMessages 的 Legal* 键中英文齐全
- 两份协议 HTML 完整（UTF-8 / privacy 含表格 / 自包含无外链）
- ISCC 真编译：有效 exe 编译成功、占位 exe 被护栏拒绝（有 ISCC 时跑，否则 skip）

---

## 五、未完成 / 待处理

1. **推送主仓库受阻**：推 `genuineknowledge/psi-agent` 返回 **403**，当前凭据
   `Twin-Ghosts` 对该仓库无写权限。需：走 fork+PR，或由有权限账号推，或加 collaborator。
   本改动目前只在 fork `psi-agent-foruse` 上。
2. **CI 未实跑**：以上为本机验证 + 分析，尚未在真实 CI（Linux）跑过一轮确认全绿。
3. **免费模型 key**：`misakamikoto` 上游 key（尾号 5c2d）在服务端失效——属服务端问题，
   客户端改不了；本次已让其错误优雅呈现（不再 fail to fetch）。
