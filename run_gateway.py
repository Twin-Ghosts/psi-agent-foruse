"""最小启动器：只跑 psi-agent Gateway（Web 控制台 + 登录），
绕开 cli.py 对全部渠道(飞书/telegram/discord)的 import。

用法：
  # 连本地云端（psi-cloud-port，路由挂在 /api/auth）
  PSI_AUTH_ENDPOINT=http://127.0.0.1:8081 python run_gateway.py

  # 连线上旧版 psi-cloud（路由挂在 /auth）
  PSI_AUTH_ENDPOINT=https://<你的认证域名> PSI_AUTH_PREFIX=/auth python run_gateway.py

  # 不配 endpoint 则整套认证不加载，界面显示「本地 Gateway 模式」
  python run_gateway.py

浏览器打开 http://127.0.0.1:8080/
"""
import os
import sys

# 两种布局都要能跑：
#   交付包            <本文件同级>/src/psi_agent/…
#   开发工作目录      <本文件同级>/psi-agent-full/src/psi_agent/…
# 原先只写死后者，于是交付包里 `import psi_agent` 直接 ModuleNotFoundError ——
# 克隆下来一跑就崩，而崩在 import 阶段、连日志都还没起来。
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (os.path.join(_HERE, "src"),
              os.path.join(_HERE, "psi-agent-full", "src")):
    if os.path.isdir(os.path.join(_cand, "psi_agent")):
        sys.path.insert(0, _cand)
        break
else:
    raise SystemExit(
        "找不到 psi_agent 包：期望在 ./src/ 或 ./psi-agent-full/src/ 下。"
        f"当前目录 {_HERE}")

# Git Bash(MSYS) 会把 "/auth" 这类纯路径值当 Unix 路径，转成
# "C:/Program Files/Git/auth"。只修正这一种被篡改的情况，**不覆盖用户的正常取值**。
#
# 原先这里无条件强写 "/auth"，有两个问题：
#   1. 空值也被当成"要修正"，于是用户没设时也被塞成 /auth；
#   2. /auth 是线上旧版 psi-cloud 的形态。本地 psi-cloud-port 只挂 /api/auth
#      与根路径两套，没有 /auth —— 连本地云端会全部 404。
#
# 现在的规则：只在检出 MSYS 篡改时还原成 /auth（那说明用户本意就是 /auth），
# 其余一律交给 AuthManager 自己解析（未设→默认 /api/auth；设为空串→无前缀）。
# 还原办法：MSYS 只在**开头**插入它的安装前缀，末尾原样保留。所以从被篡改的值
# 里把 "/api/auth"、"/auth" 这类真实前缀切出来即可，不能一律当成 "/auth" ——
# 那是上一版的 bug：设 PSI_AUTH_PREFIX=/api/auth 时 MSYS 转成
# "C:/Program Files/Git/api/auth"，命中 startswith("C:") 且 endswith("/auth")，
# 于是被强写成 "/auth"，而本地 psi-cloud-port 只挂 /api/auth —— 所有认证请求
# 404，界面上就是登录框点「获取验证码」直接报 HTTP 404。
_KNOWN_PREFIXES = ("/api/auth", "/auth")

_pfx = os.environ.get("PSI_AUTH_PREFIX")
if _pfx and ("Git" in _pfx or _pfx.startswith("C:")):
    for _p in _KNOWN_PREFIXES:          # 长的先匹配，避免 /api/auth 被切成 /auth
        if _pfx.endswith(_p):
            os.environ["PSI_AUTH_PREFIX"] = _p
            break

import anyio  # noqa: E402

from psi_agent.gateway import Gateway  # noqa: E402


def main() -> None:
    endpoint = os.environ.get("PSI_AUTH_ENDPOINT", "")
    if endpoint:
        # 前缀对不上会导致所有认证请求 404，是最该先看的一处，故启动即打印
        prefix = os.environ.get("PSI_AUTH_PREFIX")
        shown = "/api/auth（默认）" if prefix is None else (prefix or "（无前缀）")
        print(f"认证已启用：{endpoint}  前缀 {shown}")
    else:
        print("认证未启用：未设 PSI_AUTH_ENDPOINT，界面将显示「本地 Gateway 模式」")
    gw = Gateway(
        listen=os.environ.get("PSI_LISTEN", "http://127.0.0.1:8080"),
        browser=True,  # 启动后自动开浏览器
        auth_endpoint=endpoint,
        verbose=True,
    )
    anyio.run(gw.run)


if __name__ == "__main__":
    main()
