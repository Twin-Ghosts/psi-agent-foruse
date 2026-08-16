# computer_use 功能增强报告

分支：`mouse-on-latest`（基于官方最新 main，含 #688 免费模型 + 协议前移）
目标仓库：`Twin-Ghosts/psi-agent-foruse`

本报告记录本轮对 `computer_use`（桌面/浏览器自动化工具）及网关的功能增强、
修复的真实缺陷、测试与验证状态，以及诚实的边界。

---

## 一、动机

`computer_use` 是海豚 Agent 用来驱动桌面（截图/点击/输入/拖拽）的工具，底层调
外部 `cua-driver`。使用中暴露出多个真实问题：

1. 海豚在对话里调 computer_use 时误报 **“cua-driver CLI not found”**，退回
   pyautogui/OCR 等错误路径（实为 cua-driver 已装、只是宿主进程 PATH 未刷新）。
2. cua-driver 升级到 0.20 后**截图工具改名**，旧映射失效。
3. 想点海豚**网页里的按钮**（如“新交付物”）时，cua-driver 的 UIA 树读不到网页
   DOM，且 CDP 工具附加不上没开调试端口的浏览器。
4. 主程序（PyInstaller onefile）启动解压到 C 盘 `%TEMP%`，**C 盘满时启动崩溃**。

---

## 二、功能增强与修复（8 个提交）

### 1. computer_use 重构为跨平台架构（72970242）
- 拆成「薄分发器 `computer_use.py` + 私有包 `_platforms/`」。
- 公开签名（19 参数）与 docstring **完全不变**，对上层零影响。
- `_platforms/__init__.py`（按 `sys.platform` 选后端）/ `base.py`（cua-driver 调用、
  参数合并、截图落盘、诊断、dispatch 状态机，mac/win 共用）/ `mac.py`（全量动作，
  REFUSALS 空）/ `win.py`（能力账本 REFUSALS + 安装/授权提示）。
- `_platforms/` 是私有包，工具注册器非递归 `*.py` glob 不会扫到，不外泄成工具。

### 2. 等价性测试（5493d84a）
- 对比重构前后产出的 cua-driver 命令逐场景一致；覆盖签名不变、平台选择、
  REFUSALS 拦截、未知平台清晰报错、注册器不泄漏。

### 3. 真机鼠标控制测试（7a332107，后 4f05f116 加固）
- 用 Win32 `user32` 真的驱动物理光标：读位置→移动方形路径→原地点击→还原。
- 设 DPI 感知消除缩放漂移；`skipif` 非 Windows/无桌面（Linux CI 跳过，不误红）。
- 加固：固定屏幕中心坐标 + 整条路径重试，抗并发人手干扰导致的偶发 flake。

### 4. 适配 cua-driver 0.20 截图工具改名（b93ac9c5）
- `capture` mode=vision/som → `get_desktop_state`；mode=ax → `get_accessibility_tree`。
- 这两个工具 schema 为 `additionalProperties:false`，不再向它们发 `mode`/`app`。
- 加严格测试：各 mode 映射到正确工具名、不发多余字段、som 带截图落盘、ax 无图。

### 5. TEMP/TMP 重定向到安装目录（656ece9a）
- 启动器 `haitun.c` 把 `TEMP`/`TMP` 指向 `{app}\temp`。
- 修复 **C 盘满导致 psi-agent.exe 解压失败、网关起不来** 的启动崩溃。
- 真机验证：解压落 `D:\HaiTun-Agent\temp`，C 盘无新 `_MEI`，主页 200。

### 6. 网关调试端口 + 浏览器动作（02383c89）
- 网关加 `--browser-debug-port`：>0 时用 Edge/Chrome 带 `--remote-debugging-port`
  启动（独立 user-data-dir），cua-driver 可经 CDP 附加该页、驱动网页 DOM 元素。
  默认 0=关（保持现状）；开启时日志告警（CDP 端口=本机任意程序可驱动该浏览器）。
- computer_use 加 `browser_navigate/click/type/pointer/prepare` 与 `get_browser_state`
  一等公民动作：只发浏览器认的字段（coordinate/text），其余（pid/ref/url）经 `args`
  透传，不塞桌面字段，也不附加截图 flag。
- 加 7 项 browser 动作 mock 测试。
- 真机验证：网关带调试端口起 Edge → 9333 端口 CDP 可访问 → computer_use
  `browser_prepare` 成功附加（`already_prepared`）。

### 7. cua-driver 路径回退修复（c3a45534）——本轮核心
- **根因**：`preflight()` 只用 `shutil.which("cua-driver")` 查 PATH；宿主进程
  （安装前已启动的网关、或 PATH 未刷新的 shell）看不到安装器写入的 PATH 条目，
  于是误报 “not found”，上层 agent 退回 pyautogui/OCR。
- **修复**：`_resolve_bin()` 先查 PATH，再回退已知安装位置——
  `%LOCALAPPDATA%\Programs\Cua\cua-driver\bin`、`~/.cua-driver/packages/current`、
  `~/.local/bin`、`/usr/local/bin`；`preflight` 用 isfile 或 which 判定。
- **实测**：清空 PATH 仍解析到 exe、preflight 返回 OK、doctor 正常、不再误报未安装。

---

## 三、测试与验证

### 自测（本分支核心，全绿）
- computer_use dispatcher 测试（含 browser 动作）+ 真机鼠标测试：**39 passed**。
- `ruff check` / `ruff format --check` / `ty check`：**全过**。

### 真机端到端验证
- cua-driver 0.20 装好、daemon 运行；computer_use `doctor`/`list_tools`/`list_apps`/
  `get_desktop_state`（真截图 PNG）/`get_accessibility_tree`（真进程树）全部成功。
- 网关带调试端口启动 → CDP 可访问 → computer_use `browser_prepare` 附加成功。
- 清空 PATH 下 computer_use 仍能定位 cua-driver、doctor 正常（路径回退修复生效）。

### 全项目全量
- **56 failed / 1336 passed**。失败**全在 session/test_server、channel、schedule 等
  环境相关模块**，我改的 computer_use/mouse/gateway 模块**零失败**。
- 这 56 个是 **Windows 环境固有失败**（asyncio 不支持 Unix domain socket + 需要 uv
  子进程），与本轮改动无关，且在 Linux CI 上不复现（另一分支曾用 `skipif(win32)`
  将同一批降到 0，本分支从 origin/main 建，未带那些 skip 标记）。

---

## 四、诚实的边界（未完成 / 待人工）

1. **“网页版对话里完整跑一次 computer_use 任务”** 只验证到直接根因（不再误报未安装、
   doctor 正常、CDP 能附加）；海豚在真实对话中端到端跑截图/点击需人在网页发指令触发。
2. **“回复只显示最后一段”前端渲染问题**：已查明后端流聚合、历史存储、前端文本累积
   逻辑都与仓库一致、均正确（追加而非替换）；疑点在前端**按工具调用分段渲染**那层，
   **本轮未修**。
3. **CDP 调试端口是安全权衡**：开启时任何本机程序可驱动该浏览器；默认关闭、显式开启、
   日志告警。用于点已登录页面时，需浏览器以调试端口启动。
4. **免费模型 401（misakamikoto 上游 key 5c2d 失效）**：属服务端问题，客户端无此 key、
   也改不了；已另配 DeepSeek 官方 + 用户自有 key 作可用替代。
5. 全量测试的 56 项 Windows 失败若要在本机也变绿，需补 `skipif(win32)`（unix-socket
   类）+ 装 uv/tzdata（子进程/时区类）——本轮未做，因它们非本功能范畴。

---

## 五、提交清单（相对 origin/main）

```
c3a45534 fix(computer_use): PATH 找不到 cua-driver 时回退已知安装路径
02383c89 feat(computer_use+gateway): 支持驱动海豚网页元素(CDP)
656ece9a fix(launcher): TEMP/TMP 重定向到 {app}\temp，避免 C 盘满导致启动失败
4f05f116 test(mouse): 真机移动测试整轮重试，抗并发人手干扰
b93ac9c5 fix(computer_use): 适配 cua-driver 0.20 截图工具改名 + 加严格测试
7a332107 test(mouse): 加真机鼠标控制测试(Windows,可逆)
5493d84a test(computer_use): 加薄分发器重构的等价性测试
72970242 refactor(computer_use): 拆为薄分发器 + _platforms 私有运行时包
```
