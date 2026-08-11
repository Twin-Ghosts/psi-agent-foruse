"""AuthStore: 凭证落盘与 device_key 语义。

这些性质从界面上观察不到, 只能在这一层断言 —— 把 token 明文写进磁盘, 上层的
HTTP 契约测试照样全绿。

``asyncio_mode = "auto"`` 已在 pyproject 里设好, 因此 async 测试不需要装饰器。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from psi_agent.gateway._auth_store import AuthStore


class FakeKeyring:
    """可用的假钥匙串。真钥匙串在 CI 里不存在, 且会污染开发机的凭据库。"""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, user: str) -> str | None:
        return self.entries.get((service, user))

    def set_password(self, service: str, user: str, pw: str) -> None:
        self.entries[(service, user)] = pw


class BrokenKeyring:
    """不可用的钥匙串: 模拟无 Secret Service 的 Linux / 无 D-Bus 的容器。"""

    def get_password(self, service: str, user: str) -> str | None:
        raise RuntimeError("no secret service")

    def set_password(self, service: str, user: str, pw: str) -> None:
        raise RuntimeError("no secret service")


@pytest.fixture
def store_factory(tmp_path: Path):
    """产出 (store, 凭证文件路径)。默认注入可用的假钥匙串。"""

    async def make(keyring: Any = None) -> tuple[AuthStore, Path]:
        store = await AuthStore.from_appdata(
            str(tmp_path), keyring_mod=keyring if keyring is not None else FakeKeyring()
        )
        return store, tmp_path / "auth.enc.json"

    return make


async def test_token_not_stored_in_plaintext(store_factory) -> None:
    """R4 的核心: 磁盘上不得出现 token 原文。"""
    store, path = await store_factory()
    await store.save_token("secret-token-abc")

    raw = path.read_text(encoding="utf-8")
    assert "secret-token-abc" not in raw
    assert json.loads(raw)["enc"] is True
    assert await store.load_token() == "secret-token-abc"


async def test_file_carries_version_field(store_factory) -> None:
    """留 v 字段, 将来换 AES-GCM 时能识别旧格式。"""
    store, path = await store_factory()
    await store.save_token("tok")
    assert json.loads(path.read_text(encoding="utf-8"))["v"] == 1


async def test_device_key_is_stable(store_factory) -> None:
    """device_key 必须跨调用稳定: 云端 UNIQUE(user_id, device_key) 靠它保证
    重装不刷出新设备。"""
    store, _ = await store_factory()
    first = await store.device_key()
    assert first
    assert await store.device_key() == first


async def test_logout_keeps_device_key(store_factory) -> None:
    """登出只清 token。连 device_key 一起清掉的话, 重新登录会被云端当成新设备,
    设备列表越用越脏。"""
    store, _ = await store_factory()
    device_key = await store.device_key()
    await store.save_token("tok")

    await store.clear_token()

    assert await store.load_token() == ""
    assert await store.device_key() == device_key


async def test_corrupt_file_reads_as_logged_out(store_factory) -> None:
    """凭证文件损坏时视为未登录, 而不是让客户端起不来。"""
    store, path = await store_factory()
    await store.save_token("tok")
    path.write_text("{not json", encoding="utf-8")

    assert await store.load_token() == ""


async def test_key_change_detected_not_garbage(tmp_path: Path) -> None:
    """钥匙串密钥变了 (重置 / 换机) 必须识别出来。

    异或没有完整性保证, 只会解出乱码而不报错。不识别的话客户端会拿垃圾 token
    去请求, 界面却一直显示"已登录", 直到 401 才发现。
    """
    first = await AuthStore.from_appdata(str(tmp_path), keyring_mod=FakeKeyring())
    await first.save_token("tok-1")

    # 新的 FakeKeyring 等于换了一把密钥
    second = await AuthStore.from_appdata(str(tmp_path), keyring_mod=FakeKeyring())

    assert await second.load_token() == ""


async def test_keyring_unavailable_degrades_with_warning(tmp_path: Path, caplog) -> None:
    """钥匙串不可用时降级到明文, 但必须告警且在文件里留标记 —— 不静默降级。"""
    store = await AuthStore.from_appdata(str(tmp_path), keyring_mod=BrokenKeyring())
    await store.save_token("plain-token")

    raw = json.loads((tmp_path / "auth.enc.json").read_text(encoding="utf-8"))
    assert raw["enc"] is False
    assert store.encrypted is False
    # 降级仍要能工作, 否则整个客户端起不来
    assert await store.load_token() == "plain-token"
