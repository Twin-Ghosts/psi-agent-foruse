"""路由：把契约的 9 个端点接到 AuthService 上。

**响应形状以 `contract/auth_contract.py` 为准，不以 Pydantic 模型为准。**
客户端侧（psi-agent 的 AuthManager 与 SPA）的断言是照契约写的，脚手架阶段
models.py 里那几个响应模型未经客户端验证，两者冲突处一律服从契约：

    /logout            → 200 {"ok": true}          （不是 204）
    /sessions/{id}     → 200 {"ok": true}          （不是 204）
    /me                → 200 {"user": …, "identities": […]}
    /sessions          → 200 {"devices": […]}

业务逻辑全在 service.py，本文件只做三件事：取 body、取客户端 IP、把
ServiceError 翻成契约的错误体。**不在这里写任何业务判断** —— 否则限频顺序、
归一化前置这些性质就有两个实现地点。

`response_model` 一律不声明：契约的 /verify/* 是两种形状二选一
（老用户 {token,user} / 新用户 {tempToken,isNewUser}），声明 union 会让
FastAPI 按声明顺序做序列化裁剪，可能把字段悄悄裁掉。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from .contract_errors import ERRORS
from .models import (
    CompleteRequest,
    SendEmailRequest,
    SendSmsRequest,
    VerifyEmailRequest,
    VerifyPhoneRequest,
)
from .service import ServiceError

router = APIRouter()

AUTH_SCHEME = "Bearer"


def _svc(request: Request) -> Any:
    svc = getattr(request.app.state, "svc", None)
    if svc is None:  # lifespan 未跑完不应发生，防御性分支
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "service not ready")
    return svc


def _client_ip(request: Request) -> str:
    """跑在 Caddy 后面时 socket 地址是反代的，必须读转发头。

    不读的话所有用户共用一个限频桶，send_per_ip 形同虚设。uvicorn 以
    ``--forwarded-allow-ips`` 决定是否信任该头，信任判断在它那一层做，
    这里只取值。
    """
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _token(authorization: str | None) -> str:
    """取 Bearer token。缺失即 401 unauthorized，与契约错误码一致。

    刻意不用 FastAPI 现成的 security 依赖：那个抛的是 {"detail": …}，
    契约要 {"error": "unauthorized"}。
    """
    prefix = AUTH_SCHEME + " "
    if not authorization or not authorization.startswith(prefix):
        raise ServiceError("unauthorized")
    tok = authorization[len(prefix):].strip()
    if not tok:
        raise ServiceError("unauthorized")
    return tok


Bearer = Annotated[str | None, Header(alias="Authorization")]


@router.get("/healthz", tags=["ops"], summary="健康检查")
async def healthz(request: Request) -> dict[str, object]:
    """Caddy / compose healthcheck 用。只探 DB 可读，不碰供应商。"""
    svc = _svc(request)
    try:
        await svc.store.one("SELECT 1")
    except Exception:  # noqa: BLE001 —— 失败原因不该泄露给公网
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable"
        ) from None
    return {
        "ok": True,
        "service": "auth",
        "provider": getattr(svc.provider, "name", "unknown"),
    }


@router.post("/sms/send", tags=["auth"], summary="发送手机验证码")
async def send_sms(body: SendSmsRequest, request: Request) -> dict[str, Any]:
    return await _svc(request).send_code(
        "phone", body.phone, _client_ip(request), body.invitationCode
    )


@router.post("/otp", tags=["auth"], summary="发送邮箱验证码")
async def send_otp(body: SendEmailRequest, request: Request) -> dict[str, Any]:
    return await _svc(request).send_code(
        "email", body.email, _client_ip(request), body.invitationCode
    )


@router.post("/verify/phone", tags=["auth"], summary="校验手机验证码")
async def verify_phone(body: VerifyPhoneRequest, request: Request) -> dict[str, Any]:
    return await _svc(request).verify(
        "phone", body.phone, body.code, body.deviceKey, body.platform
    )


@router.post("/verify/email", tags=["auth"], summary="校验邮箱验证码")
async def verify_email(body: VerifyEmailRequest, request: Request) -> dict[str, Any]:
    return await _svc(request).verify(
        "email", body.email, body.code, body.deviceKey, body.platform
    )


@router.post("/complete", tags=["auth"], summary="新用户补全资料并建号")
async def complete(body: CompleteRequest, request: Request) -> dict[str, Any]:
    return await _svc(request).complete(
        body.tempToken,
        body.deviceKey,
        body.platform,
        body.displayName,
        body.invitationCode,
    )


@router.get("/me", tags=["session"], summary="当前用户")
async def me(request: Request, authorization: Bearer = None) -> dict[str, Any]:
    return await _svc(request).me(_token(authorization))


@router.post("/logout", tags=["session"], summary="登出（撤销本会话）")
async def logout(request: Request, authorization: Bearer = None) -> dict[str, Any]:
    return await _svc(request).logout(_token(authorization))


@router.get("/sessions", tags=["session"], summary="已登录设备列表")
async def list_sessions(
    request: Request, authorization: Bearer = None
) -> dict[str, Any]:
    return await _svc(request).list_devices(_token(authorization))


@router.delete("/sessions/{device_id}", tags=["session"], summary="踢掉某台设备")
async def revoke_session(
    device_id: str, request: Request, authorization: Bearer = None
) -> dict[str, Any]:
    return await _svc(request).revoke_device(_token(authorization), device_id)


def error_body(exc: ServiceError) -> tuple[int, dict[str, Any]]:
    """ServiceError → (状态码, 契约错误体)。未登记的码按 500，不静默成 200。"""
    code, _msg = ERRORS.get(exc.code, (500, ""))
    body: dict[str, Any] = {"error": exc.code}
    if exc.retry_after is not None:
        body["retryAfter"] = exc.retry_after
    return code, body
