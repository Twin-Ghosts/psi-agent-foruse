"""错误码 → HTTP 状态码。**这是 `contract/auth_contract.py` 的副本。**

为什么是副本而不是 import：契约在 psi-agent-auth 仓库里，本服务不能跨仓库
依赖它（部署时那个仓库不在镜像里）。两侧只通过 HTTP 契约耦合，这是那份耦合
在服务端的落地。

副本意味着会漂移。**`自检_契约一致.py` 会逐条比对这张表与契约源文件**，
不一致即报红 —— 漂移必须被机器抓到，靠人记住同步是行不通的。

改这张表的唯一正确顺序：先改 contract/auth_contract.py，再改这里，然后跑
一致性自检确认两边同步。
"""

ERRORS: dict[str, tuple[int, str]] = {
    "invalid_phone": (400, "手机号格式不正确"),
    "invalid_email": (400, "邮箱格式不正确"),
    "invalid_code": (401, "验证码不正确"),
    "code_expired": (401, "验证码已过期或不存在"),
    "temp_token_invalid": (401, "注册凭证无效或已过期"),
    "unauthorized": (401, "登录态失效"),
    "invitation_required": (403, "需要邀请码"),
    "invitation_invalid": (403, "邀请码无效或已被使用"),
    "not_found": (404, "资源不存在"),
    "rate_limited": (429, "请求过于频繁"),
    "provider_error": (502, "上游服务暂时不可用"),
}
