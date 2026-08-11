# -*- coding: utf-8 -*-
"""运维命令行：邀请码管理与库状态查看。

在此之前发一批邀请码只能手写 SQL 插入——容易写错、也没人记得字段默认值。

    python admin.py codes new --count 20            生成 20 个码
    python admin.py codes new --bound u@example.com 生成一个绑定邮箱的码
    python admin.py codes new --days 7 --note 内测  7 天后过期，带备注
    python admin.py codes list                      列出（默认只看未使用）
    python admin.py codes list --all                 含已消费/已撤销
    python admin.py codes revoke ABCD-1234           撤销一个码
    python admin.py stats                            库状态概览
    python admin.py sweep                            手动清一次过期数据

所有命令都走与服务同一份 service/store，不绕过约束（例如撤销走乐观锁）。
"""

import os
import secrets
import sys

import anyio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import service                              # noqa: E402
from app.store import Store                          # noqa: E402

# 去掉容易混淆的字符：0/O、1/I/l。邀请码要靠人念、抄、手打。
_ALPHABET = "ACDEFGHJKMNPQRSTUVWXY3456789"


def gen_code(groups=2, size=4):
    """形如 ABCD-1234。分组是为了口述与抄写时不易错位。"""
    return "-".join(
        "".join(secrets.choice(_ALPHABET) for _ in range(size))
        for _ in range(groups))


def db_path():
    return os.environ.get("AUTH_DB", "data/auth.db")


async def _open():
    path = db_path()
    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return await Store(path).open()


async def cmd_codes_new(args):
    """生成邀请码。重复的码直接重试，不静默跳过（否则 --count 会少发）。"""
    store = await _open()
    try:
        expires = service.now_iso(args.days * 86400) if args.days else None
        made = []
        for _ in range(args.count):
            for attempt in range(20):
                code = gen_code()
                try:
                    await store.write(
                        "INSERT INTO invitation_codes(code, status, expires_at,"
                        " bound_identifier, note, created_at)"
                        " VALUES (?,'unused',?,?,?,?)",
                        (code, expires, args.bound or None, args.note or None,
                         service.now_iso()))
                    made.append(code)
                    break
                except Exception:
                    if attempt == 19:
                        raise
        for c in made:
            print(c)
        extra = []
        if expires:
            extra.append(f"{args.days} 天后过期")
        if args.bound:
            extra.append(f"仅限 {args.bound}")
        if args.note:
            extra.append(f"备注「{args.note}」")
        print(f"\n已生成 {len(made)} 个"
              + ("（" + "，".join(extra) + "）" if extra else ""), file=sys.stderr)
        if not os.environ.get("INVITATION_REQUIRED", "").strip().lower() in (
                "1", "true", "yes", "on"):
            print("提示：门禁当前未开启（INVITATION_REQUIRED 非真），"
                  "这些码暂时不会被校验。", file=sys.stderr)
    finally:
        await store.aclose()


async def cmd_codes_list(args):
    store = await _open()
    try:
        sql = ("SELECT code, status, expires_at, bound_identifier,"
               " consumed_by_user_id, consumed_at, note, created_at"
               " FROM invitation_codes")
        params = ()
        if not args.all:
            sql += " WHERE status='unused'"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params = (args.limit,)
        rows = await store.all(sql, params)
        if not rows:
            print("（没有符合条件的邀请码）", file=sys.stderr)
            return
        now = service.now_iso()
        print(f"{'邀请码':<12} {'状态':<9} {'过期':<21} 绑定 / 备注")
        for r in rows:
            status = r["status"]
            # 过期但仍标 unused 的，显示成 expired 更不容易误判
            if status == "unused" and r["expires_at"] and r["expires_at"] < now:
                status = "expired"
            tail = " ".join(x for x in (r["bound_identifier"], r["note"]) if x)
            print(f"{r['code']:<12} {status:<9} {r['expires_at'] or '永不':<21} {tail}")
        print(f"\n共 {len(rows)} 条", file=sys.stderr)
    finally:
        await store.aclose()


async def cmd_codes_revoke(args):
    """撤销走乐观锁：只有仍为 unused 才能撤，已被消费的不动。"""
    store = await _open()
    try:
        n = await store.write(
            "UPDATE invitation_codes SET status='revoked'"
            " WHERE code=? AND status='unused'", (args.code,))
        if n == 1:
            print(f"已撤销 {args.code}")
            return 0
        row = await store.one("SELECT status FROM invitation_codes WHERE code=?",
                              (args.code,))
        if row is None:
            print(f"没有这个码：{args.code}", file=sys.stderr)
        else:
            print(f"未撤销：{args.code} 当前状态为 {row['status']}"
                  "（只有 unused 可撤销）", file=sys.stderr)
        return 1
    finally:
        await store.aclose()


async def cmd_stats(args):
    """库状态概览。顺带把"过期但仍在库里"的行数单列出来——那是清理是否到位的信号。"""
    store = await _open()
    try:
        now = service.now_iso()

        async def n(sql, p=()):
            return (await store.one(sql, p))[0]

        print(f"数据库：{db_path()}\n")
        print("账号")
        print(f"  users            {await n('SELECT COUNT(*) FROM users')}")
        print(f"  identities       {await n('SELECT COUNT(*) FROM identities')}")
        rows = await store.all("SELECT provider, COUNT(*) c FROM identities"
                               " GROUP BY provider")
        for r in rows:
            print(f"    {r['provider']:<14} {r['c']}")
        print("\n会话与设备")
        print(f"  devices          {await n('SELECT COUNT(*) FROM devices')}")
        print(f"  sessions         {await n('SELECT COUNT(*) FROM sessions')}")
        print(f"    活跃           {await n('SELECT COUNT(*) FROM sessions WHERE revoked_at IS NULL AND expires_at >= ?', (now,))}")
        print(f"    已撤销         {await n('SELECT COUNT(*) FROM sessions WHERE revoked_at IS NOT NULL')}")
        print("\n邀请码")
        for st in ("unused", "consumed", "revoked"):
            print(f"  {st:<16} {await n('SELECT COUNT(*) FROM invitation_codes WHERE status=?', (st,))}")
        print("\n待清理（过期仍在库里的行；数字持续增长说明定时清理没生效）")
        print(f"  email_codes      {await n('SELECT COUNT(*) FROM email_codes WHERE expires_at < ?', (now,))}")
        print(f"  sessions         {await n('SELECT COUNT(*) FROM sessions WHERE expires_at < ?', (now,))}")
        print(f"  send_quota       {await n('SELECT COUNT(*) FROM send_quota WHERE window_start < ?', (service.now_iso(-3600),))}")
    finally:
        await store.aclose()


async def cmd_sweep(args):
    """手动清一次。生产环境由服务内的定时任务自动跑，这条是排查时用的。"""
    from app import providers
    store = await _open()
    try:
        svc = service.AuthService(store, providers.MockProvider())
        result = await svc.sweep()
        print(f"已清理 {result.get('deleted', 0)} 行过期数据")
    finally:
        await store.aclose()


def main():
    import argparse

    try:
        from pnvs_console import setup_console
        setup_console()
    except ImportError:
        pass

    ap = argparse.ArgumentParser(
        description="psi-agent 认证服务运维命令",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="库路径由环境变量 AUTH_DB 决定，默认 data/auth.db")
    sub = ap.add_subparsers(dest="group", required=True)

    codes = sub.add_parser("codes", help="邀请码管理")
    csub = codes.add_subparsers(dest="action", required=True)

    new = csub.add_parser("new", help="生成邀请码")
    new.add_argument("--count", type=int, default=1, help="生成几个（默认 1）")
    new.add_argument("--days", type=int, default=0,
                     help="多少天后过期（默认永不过期）")
    new.add_argument("--bound", default="",
                     help="绑定到某个手机号/邮箱，他人不可用")
    new.add_argument("--note", default="", help="备注，便于日后对账")
    new.set_defaults(fn=cmd_codes_new)

    lst = csub.add_parser("list", help="列出邀请码")
    lst.add_argument("--all", action="store_true", help="含已消费/已撤销")
    lst.add_argument("--limit", type=int, default=50, help="最多显示几条")
    lst.set_defaults(fn=cmd_codes_list)

    rev = csub.add_parser("revoke", help="撤销一个未使用的邀请码")
    rev.add_argument("code")
    rev.set_defaults(fn=cmd_codes_revoke)

    st = sub.add_parser("stats", help="库状态概览")
    st.set_defaults(fn=cmd_stats)

    sw = sub.add_parser("sweep", help="手动清理过期数据")
    sw.set_defaults(fn=cmd_sweep)

    args = ap.parse_args()
    return anyio.run(args.fn, args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
