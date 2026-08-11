# psi-agent Gateway — 本机应用进程 + 登录界面

用户下载安装后在本机跑的那个进程：监听 `127.0.0.1`，借浏览器渲染界面，
并把手机号 / 邮箱验证码登录接到云端认证服务上。

交付日期：2026-08-11。

## 这个项目交付什么

**主体是 Gateway**（`src/psi_agent/gateway/`）—— 24 个 Python manager +
两套 SPA 前端。其余顶层包（`ai/` `router/` `session/` `channel/` `subagent/`）
一并收进来，因为 **Gateway 在模块层就 import 了 `Ai` / `Router` / `Session`**
（它负责把这些组件作为子进程拉起），只放 `gateway/` 一个目录跑不起来。

```
run_gateway.py                      最小启动器(只跑 Gateway + 登录, 绕开飞书/TG 渠道的 import)
src/psi_agent/gateway/
├── _auth_manager.py                ★ 登录态持有者 + 云端 HTTP 客户端, 持 device_key
├── _auth_store.py                  ★ 凭证落盘 auth.enc.json(keyring 加密) + device_key 持久化
├── server.py                       ★ REST 路由(含 /auth/*)、静态挂载两套 SPA
├── __init__.py                     ★ Gateway dataclass(auth_endpoint 字段) + CLI
├── _ai_manager.py _session_manager.py _chat_manager.py …  其余 20 个 manager
├── AGENTS.md                       ★ manager 清单与启动步骤(三向同步的落点)
├── spa-v2/                         ★ 主线前端 React 19 + TS + Vite
│   └── src/
│       ├── components/user-hub/    ★ 登录界面(HubLoginPanel / HubOtpInput / UserHub)
│       ├── services/api.ts         ★ REST 封装, 含 /auth/* 认证段
│       ├── services/authFlow.ts    ★ 登录流程状态机
│       └── haitun-agent/           任务卡片、执行步骤面板、工作区视图
└── spa/                            旧版前端 Vue 3(server.py 仍挂载它, /  默认跳 v2)
src/psi_agent/{ai,router,session,channel,subagent}/   Gateway 拉起的兄弟组件
tests/                              ★ AuthManager / AuthStore 的 pytest
pyproject.toml                      依赖与打包(含前端 dist 的 artifacts 声明)
```

★ 标记的是本次登录功能直接涉及的部分。

## 跑起来

```bash
# 依赖
pip install -e .            # 或 uv sync

# 前端(改了 spa-v2/src 才需要)
cd src/psi_agent/gateway/spa-v2 && npm install && npm run build

# 起 Gateway
#   连本地云端(psi-cloud-port, 路由挂 /api/auth)
PSI_AUTH_ENDPOINT=http://127.0.0.1:8081 python run_gateway.py
#   不配 endpoint 则整套认证不加载, 界面显示「本地 Gateway 模式」
python run_gateway.py
```

浏览器打开 `http://127.0.0.1:8080/`。

## 认证的三条硬约束

**1. 安装包不持有任何供应商凭证。** 阿里云 AK/SK、Resend API key 一旦打进
客户端等于公开发布，任何人可提取并盗刷。所以发短信 / 发邮件必须由云端代理，
本项目一行密钥都没有 —— 它只调云端认证服务的 HTTPS 接口。

**2. token 不进浏览器。** 前端从不持有长期 token，登录成功后由
`_auth_store.py` 用 keyring 加密存 `{appdata}/auth.enc.json`。页面脚本一旦
持有凭证，XSS 即等于凭证泄露。前端只知道「当前是否已登录」。

**3. 不新建 `psi_agent/auth/` 顶层包。** 顶层包是微内核**组件**(各有自己的
`run()`、socket、独立进程)，认证不是进程 —— 它没有 socket、不独立部署，
做成顶层包会是唯一一个「不是组件的顶层包」。故做成 Gateway manager，与
`TitleManager` 同级，沿用 `_xxx_manager.py` 平铺结构。

## 零回归

`--auth-endpoint` / `PSI_AUTH_ENDPOINT` 为空时**不创建 AuthManager、不注册
`/auth/*`**，现有本地单用户流程不受影响。认证是旁挂的：不注入 Session 构造
参数、不写 ContextVar、不进 `state/latest.json` 的 manager 快照(凭证不落
明文快照)。Session 层本期零改动。

## 前缀必须与云端对齐(踩过的坑)

`PSI_AUTH_PREFIX` 要与云端路由前缀一致，对不上则**所有认证请求 404**，
界面表现为点「获取验证码」直接报 `HTTP 404`。

| 云端形态 | 前缀 |
|---|---|
| `psi-cloud-port`(本地) | `/api/auth` |
| 线上旧版 `psi-cloud` | `/auth` |

**Git Bash(MSYS) 会篡改这个值**：它把 `/api/auth` 当 Unix 路径转成
`C:/Program Files/Git/api/auth`。`run_gateway.py` 里做了还原 —— 按已知前缀表
从尾部切回真实值(长的先匹配，否则 `/api/auth` 会被切成 `/auth`)。
早先那版无条件强写 `/auth`，正是上面那个 404 的成因。

## 交付物累计与 `[SEND:]`

Agent 在回复里写 SEND 标记(左方括号 + `SEND:` + 绝对路径 + 右方括号)即可
把文件交付给用户，界面按会话累计。两处要点：

- **必须是绝对路径。** 读取发生在 Gateway 侧，它与 Session 的工作目录不一定相同。
- **单个文件读失败不该毁掉整轮回答。** `_chat_manager._file_blob` 读不到时发
  `{"type":"error"}`，前端**只上报、不中断读流** —— 早先前端在这里 `throw`，
  于是模型写了个不存在的路径(如 Windows 上的 `/tmp/xxx`)时，它在标记之后说的
  所有话全被丢掉，用户看到回答凭空截断。回归测试见
  `spa-v2/src/services/chatStream.test.ts`。

## 验证

```bash
PYTHONPATH=src python -m pytest -q     # 28 passed
python -m ruff check src tests         # All checks passed
cd src/psi_agent/gateway/spa-v2
npm run test                           # vitest
npm run typecheck
```

本项目已实测跑通：浏览器登录框 → Gateway `/auth/send-code` → 云端
`/api/auth/sms/send` → 阿里云 PNVS 真实短信 → 输码完成登录建号。

## 已知限制

- **本机 REST 无鉴权(本期不处理)。** Gateway 绑 `127.0.0.1` + 随机端口，
  但不校验凭证；浏览器中任意站点可向本地地址发请求(CSRF / DNS rebinding)，
  而 `POST /sessions/{id}/chat` 能驱动 agent 读写文件、执行工具。随机端口只是
  弱混淆。**登录功能不加剧此风险，但也不修复它**，需单独立项。
- **凭证保护边界是操作系统用户，不是进程。** 同机恶意程序可以当前用户身份解密。
- **登录态不主动回验。** `loggedIn` 只反映本地是否有 token；token 过期 / 设备被
  踢 / 云端重置都要等下一次请求吃到 401 才纠正。离线可用是有意设计，但界面
  当前看不出「未校验」这个事实。
- **离线不可登录**；已登录状态可离线使用。
- **前端构建产物不进版本库。** `dist/` 由 `npm run build` 生成，靠
  `pyproject.toml` 的 `artifacts` 声明进 wheel —— 不构建则安装包没有界面。
