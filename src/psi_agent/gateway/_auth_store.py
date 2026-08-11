"""AuthStore —— 登录凭证与设备标识的本机落盘 (系统钥匙串加密)。

与 ``_state.py`` 同区域 (AppData) 但**刻意分开一个文件**: 钥匙串是本仓库里唯一
一处平台相关代码 (Windows→Credential Manager / macOS→Keychain / Linux→Secret
Service), 隔开便于在 CI (无钥匙串) 里替换成注入的假实现。

三点设计取舍:

1. **token 不进 ``state/latest.json``。** 现有 ``GatewayState.save`` 把 AI 的
   ``api_key`` 明文写进快照; 登录凭证不再踩这个坑 —— 快照只存业务配置, 凭证走
   本文件、经钥匙串加密。

2. **钥匙串不可用时降级到明文, 但必须 warning。** 桌面环境千差万别 (Linux 无
   Secret Service、CI 容器无 D-Bus), 硬失败会让整个客户端起不来; 静默降级又
   等于骗人。故降级 + 明确告警, 并在文件里落 ``"enc": false`` 标记, 便于排查。

3. **``device_key`` 与 token 分开存。** 它不是秘密, 但必须**跨重装稳定** ——
   云端 ``devices.UNIQUE(user_id, device_key)`` 靠它保证重装不刷出新设备。放在
   同一个文件里, 即使 token 被清 (登出) 也不重新生成。

保护边界是「操作系统用户」而非「进程」: 同机恶意程序可以当前用户身份解密。这是
桌面客户端的固有限制, 不是本模块的疏漏。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

import anyio
from loguru import logger

from psi_agent._appdata import resolve_appdata_root

# 钥匙串里的条目名。service 固定, username 用来区分不同用途的密钥。
_KEYRING_SERVICE = "psi-agent"
_KEYRING_USERNAME = "auth-store-key"

_FILENAME = "auth.enc.json"


def _new_device_key() -> str:
    """高熵随机串。不含机器指纹 —— 指纹会随硬件变动而变, 反而破坏「重装稳定」。"""
    return secrets.token_urlsafe(24)


def _checksum(plain: str) -> str:
    """明文的短摘要, 用来判断解密结果是否可信。

    只存摘要前 16 位十六进制: 够用来发现「解出乱码」, 又不足以对 token 本身
    做离线暴力破解 (token 是 32 字节高熵随机串)。
    """
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()[:16]


def _xor(data: bytes, key: bytes) -> bytes:
    """用钥匙串里的密钥做流式异或。

    这里**不是**在自制加密算法: 真正的秘密保护由操作系统钥匙串承担 (密钥本身存在
    Credential Manager / Keychain 里, 磁盘上没有), 此处只需让磁盘文件不可直接读出
    token。若将来引入 cryptography 依赖, 换成 AES-GCM 即可, 文件格式已留 ``v`` 字段。
    """
    if not key:
        return data
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


@dataclass
class AuthStore:
    """凭证落盘 ``{appdata}/auth.enc.json`` + ``device_key`` 持久化。

    ``_keyring`` 可注入: 生产传 None → 用 ``keyring`` 库; CI 传假实现避开钥匙串。
    """

    _path: anyio.Path = field(default_factory=lambda: anyio.Path(_FILENAME))
    _keyring: Any = None
    _key_cache: bytes = b""
    _encrypted: bool = True

    @classmethod
    async def from_appdata(cls, appdata_root: str = "", keyring_mod: Any = None) -> AuthStore:
        """建一个落在 *appdata_root* 下的凭证仓 (空 → 自动解析)。"""
        root = appdata_root.strip() or await resolve_appdata_root()
        return cls(_path=anyio.Path(root) / _FILENAME, _keyring=keyring_mod)

    # ---- 钥匙串 ----
    def _load_keyring(self) -> Any:
        if self._keyring is not None:
            return self._keyring
        try:
            # 刻意延迟导入: keyring 是本期新增的唯一第三方依赖, 且在无钥匙串的
            # 环境 (CI 容器 / 缺 Secret Service 的 Linux) 可能装不上。放到顶层会让
            # 「没装 keyring」变成整个 Gateway 起不来, 而不是降级 + 告警。
            import keyring  # noqa: PLC0415
        except Exception as e:
            logger.warning(
                f"keyring 不可用 ({e!r}); 登录凭证将以**明文**落盘。"
                " 这是降级行为, 不是预期状态 —— 桌面环境请安装 keyring 后重启。"
            )
            self._keyring = False
            return False
        self._keyring = keyring
        return keyring

    def _secret_key(self) -> bytes:
        """取 (或首次生成) 加密密钥。密钥只在钥匙串里, 磁盘上没有。"""
        if self._key_cache:
            return self._key_cache
        kr = self._load_keyring()
        if not kr:
            self._encrypted = False
            return b""
        try:
            raw = kr.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
            if not raw:
                raw = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
                kr.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, raw)
                logger.info("已在系统钥匙串中创建凭证加密密钥")
            self._key_cache = raw.encode()
            self._encrypted = True
            return self._key_cache
        except Exception as e:
            logger.warning(
                f"读写系统钥匙串失败 ({e!r}); 登录凭证将以**明文**落盘。"
                " 这是降级行为, 请检查钥匙串服务 (Linux 需 Secret Service)。"
            )
            self._encrypted = False
            return b""

    # ---- 读写 ----
    async def _read_raw(self) -> dict[str, Any]:
        if not await self._path.is_file():
            return {}
        try:
            raw = await self._path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning(f"读取凭证文件 {self._path} 失败: {e!r}")
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"凭证文件 {self._path} 已损坏, 视为未登录")
            return {}
        return data if isinstance(data, dict) else {}

    async def _write_raw(self, data: dict[str, Any]) -> None:
        text = json.dumps(data, ensure_ascii=False, indent=2)
        try:
            await self._path.parent.mkdir(parents=True, exist_ok=True)
            await self._path.write_text(text, encoding="utf-8")
        except Exception as e:
            logger.warning(f"写入凭证文件 {self._path} 失败: {e!r}")

    async def load_token(self) -> str:
        """返回 token; 未登录、解密失败或校验不过均返回空串。"""
        data = await self._read_raw()
        blob = data.get("token", "")
        if not blob:
            return ""
        if not data.get("enc", False):
            return str(blob)
        try:
            plain = _xor(base64.b64decode(blob), self._secret_key()).decode("utf-8")
        except Exception as e:
            logger.warning(f"凭证解密失败 ({e!r}); 视为未登录, 需重新登录")
            return ""
        # 校验和是必需的: 异或本身没有完整性保证, 换了密钥 (钥匙串被重置/换机器)
        # 只会解出乱码而不会报错。没有这一步, 客户端会拿着垃圾 token 去请求,
        # 得到 401 才发现, 而且期间界面一直显示「已登录」。
        want = data.get("sum", "")
        if want and _checksum(plain) != want:
            logger.warning("凭证校验和不匹配 (钥匙串密钥可能已变更); 视为未登录")
            return ""
        return plain

    async def save_token(self, token: str) -> None:
        """写入 token, 保留已有的 ``device_key``。"""
        data = await self._read_raw()
        key = self._secret_key()
        if key:
            data["token"] = base64.b64encode(_xor(token.encode("utf-8"), key)).decode()
            data["enc"] = True
            data["sum"] = _checksum(token)
        else:
            data["token"] = token
            data["enc"] = False
            data.pop("sum", None)
        data["v"] = 1
        await self._write_raw(data)

    async def clear_token(self) -> None:
        """登出: 只清 token, **保留** ``device_key`` —— 否则重新登录会被云端当成新设备。"""
        data = await self._read_raw()
        data.pop("token", None)
        data.pop("enc", None)
        await self._write_raw(data)

    async def device_key(self) -> str:
        """取 (或首次生成并落盘) 设备标识。跨重装稳定。"""
        data = await self._read_raw()
        existing = data.get("device_key", "")
        if isinstance(existing, str) and existing:
            return existing
        fresh = _new_device_key()
        data["device_key"] = fresh
        data.setdefault("v", 1)
        await self._write_raw(data)
        logger.info("已生成本机 device_key")
        return fresh

    @property
    def encrypted(self) -> bool:
        """最近一次读写是否真的用了钥匙串加密 (供 ``GET /auth/status`` 自曝降级)。"""
        return self._encrypted
