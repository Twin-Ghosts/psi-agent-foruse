# -*- coding: utf-8 -*-
"""psi-agent 认证服务 HTTP 契约（第 0 步产出）。

本文件是**唯一契约来源**：客户端与云端都对着它写，两侧只通过 HTTP 耦合。
内容逐条来自《psi-agent C 端注册登录方案》的「接口」「Token 模型」「限频与
安全」三节，未自行发明字段。有疑问处标了 TODO(评审)，不擅自定稿。

统一前缀 /api/auth。鉴权用 Authorization: Bearer <token>。

    ENDPOINTS      每个端点的方法、路径、请求/响应字段、状态码
    ERRORS         错误码表
    RATE_LIMITS    限频维度（第 3 步据此验证）

刻意不引 pydantic / jsonschema：本期只需要"字段在不在、类型对不对"，
标准库够用，且契约不该为了校验方便而拖第三方依赖。
"""

# ---------------------------------------------------------------- 通用约定

PREFIX = "/api/auth"

AUTH_HEADER = "Authorization"
AUTH_SCHEME = "Bearer"

# token：高熵随机串，不携带信息；服务端只存 SHA256 哈希；60 天绝对上限
TOKEN_ABSOLUTE_TTL_DAYS = 60
# 新用户两段式的中间凭证
TEMP_TOKEN_TTL_MINUTES = 10

# 供两侧共用的字段形状。值为 Python 类型，用于契约测试做类型断言。
USER_SHAPE = {
    "id": str,
    "displayName": (str, type(None)),
    "avatarUrl": (str, type(None)),
    "createdAt": str,
}

DEVICE_SHAPE = {
    "id": str,
    "platform": str,
    "name": (str, type(None)),
    "createdAt": str,
    "lastSeenAt": (str, type(None)),
    "current": bool,        # 是否为发起本次请求的设备
}

# ---------------------------------------------------------------- 端点定义
#
# 每项：
#   method / path          HTTP 方法与相对 PREFIX 的路径
#   auth                   是否需要 Bearer token
#   request                请求字段 -> (类型, 是否必填)
#   responses              状态码 -> 该状态下必含的字段形状（None 表示无体）
#   note                   实现要点，来自方案文档

ENDPOINTS = {
    "sms_send": {
        "method": "POST",
        "path": "/sms/send",
        "auth": False,
        "request": {"phone": (str, True), "invitationCode": (str, False)},
        "responses": {
            200: {"retryAfter": int},
            400: {"error": str},        # 号码格式非法
            403: {"error": str},        # 邀请码门禁开启且未提供/无效
            429: {"error": str, "retryAfter": int},
        },
        "note": "归一化必须在入库和限频之前；限频必须在调用 PNVS 之前",
    },
    "otp": {
        "method": "POST",
        "path": "/otp",
        "auth": False,
        "request": {"email": (str, True), "invitationCode": (str, False)},
        "responses": {
            200: {"retryAfter": int},
            400: {"error": str},
            403: {"error": str},
            429: {"error": str, "retryAfter": int},
        },
        "note": "fire-and-forget，不回报账号是否存在，避免账号枚举",
    },
    "verify_phone": {
        "method": "POST",
        "path": "/verify/phone",
        "auth": False,
        "request": {"phone": (str, True), "code": (str, True),
                    "deviceKey": (str, True), "platform": (str, True)},
        "responses": {
            # 老用户直接登录；新用户拿 tempToken 去 /complete
            200: None,      # 二选一，形状见 VERIFY_OK_EXISTING / _NEW
            400: {"error": str},
            401: {"error": str},        # 验证码错误
            429: {"error": str},        # 校验侧限频：5 次 / 300s
        },
        "note": "PNVS 成功条件是两层：code=='OK' 且 model.verifyResult=='PASS'",
    },
    "verify_email": {
        "method": "POST",
        "path": "/verify/email",
        "auth": False,
        "request": {"email": (str, True), "code": (str, True),
                    "deviceKey": (str, True), "platform": (str, True)},
        "responses": {
            200: None,
            400: {"error": str},
            401: {"error": str},
            429: {"error": str},
        },
        "note": "邮箱验证码由我们自管：命中即删、试错 5 次作废",
    },
    "complete": {
        "method": "POST",
        "path": "/complete",
        "auth": False,
        "request": {"tempToken": (str, True), "displayName": (str, False),
                    "invitationCode": (str, False),
                    "deviceKey": (str, True), "platform": (str, True)},
        "responses": {
            200: {"token": str, "user": dict},
            400: {"error": str},
            401: {"error": str},        # tempToken 无效或已过期（10 分钟）
            403: {"error": str},        # 邀请码门禁
        },
        "note": "手机/邮箱/将来的 OAuth 三种来源都收敛到这一个端点",
    },
    "me": {
        "method": "GET",
        "path": "/me",
        "auth": True,
        "request": {},
        "responses": {200: {"user": dict, "identities": list},
                      401: {"error": str}},
        "note": "每次请求刷新 sessions.last_used_at",
    },
    "logout": {
        "method": "POST",
        "path": "/logout",
        "auth": True,
        "request": {},
        "responses": {200: {"ok": bool}, 401: {"error": str}},
        "note": "撤销本会话，标 revoked_at，即时生效",
    },
    "sessions_list": {
        "method": "GET",
        "path": "/sessions",
        "auth": True,
        "request": {},
        "responses": {200: {"devices": list}, 401: {"error": str}},
        "note": "满足 R5：多设备可见",
    },
    "session_revoke": {
        "method": "DELETE",
        "path": "/sessions/{id}",
        "auth": True,
        "request": {},
        "responses": {200: {"ok": bool}, 401: {"error": str},
                      404: {"error": str}},
        "note": "满足 R5：踢掉某台后该设备下次请求立即 401",
    },
}

# /verify/* 的 200 有两种形状，二选一
VERIFY_OK_EXISTING = {"token": str, "user": dict}
VERIFY_OK_NEW = {"tempToken": str, "isNewUser": bool}

# ---------------------------------------------------------------- 错误码表
#
# 客户端据此决定行为，所以必须稳定。文案可改，code 不可改。

ERRORS = {
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

# 客户端拿到 401 即视为登录态失效：清本地凭证、回登录界面。无静默续期。
CLIENT_CLEARS_CREDENTIALS_ON = 401


# ---------------------------------------------------------------- 限频维度
#
# 第 3 步据此写验证。key 为限频桶的维度，window 秒，limit 次。
# 文档强调两点，都要在第 3 步验：
#   1. 自己的限频必须在调用供应商之前（撞供应商的闸时钱已经花了）
#   2. 归一化必须在限频之前（否则同一个人可绕过限频、注册多个账号）

RATE_LIMITS = {
    "send_per_identifier": {"scope": "identifier", "window": 60, "limit": 1,
                            "satisfies": "R6"},
    "send_per_ip": {"scope": "ip", "window": 60, "limit": 5,
                    "satisfies": "R6"},
    "verify_per_identifier": {"scope": "identifier", "window": 300, "limit": 5,
                              "satisfies": "R7"},
}

# 邮箱验证码自管参数（手机号由 PNVS 托管，我们零存储）
EMAIL_CODE = {"length": 6, "ttl_seconds": 600, "max_attempts": 5}


# 客户端 IP 的取法。服务跑在 Caddy 后面，socket 上看到的是反代的地址，
# 若不读转发头，则**所有用户共用一个限频桶**，send_per_ip 形同虚设（或反过来，
# 一个用户就能把全站发码额度打满）。
CLIENT_IP_HEADER = "X-Forwarded-For"
# 只信任来自反代的转发头；直连时以 socket 地址为准。
# TODO(评审)：Caddy 默认会写 X-Forwarded-For；需确认是否改用 trusted_proxies
# 配合 RFC 7239 的 Forwarded 头，以及公网直连时是否拒绝携带该头的请求。


def bearer(token: str) -> dict:
    """构造鉴权头。两侧共用，避免拼错 scheme。"""
    return {AUTH_HEADER: f"{AUTH_SCHEME} {token}"}


def url(base: str, key: str, **path_args) -> str:
    """拼出某端点的完整 URL。path_args 用于填 {id} 这类占位符。"""
    spec = ENDPOINTS[key]
    path = spec["path"]
    for k, v in path_args.items():
        path = path.replace("{" + k + "}", str(v))
    return f"{base.rstrip('/')}{PREFIX}{path}"


# TODO(评审)：以下三处文档未定稿，实现前需确认
#   1. /verify/* 返回 200 时，老用户与新用户两种形状靠字段区分（token vs
#      tempToken）。是否需要显式 isNewUser: false 让客户端少做分支判断？
#   2. 错误响应体统一为 {"error": "<code>"} 还是 {"error": {...}, "message"}？
#      本文件按前者写，客户端据 code 分支。
#   3. platform 的取值集合（win32 / darwin / linux？）未在文档中列举。
#   4. 客户端 IP 的可信来源（见 CLIENT_IP_HEADER 处的 TODO）。
REVIEW_TODOS = 4
