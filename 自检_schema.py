# -*- coding: utf-8 -*-
"""schema.sql 的约束验证（第 1 步验收）。

第 1 步的验收标准不是"建表不报错"——那什么都证明不了。这里验三件事：

1. **约束本身有效**：并发插同一个 (provider, identifier) 必须有且只有一个成功。
   应用层"先查后插"在并发下会建出两个账号，所以这条只能靠数据库主键保证。
2. **PRAGMA 真的生效**：SQLite 的 foreign_keys 默认关闭，不显式打开等于没写；
   journal_mode 是持久化设置，busy_timeout 是每连接的。三者分别验。
3. **过期数据能清干净**：这几张表都没有 TTL 兜底，漏清不报错、只会慢慢积垢。

    python 自检_schema.py
    python 自检_schema.py --negative    反向验证：去掉约束后必须转红
"""

import os
import sqlite3
import sys
import tempfile
import threading
import time

SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

PASS, FAIL = [], []
RESULTS = []
_SECTION = ""


def section(name):
    global _SECTION
    _SECTION = name
    print(f"\n{name}")


def check(name, cond, detail=""):
    if callable(cond):
        try:
            cond = cond()
        except Exception as e:
            cond, detail = False, f"{type(e).__name__}: {e}"
    ok = bool(cond)
    RESULTS.append({"section": _SECTION, "name": name, "ok": ok,
                    "detail": "" if ok else str(detail)})
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}"
          + (f"  {detail}" if detail and not ok else ""))


def connect(path):
    """每个连接都要重设 PRAGMA：foreign_keys 与 busy_timeout 是连接级的，
    只在建库时设一次是无效的——这是 SQLite 的常见坑。"""
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def make_db(schema_sql=None, path=None):
    """按 schema 建库。schema_sql 传入时用它替代文件内容（反向验证用）。"""
    if path is None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
    sql = schema_sql if schema_sql is not None else open(
        SCHEMA, encoding="utf-8").read()
    conn = connect(path)
    conn.executescript(sql)
    conn.commit()
    conn.close()
    return path


def now(offset=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + offset))


def test_pragmas(path):
    section("[1] PRAGMA 真的生效")
    conn = connect(path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    check("journal_mode = WAL（默认 rollback journal 下写会阻塞读）",
          str(mode).lower() == "wal", str(mode))

    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    check("foreign_keys = ON（SQLite 默认关闭）", fk == 1, str(fk))

    bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    check("busy_timeout 已设（并发写要靠它等锁）", bt >= 5000, str(bt))

    # 外键真的拦得住，而不只是开关为 1
    try:
        conn.execute(
            "INSERT INTO identities(user_id, provider, identifier, verified_at)"
            " VALUES ('no-such-user', 'email', 'x@y.com', ?)", (now(),))
        conn.commit()
        check("外键实际拦住孤儿行", False, "插入本该失败却成功了")
    except sqlite3.IntegrityError:
        check("外键实际拦住孤儿行", True)
    conn.close()

    # 新开一个连接：journal_mode 是持久化的，foreign_keys 不是
    conn2 = sqlite3.connect(path)
    fk2 = conn2.execute("PRAGMA foreign_keys").fetchone()[0]
    check("新连接默认 foreign_keys=0（故连接层必须重设，已在 connect() 里做）",
          fk2 == 0, f"得到 {fk2}，与 SQLite 语义不符则说明环境异常")
    mode2 = conn2.execute("PRAGMA journal_mode").fetchone()[0]
    check("新连接仍是 WAL（journal_mode 持久化）",
          str(mode2).lower() == "wal", str(mode2))
    conn2.close()


def test_tables(path):
    section("[2] 表与约束齐备")
    conn = connect(path)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("users", "identities", "devices", "sessions", "email_codes",
              "send_quota", "invitation_codes"):
        check(f"表 {t} 存在", t in names, str(sorted(names)))

    # identities 主键必须是 (provider, identifier) 复合，顺序也要对
    pk = [r[1] for r in conn.execute("PRAGMA table_info(identities)")
          if r[5]]
    check("identities 主键为 (provider, identifier) 复合",
          pk == ["provider", "identifier"], str(pk))

    # sessions.token_hash 必须唯一
    idx = conn.execute("PRAGMA index_list(sessions)").fetchall()
    uniq_cols = []
    for row in idx:
        if row[2]:      # unique
            uniq_cols.extend(r[2] for r in
                             conn.execute(f"PRAGMA index_info('{row[1]}')"))
    check("sessions.token_hash 有唯一约束", "token_hash" in uniq_cols,
          str(uniq_cols))

    # devices UNIQUE(user_id, device_key)
    didx = conn.execute("PRAGMA index_list(devices)").fetchall()
    dcols = []
    for row in didx:
        if row[2]:
            dcols.append([r[2] for r in
                          conn.execute(f"PRAGMA index_info('{row[1]}')")])
    check("devices 有 UNIQUE(user_id, device_key)",
          any(set(c) == {"user_id", "device_key"} for c in dcols), str(dcols))

    # status / provider 的 CHECK 生效
    conn.execute("INSERT INTO users(id, created_at) VALUES ('u1', ?)", (now(),))
    conn.commit()
    try:
        conn.execute("INSERT INTO users(id, created_at, status)"
                     " VALUES ('bad', ?, 'nonsense')", (now(),))
        conn.commit()
        check("users.status 的 CHECK 拦住非法值", False, "非法状态被接受")
    except sqlite3.IntegrityError:
        check("users.status 的 CHECK 拦住非法值", True)
    try:
        conn.execute("INSERT INTO identities(user_id, provider, identifier)"
                     " VALUES ('u1', 'telepathy', 'x')")
        conn.commit()
        check("identities.provider 的 CHECK 拦住非法值", False, "非法 provider 被接受")
    except sqlite3.IntegrityError:
        check("identities.provider 的 CHECK 拦住非法值", True)
    conn.close()


def test_concurrent_identity(path):
    section("[3] 并发注册同一身份：只能建一个账号")
    # 先把 12 个 user 建好并提交。若放在各线程里、与 identities 插入同处一个
    # 未提交事务，写锁会让线程互相阻塞、卡死 barrier——那是测试写法问题，
    # 会掩盖真正要验的东西（复合主键在并发下的行为）。
    conn = connect(path)
    conn.execute("INSERT OR IGNORE INTO users(id, created_at) VALUES ('u1', ?)",
                 (now(),))
    for i in range(12):
        conn.execute("INSERT OR IGNORE INTO users(id, created_at)"
                     " VALUES (?, ?)", (f"race-u{i}", now()))
    conn.commit()
    conn.close()

    ident = ("email", "race@example.com")
    ok_count = [0]
    err_count = [0]
    other = []
    lock = threading.Lock()
    barrier = threading.Barrier(12)

    def worker(i):
        c = connect(path)
        try:
            barrier.wait(timeout=10)    # 让写入尽量撞在一起
            c.execute("INSERT INTO identities(user_id, provider, identifier,"
                      " verified_at) VALUES (?, ?, ?, ?)",
                      (f"race-u{i}", ident[0], ident[1], now()))
            c.commit()
            with lock:
                ok_count[0] += 1
        except sqlite3.IntegrityError:
            c.rollback()
            with lock:
                err_count[0] += 1
        except Exception as e:       # 其它异常要暴露，不能静静吞掉
            c.rollback()
            with lock:
                other.append(f"{type(e).__name__}: {e}")
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]

    conn = connect(path)
    rows = conn.execute(
        "SELECT COUNT(*) FROM identities WHERE provider=? AND identifier=?",
        ident).fetchone()[0]
    conn.close()

    check("并发写入无非预期异常", not other, "; ".join(other[:3]))
    check("12 路并发只有 1 个成功", ok_count[0] == 1,
          f"成功 {ok_count[0]}，冲突 {err_count[0]}，其它 {len(other)}")
    check("库里该身份只有 1 行（防重复注册的唯一保障）", rows == 1, str(rows))
    check("其余请求收到 IntegrityError（应用层可据此转登录）",
          err_count[0] == 11, str(err_count[0]))


def test_optimistic_invitation(path):
    section("[4] 邀请码乐观锁：并发只能消费一次")
    conn = connect(path)
    conn.execute("INSERT INTO invitation_codes(code, created_at)"
                 " VALUES ('INV-1', ?)", (now(),))
    conn.execute("INSERT OR IGNORE INTO users(id, created_at)"
                 " VALUES ('inv-u', ?)", (now(),))
    conn.commit()
    conn.close()

    wins = [0]
    lock = threading.Lock()
    barrier = threading.Barrier(10)

    def consume(i):
        c = connect(path)
        try:
            barrier.wait(timeout=5)
            # 乐观锁：靠 WHERE status='unused' + 受影响行数判断是否抢到
            cur = c.execute(
                "UPDATE invitation_codes SET status='consumed',"
                " consumed_by_user_id=?, consumed_at=?"
                " WHERE code='INV-1' AND status='unused'",
                ("inv-u", now()))
            c.commit()
            if cur.rowcount == 1:
                with lock:
                    wins[0] += 1
        except Exception:
            c.rollback()
        finally:
            c.close()

    ts = [threading.Thread(target=consume, args=(i,)) for i in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("10 路并发消费同一邀请码，只有 1 个成功", wins[0] == 1, str(wins[0]))


def test_cascade(path):
    section("[5] 级联删除")
    conn = connect(path)
    conn.execute("INSERT INTO users(id, created_at) VALUES ('cas-u', ?)",
                 (now(),))
    conn.execute("INSERT INTO identities(user_id, provider, identifier,"
                 " verified_at) VALUES ('cas-u','email','cas@x.com',?)",
                 (now(),))
    conn.execute("INSERT INTO devices(id, user_id, device_key, platform,"
                 " created_at) VALUES ('cas-d','cas-u','k1','win32',?)",
                 (now(),))
    conn.execute("INSERT INTO sessions(id, user_id, device_id, token_hash,"
                 " created_at, expires_at) VALUES"
                 " ('cas-s','cas-u','cas-d','hash-cas',?,?)",
                 (now(), now(3600)))
    conn.commit()

    conn.execute("DELETE FROM users WHERE id='cas-u'")
    conn.commit()
    left = {
        "identities": conn.execute(
            "SELECT COUNT(*) FROM identities WHERE user_id='cas-u'").fetchone()[0],
        "devices": conn.execute(
            "SELECT COUNT(*) FROM devices WHERE user_id='cas-u'").fetchone()[0],
        "sessions": conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id='cas-u'").fetchone()[0],
    }
    check("删 user 级联清掉 identities/devices/sessions",
          all(v == 0 for v in left.values()), str(left))
    conn.close()


def test_cleanup(path):
    section("[6] 过期数据清理")
    # 这几张表都没有 TTL 兜底：漏清不会报错，只会慢慢积垢，所以必须由验收盯。
    conn = connect(path)
    conn.execute("INSERT OR IGNORE INTO users(id, created_at)"
                 " VALUES ('cl-u', ?)", (now(),))
    conn.execute("INSERT OR IGNORE INTO devices(id, user_id, device_key,"
                 " platform, created_at) VALUES ('cl-d','cl-u','k','win32',?)",
                 (now(),))
    # 造过期数据
    conn.execute("INSERT INTO email_codes(identifier, code_hash, expires_at,"
                 " sent_at) VALUES ('old@x.com','h',?,?)",
                 (now(-100), now(-400)))
    conn.execute("INSERT INTO email_codes(identifier, code_hash, expires_at,"
                 " sent_at) VALUES ('fresh@x.com','h',?,?)",
                 (now(600), now()))
    conn.execute("INSERT INTO sessions(id, user_id, device_id, token_hash,"
                 " created_at, expires_at) VALUES"
                 " ('cl-s1','cl-u','cl-d','h-old',?,?)", (now(-99999), now(-10)))
    conn.execute("INSERT INTO sessions(id, user_id, device_id, token_hash,"
                 " created_at, expires_at) VALUES"
                 " ('cl-s2','cl-u','cl-d','h-new',?,?)", (now(), now(99999)))
    conn.execute("INSERT INTO send_quota(scope, key, window_start, count)"
                 " VALUES ('ip','1.2.3.4',?,3)", (now(-7200),))
    conn.execute("INSERT INTO send_quota(scope, key, window_start, count)"
                 " VALUES ('ip','5.6.7.8',?,1)", (now(),))
    conn.commit()

    cutoff = now()
    conn.execute("DELETE FROM email_codes WHERE expires_at < ?", (cutoff,))
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (cutoff,))
    conn.execute("DELETE FROM send_quota WHERE window_start < ?", (now(-3600),))
    conn.commit()

    check("过期 email_codes 被清、未过期保留",
          conn.execute("SELECT COUNT(*) FROM email_codes").fetchone()[0] == 1
          and conn.execute("SELECT identifier FROM email_codes").fetchone()[0]
          == "fresh@x.com")
    check("过期 sessions 被清、未过期保留",
          conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id='cl-u'"
                       ).fetchone()[0] == 1)
    check("过期限频窗口被清、当前窗口保留",
          conn.execute("SELECT COUNT(*) FROM send_quota").fetchone()[0] == 1)

    # 清理用的索引必须存在，否则全表扫
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    check("expires_at / window_start 有索引（避免清理时全表扫）",
          "idx_sessions_expires" in idx and "idx_email_codes_expires" in idx
          and "idx_send_quota_window" in idx, str(sorted(idx)))
    conn.close()


def run_all(schema_sql=None):
    """建一个临时库跑完整套。每段单独兜异常。"""
    PASS.clear(); FAIL.clear(); RESULTS.clear()
    path = None
    try:
        path = make_db(schema_sql)
    except Exception as e:
        check("建库", False, f"{type(e).__name__}: {e}")
        return {"results": list(RESULTS), "passed": len(PASS),
                "failed": len(FAIL), "failures": list(FAIL),
                "total": len(RESULTS)}
    try:
        for fn in (test_pragmas, test_tables, test_concurrent_identity,
                   test_optimistic_invitation, test_cascade, test_cleanup):
            try:
                fn(path)
            except Exception as e:
                check(f"{fn.__name__} 整段异常", False,
                      f"{type(e).__name__}: {e}")
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass
    return {"results": list(RESULTS), "passed": len(PASS), "failed": len(FAIL),
            "failures": list(FAIL), "total": len(RESULTS)}


# 反向验证：逐个拆掉一条约束，每个都必须让自检转红。
# 破坏点必须产生**合法但无约束**的 SQL——若只是把 SQL 改坏（如留下尾随逗号），
# 自检会因"建库失败"而红，那验证的是语法而非约束行为，等于没验。
SABOTAGES = [
    ("identities 主键改成非唯一",
     "防重复注册的唯一保障；去掉后并发会建出多个账号",
     lambda s: s.replace("    identifier  TEXT NOT NULL,\n"
                         "    verified_at TEXT,\n"
                         "    PRIMARY KEY (provider, identifier)\n",
                         "    identifier  TEXT NOT NULL,\n"
                         "    verified_at TEXT\n")),
    ("去掉 devices 的 UNIQUE",
     "重装会刷出新设备，设备列表失真",
     lambda s: s.replace("    last_seen_at TEXT,\n"
                         "    UNIQUE (user_id, device_key)\n",
                         "    last_seen_at TEXT\n")),
    ("sessions.token_hash 去掉 UNIQUE",
     "token 哈希必须唯一，否则一个 token 可能撞到多条会话",
     lambda s: s.replace("token_hash   TEXT NOT NULL UNIQUE,",
                         "token_hash   TEXT NOT NULL,")),
    ("journal_mode 退回 delete",
     "rollback journal 下写会阻塞读",
     lambda s: s.replace("PRAGMA journal_mode = WAL;",
                         "PRAGMA journal_mode = DELETE;")),
    ("去掉 status 的 CHECK",
     "非法状态可入库",
     lambda s: s.replace("CHECK (status IN ('active', 'disabled'))", "")),
    ("去掉 provider 的 CHECK",
     "非法 provider 可入库，将来加 OAuth 时更易写错",
     lambda s: s.replace(
         "                CHECK (provider IN ('phone', 'email', 'wechat',"
         " 'google',\n                                    'apple'))", "")),
    ("去掉级联删除",
     "删账号会留下孤儿 identities/devices/sessions",
     lambda s: s.replace(" ON DELETE CASCADE", "")),
    ("去掉清理用的索引",
     "清理过期数据时全表扫，库越大越慢",
     lambda s: s.replace(
         "CREATE INDEX IF NOT EXISTS idx_sessions_expires"
         " ON sessions(expires_at);", "")),
]


def run_negative():
    import contextlib
    import io
    base = open(SCHEMA, encoding="utf-8").read()
    print("反向验证：逐个拆掉一条约束，确认自检能抓出来\n")
    all_caught = True
    for name, why, mangle in SABOTAGES:
        mangled = mangle(base)
        if mangled == base:
            # 破坏点没生效（schema 文本改过、replace 没匹配上），
            # 这时无论红绿都不能算验证过，必须报出来。
            all_caught = False
            print(f"  [无效] {name}")
            print("         破坏点未匹配到 schema 文本，需更新反向验证")
            continue
        with contextlib.redirect_stdout(io.StringIO()):
            s = run_all(mangled)
        caught = s["failed"] > 0
        all_caught = all_caught and caught
        print(f"  [{'抓到' if caught else '漏掉'}] {name}")
        print(f"         理由：{why}")
        print(f"         失败 {s['failed']} 项"
              + (f"，例如：{'; '.join(s['failures'][:2])}" if caught else ""))
    with contextlib.redirect_stdout(io.StringIO()):
        healthy = run_all()
    print(f"\n  恢复后：失败 {healthy['failed']} 项（应为 0）")
    effective = all_caught and healthy["failed"] == 0
    print("\n  结论：" + ("每个破坏点都被抓到，且恢复后全绿——自检有约束力"
                          if effective else "有破坏点未被抓到，需修正自检"))
    return 0 if effective else 1


def main():
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(
            os.path.abspath(__file__)), ".."))
        from pnvs_console import setup_console
        setup_console()
    except ImportError:
        pass

    if "--negative" in sys.argv:
        return run_negative()

    s = run_all()
    print(f"\n通过 {s['passed']} / {s['total']}，失败 {s['failed']}")
    if s["failed"]:
        print("失败项：" + "; ".join(s["failures"][:10]))
    else:
        print("schema 的约束、PRAGMA、级联、清理均已验证。")
        print("注意：foreign_keys 与 busy_timeout 是连接级设置，"
              "应用层每开一个连接都要重设（见 connect()）。")
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
