# Resend 发信域名 DNS 配置模板

目的：让 Resend 能以你的域名发信（否则只能发往你自己的注册邮箱）。需要在域名 DNS 加
几条记录做 SPF + DKIM 验证。

## 建议用子域发信

不要直接用主域 `example.com` 发信（会和网站/其他邮件的 SPF 纠缠）。
建议用子域，例如 `send.example.com`。发件地址就是 `noreply@send.example.com`。

## 操作顺序

1. Resend 后台 → Domains → Add Domain，填 `send.example.com`。
2. Resend 会**生成该域专属的具体记录值**（DKIM 的公钥、SPF include 等）——**以它页面显示的为准**，下面是记录**类型和位置**的模板，值要用 Resend 给你的。
3. 到域名 DNS 服务商（你的域名解析在哪就去哪，可能是阿里云 DNS）添加这些记录。
4. 回 Resend 点 Verify，等 DNS 生效（通常几分钟到几小时）。
5. 验证通过后，把 `RESEND_FROM=noreply@send.example.com` 和 `RESEND_API_KEY` 给我，我写进服务器 `.env`。

## 记录模板（具体值以 Resend 页面为准）

| 类型 | 主机记录(Name) | 记录值(Value) | 说明 |
|---|---|---|---|
| TXT | `send`（即 send.example.com） | `v=spf1 include:amazonses.com ~all` | SPF，声明允许 Resend(底层 SES) 代发 |
| TXT | `resend._domainkey.send` | Resend 给的一长串 `p=...` 公钥 | DKIM，邮件签名验证 |
| MX | `send` | `feedback-smtp.us-east-1.amazonses.com`（优先级 10） | 退信/反馈回收，Resend 指定 |
| TXT（可选,推荐） | `_dmarc.send` | `v=DMARC1; p=none;` | DMARC，先用 none 观察，不拦截 |

> 注：
> - Resend 现在多数区域用它自己的 SES 基础设施，实际 include 域和 MX 主机名**以 Resend 面板为准**，别照抄上表的 `amazonses.com`——它可能给你 `_domainkey` 用不同名字。
> - 主机记录填法看 DNS 商：有的要填完整 `send.example.com`，有的只填 `send`（它自动补主域）。阿里云 DNS 是填子域前缀。

## 我需要你最终给我的（阶段2b 产出）

1. `RESEND_API_KEY`（Resend 后台 API Keys 生成，给"发送"权限即可）
2. `RESEND_FROM`（如 `noreply@send.example.com`，域名已在 Resend 显示 Verified）

拿到这两个我就能在服务端切到真邮箱通道跑发信验收（阶段4，邮箱链路不依赖 ICP 备案，可先做）。
