# psi-agent-auth

psi-agent C 端注册登录的云端认证服务。手机号走阿里云号码认证服务（PNVS），
邮箱走 Resend，单机 SQLite。

客户端（psi-agent）与本服务只通过 HTTP 契约耦合，契约定义在 `contract/`。

## 为什么服务端存在

安装包不能持有任何供应商凭证 —— 阿里云 AK/SK、Resend API key 一旦打进客户端，
等于公开发布，任何人可提取并盗刷。所以发短信 / 发邮件必须由服务端代理。

同理，本机进程无法信任其运行环境（用户本人即机器管理员），所有授权判定都在云端。

## 当前进度

| 步 | 内容 | 状态 |
|---|---|---|
| 0 | HTTP 契约冻结 + 契约测试 | 完成 |
| 1 | SQLite schema + 约束验证 | 完成 |
| 2 | token / session 层 + `/me` `/sessions` `/logout` | 完成 |
| 3 | 限频与配额（mock provider 阶段验完） | 完成 |
| 4 | 部署配置（Caddy + Compose + 热备） | 完成（服务器侧五条待你验） |
| 5 | 邮箱链路接 Resend | 适配层完成，**真实发信待凭据** |
| 6 | 手机链路接 PNVS | 适配层完成，**真实发信待凭据** |
| 7 | 客户端 AuthManager（psi-agent 仓库） | 等仓库路径 |
| 8 | SPA v2 登录界面 | 等仓库归属决定 |
| 9 | 邀请码门禁（默认关闭） | 完成 |

第 5、6 步的适配层已写完并验证：`app/real_providers.py` 里的 `ResendProvider`
与 `PnvsProvider`，签名规则、请求体、响应解析、错误码映射、两层成功判据、通道
分发全部对着本地 mock 验过（65 项 + 6 个破坏点）。**唯一没验的是"真的收到
邮件/短信"** —— 那需要服务器上的真实凭据。

RPC 签名已对过阿里云官方固定参数示例，逐字节一致，该向量固化为常驻回归项
（`自检_供应商.py` 的 [0] 段）。独立复核脚本见 `tools_签名官方向量.py`。

启用真实通道：设 `AUTH_USE_REAL_PROVIDERS=true` 并配齐凭据；凭据不全会回落到
mock 并在启动日志打警告（不静默失败）。

`deploy/验收清单.md` 里那五条（HTTPS 可访问、测试钩子不可达、重启不丢库、
真实 IP 透传、备份可恢复）只能在服务器上验，自检不假装覆盖。

步骤顺序按"每步都能独立验证、依赖单向、验证成本递增"编排：契约先冻结，限频在
mock 阶段验完（撞供应商的闸时钱已经花了），部署提前到真实链路之前（Resend 要
DNS 验证、PNVS 要公网环境，localhost 收不到信），邀请码默认关闭故放最后。

## 验收

```bash
python verify.py            # 全部验收（正向 + 反向）
python verify.py --quick    # 只跑正向
```

**每一步的验收都必须包含反向验证。** 全绿本身不构成证据 —— 必须确认"故意弄坏
会变红"，否则不知道这些断言是否真的在约束什么。目前 `verify.py` 17 / 17：

| 自检 | 正向 | 破坏点 |
|---|---|---|
| 契约（空服务 / 参考实现） | 0/51 全红、60/60 全绿 | 5 |
| schema | 28/28 | 8 |
| 服务层（库内性质） | 25/25 | 6 |
| 真实服务跑契约 | 60/60 | 4 |
| 限频 | 27/27 | 8 |
| 邀请码 | 19/19 | 5 |
| 部署 | 48/48 | 9 |
| 供应商适配 | 65/65 | 6 |

破坏点必须选**必然**改变行为的，并且断言要正向而非"没出现某个词"。这两条都是
踩过坑才写下的：曾用"只比摘要前两位"这种概率性破坏点（1/256 才生效），68 项
全绿、反向验证失效；也曾因破坏点只是把 SQL 改出语法错，导致"抓到"其实抓的是
语法而非约束；还写过"没出现 cp 就算用了 VACUUM INTO"这种弱断言，把
VACUUM INTO 注释掉的脚本会照样通过。

## 目录

```
contract/
  auth_contract.py    唯一契约来源：9 个端点、错误码表、限频维度
  契约测试.py          60 项，可打任意 base URL
  参考实现.py          一次性脚手架，仅证明契约可满足
app/
  store.py            SQLite 存取，串行化所有库访问
  service.py          业务层：发码 / 校验 / 建号 / 会话 / 撤销 / 邀请码
  providers.py        发码通道抽象 + MockProvider
  real_providers.py   Resend / PNVS 适配 + 按通道分发
  server.py           HTTP 层（标准库；换 aiohttp 时只改这个文件）
deploy/
  Caddyfile           自动 HTTPS、安全头、真实 IP 透传
  docker-compose.yml  数据挂 volume、healthcheck、凭据强制注入
  backup.sh           VACUUM INTO + integrity_check
  验收清单.md          只能在服务器上验的那几条
schema.sql            7 张表
自检_*.py             各步自检，均带 --negative
verify.py             统一验收入口
```

## SQLite 注意事项

`journal_mode` 是数据库级、持久化的，设一次即可。但 **`foreign_keys` 与
`busy_timeout` 是连接级的，应用每开一个连接都要重设** —— 写在 schema.sql 里
只对建库那一个连接有效。漏掉就等于没有外键约束（SQLite 默认 `foreign_keys=OFF`）。

数据文件必须挂 volume，不能留在容器层。备份用 `VACUUM INTO` 或 `.backup`，
不能直接 `cp`。

过期数据没有 TTL 兜底：`send_quota` 窗口、`email_codes` 过期、`sessions` 过期
都要自己清。漏清不会报错，只会慢慢积垢，故列入验收。

## 凭据

全部走环境变量，不进任何持久化配置快照：

```
ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET
DYPNS_SIGN_NAME / DYPNS_TEMPLATE_CODE
RESEND_API_KEY / RESEND_FROM
EMAIL_CODE_SALT
```

`.gitignore` 已排除 `.env`、`*.db`、`*.pem`、`*.key`。

## 待评审确认

契约里有 4 处 `TODO(评审)`，实现前需定稿：

1. `/verify/*` 老用户是否显式返回 `isNewUser: false`
2. 错误响应体格式：`{"error": "<code>"}` 还是带 `message` 的嵌套结构
3. `platform` 的取值集合
4. 客户端 IP 的可信来源 —— 服务跑在 Caddy 后面，若不读 `X-Forwarded-For`，
   所有用户共用一个限频桶，`send_per_ip` 形同虚设
