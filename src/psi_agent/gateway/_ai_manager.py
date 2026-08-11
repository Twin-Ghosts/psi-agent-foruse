from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
from loguru import logger

from psi_agent.ai import Ai
from psi_agent.gateway._manager import (
    _ensure_socket_dir,
    _new_uuid,
    _noop,
    _remove_socket,
    _socket_path,
    _wait_socket,
)


@dataclass
class AiInfo:
    id: str
    socket: str
    provider: str
    model: str
    api_key: str
    base_url: str
    max_context_tokens: int = -1
    """Prompt token threshold that triggers compaction.

    ``-1`` keeps ``Ai``'s own resolution (``PSI_MAX_CONTEXT_TOKENS`` env var,
    else 100K); ``0`` disables compaction.  Defaulted so state snapshots
    written before this field existed still restore.
    """


@dataclass
class _AiEntry:
    scope: anyio.CancelScope
    info: AiInfo


@dataclass
class AIManager:
    _prefix: str
    _tg: Any  # anyio.TaskGroup (ty不识别的第三方类型)
    _entries: dict[str, _AiEntry] = field(default_factory=dict)
    _lock: anyio.Lock = field(default_factory=anyio.Lock)
    _persist: Callable[[], Awaitable[None]] = _noop

    async def create(
        self,
        provider: str,
        model: str,
        api_key: str,
        base_url: str,
        *,
        id: str = "",
        max_context_tokens: int = -1,
    ) -> AiInfo:
        want_key = self._config_key(provider, model, api_key, base_url)
        explicit_id = id.strip()
        ai_id = explicit_id or _new_uuid()
        async with self._lock:
            logger.debug(f"AIManager: acquired lock for create {ai_id!r}")
            if ai_id in self._entries:
                raise ValueError(f"AI {ai_id!r} already exists")
            # No explicit id: reuse an already-running identical config (dedupe).
            # Explicit id (Session revive) may still create a second instance with
            # the same provider/model/key so the Session keeps its backend_id.
            if not explicit_id:
                for entry in self._entries.values():
                    info = entry.info
                    if self._config_key(info.provider, info.model, info.api_key, info.base_url) == want_key:
                        logger.info(
                            f"AI create: reusing identical config as {info.id!r} "
                            f"(provider={provider!r} model={model!r})"
                        )
                        return info
            socket = _socket_path(self._prefix, "ais", ai_id)
            await _ensure_socket_dir(socket)
            ai = Ai(
                session_socket=socket,
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
                max_context_tokens=max_context_tokens,
            )
            scope = anyio.CancelScope()

            async def _run_ai() -> None:
                try:
                    with scope:
                        await ai.run()
                except Exception as e:
                    logger.error(f"AI {ai_id!r} crashed: {e!r}")
                    async with self._lock:
                        self._entries.pop(ai_id, None)
                    await self._persist()

            logger.debug(f"AIManager: starting AI {ai_id!r} task")
            self._tg.start_soon(_run_ai)
            info = AiInfo(
                id=ai_id,
                socket=socket,
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
                max_context_tokens=max_context_tokens,
            )
            self._entries[ai_id] = _AiEntry(scope=scope, info=info)
        try:
            await _wait_socket(info.socket)
        except Exception:
            logger.warning(f"AI {ai_id!r} did not become ready, rolling back")
            with anyio.CancelScope(shield=True):
                async with self._lock:
                    self._entries.pop(ai_id, None)
                    scope.cancel()
                    await _remove_socket(info.socket)
                await self._persist()
            raise
        await self._persist()
        logger.info(f"AI {ai_id!r} created on {info.socket}")
        return info

    @staticmethod
    def _config_key(provider: str, model: str, api_key: str, base_url: str) -> tuple[str, str, str, str]:
        return (provider, model, api_key, base_url.rstrip("/"))

    async def delete(self, ai_id: str) -> None:
        async with self._lock:
            logger.debug(f"AIManager: acquired lock for delete {ai_id!r}")
            if ai_id not in self._entries:
                raise LookupError(f"AI {ai_id!r} not found")
            entry = self._entries.pop(ai_id)
            entry.scope.cancel()
            await _remove_socket(entry.info.socket)
        await self._persist()
        logger.info(f"AI {ai_id!r} deleted")

    async def list_all(self) -> list[AiInfo]:
        return [e.info for e in list(self._entries.values())]

    def get_socket(self, ai_id: str) -> str:
        if ai_id in self._entries:
            return self._entries[ai_id].info.socket
        return _socket_path(self._prefix, "ais", ai_id)

    def has(self, ai_id: str) -> bool:
        return ai_id in self._entries
