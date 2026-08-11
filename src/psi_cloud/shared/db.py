"""SQLite 封装。

单机 SQLite 的三个必设项:
- WAL:读不阻塞写,单机并发够用
- busy_timeout:并发写时等待而非立刻 SQLITE_BUSY
- foreign_keys:SQLite 默认关闭外键约束,不显式开则形同虚设

不用 aiosqlite:其内部是 asyncio future。用 stdlib sqlite3 +
anyio.to_thread 把阻塞调用挪出事件循环。
"""

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from anyio import to_thread

SqlParams = Sequence[Any] | dict[str, Any]


class Database:
    """一个 SQLite 文件的访问入口。每次调用开关连接,不做连接池。"""

    def __init__(self, path: str) -> None:
        self._path = path

    @property
    def path(self) -> str:
        return self._path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
        finally:
            conn.close()

    # ---- 同步 API(供启动期建表、脚本使用)----

    def execute(self, sql: str, params: SqlParams = ()) -> None:
        with self.connect() as conn:
            conn.execute(sql, params)

    def executescript(self, script: str) -> None:
        with self.connect() as conn:
            conn.executescript(script)

    def query_all(self, sql: str, params: SqlParams = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(sql, params))

    def query_one(self, sql: str, params: SqlParams = ()) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchone()

    # ---- 异步包装(供路由使用,阻塞调用挪出事件循环)----

    async def aexecute(self, sql: str, params: SqlParams = ()) -> None:
        await to_thread.run_sync(self.execute, sql, params)

    async def aquery_all(
        self, sql: str, params: SqlParams = ()
    ) -> list[sqlite3.Row]:
        return await to_thread.run_sync(self.query_all, sql, params)

    async def aquery_one(
        self, sql: str, params: SqlParams = ()
    ) -> sqlite3.Row | None:
        return await to_thread.run_sync(self.query_one, sql, params)

    # ---- 热备:必须用 VACUUM INTO / backup,不能 cp(WAL 下 cp 可能拿到撕裂状态)----

    def backup_to(self, dest: str) -> None:
        with self.connect() as conn:
            conn.execute("VACUUM INTO ?", (dest,))

    def healthy(self) -> bool:
        try:
            row = self.query_one("SELECT 1 AS ok")
            return row is not None and row["ok"] == 1
        except sqlite3.Error:
            return False


def apply_schema(db: Database, statements: Iterable[str]) -> None:
    """幂等建表。每条语句都应是 CREATE ... IF NOT EXISTS。"""
    with db.connect() as conn:
        conn.execute("BEGIN")
        try:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
