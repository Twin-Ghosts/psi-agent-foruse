# psi-cloud

psi-agent 的云端服务。两个 FastAPI 服务共用一个镜像与一套 `shared/`:

| 服务 | 职责 | 容器端口(仅本机) |
|---|---|---|
| `auth` | 手机号 / 邮箱验证码免密登录 | `127.0.0.1:8081` |
| `analytics` | 官网埋点收集 | `127.0.0.1:8082` |

设计方案见 psi-agent 仓库 `docs/onboarding/psi-agent C端注册登录方案.md`,
本文件只记录本仓库的结构与硬规则,不重复方案内容。

## 目录结构

```
src/psi_cloud/
├── shared/        config.py(env 配置) db.py(SQLite) logging.py(脱敏)
├── auth/          app.py routes.py models.py schema.py identifiers.py
│   └── providers/ base.py mock.py aliyun.py resend.py factory.py
└── analytics/     app.py models.py schema.py
data/{auth,analytics}/    ← 宿主机挂载,容器删除不影响
deploy/                   ← 备份脚本、systemd 单元、Caddy 参考片段
```

`auth` / `analytics` 是同一个包的子包,不是顶层目录 —— 两者是可部署服务、
`shared` 是库,收进 `psi_cloud` 后导入天然成立,不靠 `PYTHONPATH` 拼。

## 硬规则

1. **凭证只从环境变量读。** 不写入任何持久化快照文件。`.env` 已在
   `.gitignore` 中且 mode 600。
2. **归一化必须在入库和限频之前。** 见 `auth/identifiers.py`。否则同一个
   人能注册出多个账号,也能靠变形绕过限频。
3. **校验路径必须限频。** 6 位码只有 100 万种可能。手机验证码由 PNVS
   托管不等于自动获得防爆破。
4. **自己的限频做在调用供应商之前。** 供应商侧限频是最后一道闸,撞到它
   时钱已经花了。
5. **PNVS 校验成功要判两层:** `body.code == "OK"` 且
   `body.model.verifyResult == "PASS"`。只判外层会把「码错」当成「成功」。
6. **SQLite 三个 PRAGMA 必设:** WAL、`busy_timeout`、`foreign_keys`
   (SQLite 默认关闭外键,不显式开则形同虚设)。已封装在 `shared/db.py`。
7. **热备只用 `VACUUM INTO`,不用 `cp`。** WAL 下 `.db` 不含最新提交,
   `cp` 可能拿到撕裂状态。
8. **日志不打印完整手机号 / 邮箱 / 验证码。** 用
   `shared/logging.py` 的 `mask_phone` / `mask_email`。
9. **过期清理必须显式实现。** 没有 Redis TTL 可依赖,
   `send_quota` / `email_codes` / `sessions` 的过期数据不会自己消失 ——
   它以「库里攒垃圾」的方式静默失败。

## Caddy 与容器解耦

**Caddy 原生跑在宿主机上,不在 compose 里。** 配置在
`/etc/caddy/Caddyfile`,片段参考 `deploy/Caddyfile.reference`。

`docker compose down --rmi all -v` 之后:Caddy 照常运行、静态站照常服务、
两个 vhost 变 502、`data/` 与 `/etc/caddy/Caddyfile` 都还在。重新
`docker compose up -d` 即恢复,数据不丢。

## 常用命令

```bash
docker compose build              # 构建(pip 走阿里云镜像,可用 --build-arg 覆盖)
docker compose up -d              # 起两个服务
docker compose ps                 # 看健康状态
docker compose logs -f auth       # 跟日志
docker compose restart auth       # 只重启认证服务
deploy/backup.sh                  # 手动热备(宿主机执行,不依赖容器)
```

健康检查:`curl -s localhost:8081/healthz`、`curl -s localhost:8082/healthz`。
自动文档:`https://account.genuineknowledge.cn/docs`。

## 当前状态

**业务已移植,邮箱通道已通。** 已就位:环境、建表、provider 抽象与 mock、
限频口径、`/healthz`、10 条路由的签名与响应模型(即 OpenAPI 契约),以及
`service.py` 的完整业务实现(发码/校验/建号/会话/设备撤销)。

> 注意:本仓的路由**不是** 501 空壳(那是 `psi-cloud-server` 那套的状态)。

**未实现:** `providers/aliyun.py` 是留空实现,docstring 里记了已确认的参数
与坑 —— 手机通道仍发不出真短信。`providers/resend.py` 已补齐(真实发信已验,
见下)。

## Resend 邮件通道(已打通)

`AUTH_EMAIL_PROVIDER=resend` + `AUTH_RESEND_API_KEY` 即启用。验收用
`python 自检_真实发信.py`(会真发邮件),反向验证用 `--negative`。

三条移植时踩到、已修的坑:

1. **环境变量名要按 `AUTH_` 前缀读。** 配置面(`.env.example` /
   `docker-compose.yml` / `shared/config.py`)统一用 `AUTH_RESEND_API_KEY`,
   而 `real_providers.py` 原先只认裸名 `RESEND_API_KEY` —— 凭据配了却读不到,
   且是**静默**失效(日志只说"凭据不全",看着像没配)。现在两种名字都读。
2. **HTTP 客户端用 httpx,不用 aiohttp。** 本仓 `requirements.txt` 里没有
   aiohttp,`import aiohttp` 会让服务直接起不来。
3. **httpx 的 `trust_env` 必须显式关掉。** 它会读系统代理(Windows 下连注册表
   里的都读,不只 `HTTP_PROXY`),于是自检里发往 `127.0.0.1` 的请求被塞进本机
   代理、假服务器收不到请求,表现为莫名的 **502 空响应体**。aiohttp 默认不读
   代理,所以移植前没这个问题。需要走代理时设 `AUTH_HTTP_TRUST_ENV=true`。

**通道开关口径**:以 `AUTH_EMAIL_PROVIDER` / `AUTH_SMS_PROVIDER` 为准
(`resend` / `aliyun` 即真实)。`AUTH_USE_REAL_PROVIDERS` 是一次性打开两个通道的
总开关,仅兼容既有部署脚本 —— 它从未出现在配置面文档里,只认它会让照
`.env.example` 填好的配置仍然静默停在 mock。

**当前限制**:发件人是 `onboarding@resend.dev`,未验证自有域名,因此
**只能发往 Resend 账号的注册邮箱**(发给第三方域名返回 422
`validation_error`)。要发给任意用户必须先完成自有域名的 SPF + DKIM 验证。

`analytics` 已完整可用 —— 它是原 `collector.py` 的改写,
`POST /api/events` 的路径、入参键名、CORS、返回体、表结构均未变。
