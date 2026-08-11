# -*- coding: utf-8 -*-
"""SQLite 存储层（anyio 版）。

方案文档定的三条约束在这里落地：

1. **不用 aiosqlite** —— 它内部是 asyncio future，与仓库"禁 asyncio 原生 API"
   的约束冲突。用 stdlib sqlite3 + ``anyio.to_thread.run_sync`` 包一层。
2. **每个连接都要重设 foreign_keys / busy_timeout** —— 它们是连接级 PRAGMA，
   写在 schema.sql 里只对建库那一个连接有效。漏掉等于没有外键。
3. **单写连接串行化** —— SQLite 同一时刻只允许一个写事务；且共享 Connection
   对象本身不是并发安全的（``check_same_thread=False`` 只关掉归属检查，不提供
   并发保护），所以读也要持锁。

为什么全部走 to_thread：sqlite3 的调用是阻塞的，直接在事件循环里跑会卡住整个
服务。放到线程里跑 + anyio.Lock 串行化，既不阻塞循环也不会并发撞库。
"""

import os
import sqlite3
import threading

import anyio

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "schema.sql")


class Store:
    """异步 SQLite 存取。构造后必须 await ``open()``。"""

    def __init__(self, path=":memory:"):
        self.path = path
        # anyio.Lock 串行化所有库访问（读也要，见模块 docstring 第 3 条）
        self._lock = anyio.Lock()
        # 线程锁：to_thread 里的实际访问要防止两个工作线程同时碰同一个连接
        self._thread_lock = threading.RLock()
        self._conn = None

    # ---- 生命周期 ----
    async def open(self):
        """建连接并初始化 schema。返回 self，便于 ``store = await Store(p).open()``。"""
        await anyio.to_thread.run_sync(self._open_sync)
        return self

    def _open_sync(self):
        conn = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # 连接级 PRAGMA：每个新连接都必须重设，这不是可选优化
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        self._conn = conn
        sql = open(os.path.abspath(SCHEMA_PATH), encoding="utf-8").read()
        with self._thread_lock:
            conn.executescript(sql)
            conn.commit()
        # WAL 对 :memory: 无意义，只对文件库要求
        if self.path != ":memory:":
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                conn.execute("PRAGMA journal_mode = WAL")

    async def aclose(self):
        if self._conn is not None:
            await anyio.to_thread.run_sync(self._conn.close)
            self._conn = None

    # ---- 读 ----
    async def one(self, sql, args=()):
        async with self._lock:
            return await anyio.to_thread.run_sync(self._one_sync, sql, args)

    def _one_sync(self, sql, args):
        with self._thread_lock:
            return self._conn.execute(sql, args).fetchone()

    async def all(self, sql, args=()):
        async with self._lock:
            return await anyio.to_thread.run_sync(self._all_sync, sql, args)

    def _all_sync(self, sql, args):
        with self._thread_lock:
            return self._conn.execute(sql, args).fetchall()

    # ---- 写 ----
    async def write(self, sql, args=()):
        """返回受影响行数。乐观锁靠这个返回值判断是否抢到。"""
        async with self._lock:
            return await anyio.to_thread.run_sync(self._write_sync, sql, args)

    def _write_sync(self, sql, args):
        with self._thread_lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.rowcount

    async def dump(self):
        """整库倒成文本。给自检搜明文用（比逐列检查更难漏）。"""
        async with self._lock:
            return await anyio.to_thread.run_sync(self._dump_sync)

    def _dump_sync(self):
        with self._thread_lock:
            return "\n".join(self._conn.iterdump())

    async def script(self, statements):
        """在一个事务里跑多条语句。statements 为 [(sql, args), ...]。

        用它而非多次 write：建 user + identity 必须原子——否则并发下会留下
        没有 identity 的孤儿 user。
        """
        async with self._lock:
            return await anyio.to_thread.run_sync(self._script_sync, statements)

    def _script_sync(self, statements):
        with self._thread_lock:
            try:
                for sql, args in statements:
                    self._conn.execute(sql, args)
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    async def in_tx(self, fn):
        """把一段读写逻辑放进单个事务执行。

        fn 是**同步**函数，签名 ``fn(conn) -> 结果``，在工作线程里跑。刻意不接
        async 回调：那会让事务跨 await 点，其它任务可能插进来写库。
        """
        async with self._lock:
            return await anyio.to_thread.run_sync(self._in_tx_sync, fn)

    def _in_tx_sync(self, fn):
        with self._thread_lock:
            try:
                result = fn(self._conn)
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise
