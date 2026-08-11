# -*- coding: utf-8 -*-
"""业务层：发码、校验、建号、会话、设备撤销。

顺序上的两条硬要求（方案文档强调，契约测试会验）：

    归一化必须在入库与限频之前   否则同一个人能注册多个账号、也能绕过限频
    限频必须在调用供应商之前     供应商侧也有限频，但撞到它时钱已经花了

token 模型：单个不透明高熵随机串，服务端只存 SHA256 哈希，60 天绝对上限，
撤销标 revoked_at 即时生效。不用 JWT —— 收益是无状态验证，但这里每次请求都要
写 last_used_at，那次查库省不掉；而即时撤销 JWT 做不到。
"""

import hashlib
import re
import secrets
import time
import uuid

from . import providers_core as providers

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

TOKEN_TTL_DAYS = 60
TEMP_TOKEN_TTL = 600            # 10 分钟

RATE_LIMITS = {
    "send_per_identifier": (60, 1),
    "send_per_ip": (60, 5),
    "verify_per_identifier": (300, 5),
}


def norm_phone(raw):
    """去空格/横线/+、去 86 前缀，再校验大陆号段。"""
    s = re.sub(r"[\s\-+]", "", str(raw or ""))
    if s.startswith("86"):
        s = s[2:]
    return s if PHONE_RE.match(s) else None


def norm_email(raw):
    """小写、去首尾空格；Gmail 去点号（a.b@gmail.com 与 ab@gmail.com 同一人）。"""
    s = str(raw or "").strip().lower()
    if s.count("@") != 1 or " " in s:
        return None
    local, _, domain = s.partition("@")
    if not local or "." not in domain or domain.startswith(".") \
            or domain.endswith("."):
        return None
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+")[0].replace(".", "")
    return f"{local}@{domain}" if local else None


def now_iso(offset=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + offset))


def token_hash(token):
    """只存哈希，不存原文。库被读走也无法反推 token。"""
    return hashlib.sha256(token.encode()).hexdigest()


class ServiceError(Exception):
    """携带契约错误码。HTTP 层据此取状态码。"""

    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class AuthService:
    def __init__(self, store, provider=None, invitation_required=False):
        self.store = store
        self.provider = provider or providers.MockProvider()
        # 邀请码门禁：默认关闭（第 9 步的开关）
        self.invitation_required = invitation_required
        # tempToken 存 temp_tokens 表，不放进程内字典——多 worker 下字典不共享

    # ------------------------------------------------------------ 限频
    async def _hit(self, scope, key, window, limit):
        """滑动窗口计数。放行返回 (True, 0)，拒绝返回 (False, 还需等待秒数)。

        用 send_quota 表按窗口分桶，顺手删本 key 的过期行——过期数据没有 TTL
        兜底，写入时清一次能让积垢慢很多。
        """
        now = time.time()
        cutoff = now_iso(-window)

        # 整段放进一个事务里跑（同步函数，在工作线程内执行）。刻意不拆成多次
        # await：查与写之间若能被其它任务插入，并发下就会多放几个额度出去。
        def _tx(conn):
            conn.execute("DELETE FROM send_quota WHERE scope=? AND key=?"
                         " AND window_start < ?", (scope, key, cutoff))
            rows = conn.execute(
                "SELECT window_start, count FROM send_quota"
                " WHERE scope=? AND key=?", (scope, key)).fetchall()
            total = sum(r["count"] for r in rows)
            if total >= limit:
                oldest = min(r["window_start"] for r in rows)
                elapsed = now - time.mktime(time.strptime(
                    oldest, "%Y-%m-%dT%H:%M:%SZ")) + time.timezone
                return False, max(1, int(window - elapsed) + 1)
            conn.execute(
                "INSERT INTO send_quota(scope, key, window_start, count)"
                " VALUES (?,?,?,1)"
                " ON CONFLICT(scope, key, window_start)"
                " DO UPDATE SET count = count + 1", (scope, key, now_iso()))
            return True, 0

        return await self.store.in_tx(_tx)

    async def _refund(self, scope, key):
        """退还一次配额：把最近那个窗口的计数减一，减到 0 就删行。

        用途见 send_code：供应商失败时不该让用户被我们的故障锁在门外。
        """
        def _tx(conn):
            row = conn.execute(
                "SELECT window_start, count FROM send_quota"
                " WHERE scope=? AND key=? ORDER BY window_start DESC LIMIT 1",
                (scope, key)).fetchone()
            if not row:
                return
            if row["count"] <= 1:
                conn.execute("DELETE FROM send_quota WHERE scope=? AND key=?"
                             " AND window_start=?",
                             (scope, key, row["window_start"]))
            else:
                conn.execute("UPDATE send_quota SET count = count - 1"
                             " WHERE scope=? AND key=? AND window_start=?",
                             (scope, key, row["window_start"]))

        await self.store.in_tx(_tx)

    async def reset_limits(self):
        """仅测试用。刻意与 sweep 分开：sweep 不该重置活跃窗口，
        否则清理任务等于给限频开了后门。"""
        await self.store.write("DELETE FROM send_quota")

    async def sweep(self):
        """清过期数据。定时任务与写入路径共用这一份逻辑。"""
        cutoff = now_iso()
        n = 0
        n += await self.store.write("DELETE FROM email_codes WHERE expires_at < ?",
                              (cutoff,))
        n += await self.store.write("DELETE FROM sessions WHERE expires_at < ?",
                              (cutoff,))
        n += await self.store.write("DELETE FROM send_quota WHERE window_start < ?",
                              (now_iso(-3600),))
        n += await self.store.write("DELETE FROM temp_tokens WHERE expires_at < ?",
                              (cutoff,))
        return {"deleted": n}

    # ------------------------------------------------------------ 发码
    async def send_code(self, kind, raw_identifier, ip, invitation_code=None):
        """kind 为 'phone' 或 'email'。返回 {'retryAfter': int}。"""
        # 归一化必须在限频之前：否则 +86 前缀等写法能各自占一个桶
        identifier = (norm_phone if kind == "phone" else norm_email)(
            raw_identifier)
        if not identifier:
            raise ServiceError("invalid_phone" if kind == "phone"
                               else "invalid_email")

        if self.invitation_required:
            await self._check_invitation(invitation_code, identifier, consume=False)

        window, limit = RATE_LIMITS["send_per_identifier"]
        ok, wait = await self._hit("identifier", identifier, window, limit)
        if not ok:
            raise ServiceError("rate_limited", wait)
        window, limit = RATE_LIMITS["send_per_ip"]
        ok, wait = await self._hit("ip", ip, window, limit)
        if not ok:
            raise ServiceError("rate_limited", wait)

        # 限频通过后才碰供应商
        # 供应商失败时退还 identifier 配额：故障是我们这边的，不该把用户锁在
        # 门外。IP 桶不退——否则攻击者可借"制造失败"无限重试来绕过 IP 限频。
        if kind == "email":
            code = providers.generate_code()
            _id, err = await self.provider.send_code("email", identifier, code)
            if err:
                await self._refund("identifier", identifier)
                raise ServiceError("provider_error")
            # 邮箱验证码由我们自管：只存哈希
            await self.store.write(
                "INSERT INTO email_codes(identifier, code_hash, expires_at,"
                " attempts, sent_at) VALUES (?,?,?,0,?)"
                " ON CONFLICT(identifier) DO UPDATE SET"
                " code_hash=excluded.code_hash,"
                " expires_at=excluded.expires_at, attempts=0,"
                " sent_at=excluded.sent_at",
                (identifier, providers.hash_code(identifier, code),
                 now_iso(providers.CODE_TTL), now_iso()))
        else:
            # 手机：PNVS 托管验证码，我们零存储
            _id, err = await self.provider.send_code("phone", identifier, None)
            if err:
                await self._refund("identifier", identifier)
                raise ServiceError("provider_error")

        return {"retryAfter": RATE_LIMITS["send_per_identifier"][0]}


    # ------------------------------------------------------------ 校验
    async def verify(self, kind, raw_identifier, code, device_key, platform):
        """校验通过则登录或返回 tempToken。

        登录与注册同一入口：identity 存在则登录，不存在则发 tempToken 走
        /complete 建号。
        """
        identifier = (norm_phone if kind == "phone" else norm_email)(
            raw_identifier)
        if not identifier:
            raise ServiceError("invalid_phone" if kind == "phone"
                               else "invalid_email")
        if not code or not device_key or not platform:
            raise ServiceError("invalid_code")

        # 校验侧限频：6 位码只有 100 万种，不限次就能穷举
        window, limit = RATE_LIMITS["verify_per_identifier"]
        ok, wait = await self._hit("identifier", f"verify:{identifier}", window,
                             limit)
        if not ok:
            raise ServiceError("rate_limited", wait)

        if kind == "phone":
            passed, err = await self.provider.check_hosted(identifier, code)
            if not passed:
                raise ServiceError(err or "invalid_code")
        else:
            await self._check_email_code(identifier, code)

        uid = await self._find_user(kind, identifier)
        if uid:
            token = await self._issue_session(uid, device_key, platform)
            return {"token": token, "user": await self._user_dto(uid)}

        # tempToken 落库而非进程内字典：线上 uvicorn --workers 2，/verify 与
        # /complete 可能落在不同进程，字典方案下后者查不到（实测 9 并发只 2 成功）。
        # 只存哈希，与 sessions.token_hash 同理。
        tt = secrets.token_urlsafe(24)
        await self.store.write(
            "INSERT INTO temp_tokens(token_hash, provider, identifier,"
            " expires_at, created_at) VALUES (?,?,?,?,?)",
            (token_hash(tt), kind, identifier,
             now_iso(TEMP_TOKEN_TTL), now_iso()))
        return {"tempToken": tt, "isNewUser": True}

    async def _check_email_code(self, identifier, code):
        """邮箱验证码自管：常数时间比对、命中即删、试错到上限即作废。

        **整段必须在一个事务里。** 改成 async 后，"读一次 + 删一次"之间会插进
        其它请求，8 路并发用同一个码时会有多个同时通过（实测 5 个）——这正是
        契约测试"命中即删，不可重放"那条抓出来的。
        胜者由 DELETE 的受影响行数决定：只有真正删掉那行的请求算通过。
        """
        import hmac as _hmac

        def _tx(conn):
            row = conn.execute(
                "SELECT code_hash, expires_at, attempts FROM email_codes"
                " WHERE identifier=?", (identifier,)).fetchone()
            if not row:
                return "code_expired"
            if row["expires_at"] < now_iso():
                conn.execute("DELETE FROM email_codes WHERE identifier=?",
                             (identifier,))
                return "code_expired"
            if row["attempts"] >= providers.MAX_ATTEMPTS:
                conn.execute("DELETE FROM email_codes WHERE identifier=?",
                             (identifier,))
                return "code_expired"
            if _hmac.compare_digest(
                    row["code_hash"],
                    providers.hash_code(identifier, str(code).strip())):
                cur = conn.execute(
                    "DELETE FROM email_codes WHERE identifier=?", (identifier,))
                # 并发下只有一个请求能删到行，其余视为码已被用掉
                return None if cur.rowcount == 1 else "code_expired"
            conn.execute("UPDATE email_codes SET attempts = attempts + 1"
                         " WHERE identifier=?", (identifier,))
            return "invalid_code"

        err = await self.store.in_tx(_tx)
        if err:
            raise ServiceError(err)

    async def _find_user(self, provider, identifier):
        row = await self.store.one(
            "SELECT user_id FROM identities WHERE provider=? AND identifier=?",
            (provider, identifier))
        return row["user_id"] if row else None

    # ------------------------------------------------------------ 建号
    async def complete(self, temp_token, device_key, platform, display_name=None,
                 invitation_code=None):
        """两段式注册的第二段。手机/邮箱/将来的 OAuth 都收敛到这里。"""
        if not device_key or not platform:
            raise ServiceError("invalid_code")
        # DELETE ... RETURNING 一步取用并删除：等价于原来字典的 pop，且在并发下
        # 只有一个请求能拿到行（SQLite 的写锁保证），不会两个请求同时建号。
        # 拆成「先 SELECT 再 DELETE」就有窗口，8 路并发能建出两个账号。
        rows = await self.store.all(
            "DELETE FROM temp_tokens WHERE token_hash=? AND expires_at > ?"
            " RETURNING provider, identifier",
            (token_hash(temp_token or ""), now_iso()))
        if not rows:
            raise ServiceError("temp_token_invalid")
        rec = {"provider": rows[0]["provider"],
               "identifier": rows[0]["identifier"]}

        if self.invitation_required:
            await self._check_invitation(invitation_code, rec["identifier"],
                                   consume=True)

        key = (rec["provider"], rec["identifier"])
        uid = await self._find_user(*key)
        if uid is None:
            uid = f"u_{uuid.uuid4().hex[:16]}"
            def _tx(conn):
                conn.execute("INSERT INTO users(id, created_at,"
                             " display_name) VALUES (?,?,?)",
                             (uid, now_iso(), display_name))
                # 复合主键防重复注册：并发下这里会抛 IntegrityError
                conn.execute(
                    "INSERT INTO identities(user_id, provider, identifier,"
                    " verified_at) VALUES (?,?,?,?)",
                    (uid, key[0], key[1], now_iso()))

            try:
                await self.store.in_tx(_tx)
            except Exception:
                # 并发下别人先建成了，转为登录
                uid = await self._find_user(*key)
                if uid is None:
                    raise ServiceError("provider_error")
        token = await self._issue_session(uid, device_key, platform)
        return {"token": token, "user": await self._user_dto(uid)}


    # ------------------------------------------------------------ 会话
    async def _issue_session(self, uid, device_key, platform):
        """签发 token。device_key 相同则复用设备行，保证重装不刷出新设备。"""
        token = secrets.token_urlsafe(32)

        def _tx(conn):
            row = conn.execute(
                "SELECT id FROM devices WHERE user_id=? AND device_key=?",
                (uid, device_key)).fetchone()
            if row:
                did = row["id"]
                conn.execute("UPDATE devices SET platform=?, last_seen_at=?"
                             " WHERE id=?", (platform, now_iso(), did))
            else:
                did = f"d_{uuid.uuid4().hex[:16]}"
                conn.execute(
                    "INSERT INTO devices(id, user_id, device_key, platform,"
                    " created_at) VALUES (?,?,?,?,?)",
                    (did, uid, device_key, platform, now_iso()))
            conn.execute(
                "INSERT INTO sessions(id, user_id, device_id, token_hash,"
                " created_at, last_used_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (f"s_{uuid.uuid4().hex[:16]}", uid, did, token_hash(token),
                 now_iso(), now_iso(), now_iso(TOKEN_TTL_DAYS * 86400)))

        await self.store.in_tx(_tx)
        return token

    async def authenticate(self, token):
        """校验 token，返回会话行。顺手刷新 last_used_at（滑动过期）。

        撤销即时生效靠 revoked_at IS NULL 这个条件——这是不用 JWT 的核心理由。
        """
        if not token:
            raise ServiceError("unauthorized")
        row = await self.store.one(
            "SELECT * FROM sessions WHERE token_hash=?"
            " AND revoked_at IS NULL", (token_hash(token),))
        if not row or row["expires_at"] < now_iso():
            raise ServiceError("unauthorized")
        await self.store.write("UPDATE sessions SET last_used_at=? WHERE id=?",
                         (now_iso(), row["id"]))
        return row

    async def me(self, token):
        sess = await self.authenticate(token)
        idents = [{"provider": r["provider"], "identifier": r["identifier"],
                   "verifiedAt": r["verified_at"]}
                  for r in await self.store.all(
                      "SELECT provider, identifier, verified_at FROM identities"
                      " WHERE user_id=?", (sess["user_id"],))]
        return {"user": await self._user_dto(sess["user_id"]), "identities": idents}

    async def logout(self, token):
        sess = await self.authenticate(token)
        await self.store.write("UPDATE sessions SET revoked_at=? WHERE id=?",
                         (now_iso(), sess["id"]))
        return {"ok": True}

    async def list_devices(self, token):
        """满足 R5：多设备可见。只列还有活跃会话的设备。"""
        sess = await self.authenticate(token)
        rows = await self.store.all(
            "SELECT DISTINCT d.* FROM devices d"
            " JOIN sessions s ON s.device_id = d.id"
            " WHERE d.user_id=? AND s.revoked_at IS NULL"
            " AND s.expires_at >= ?", (sess["user_id"], now_iso()))
        return {"devices": [
            {"id": r["id"], "platform": r["platform"], "name": r["name"],
             "createdAt": r["created_at"], "lastSeenAt": r["last_seen_at"],
             "current": r["id"] == sess["device_id"]} for r in rows]}

    async def revoke_device(self, token, device_id):
        """满足 R5：踢掉某台设备，该设备下次请求立即 401。"""
        sess = await self.authenticate(token)
        dev = await self.store.one("SELECT id FROM devices WHERE id=? AND user_id=?",
                             (device_id or "", sess["user_id"]))
        if not dev:
            raise ServiceError("not_found")
        n = await self.store.write(
            "UPDATE sessions SET revoked_at=? WHERE device_id=?"
            " AND revoked_at IS NULL", (now_iso(), device_id))
        if n == 0:
            raise ServiceError("not_found")
        return {"ok": True}

    async def _user_dto(self, uid):
        r = await self.store.one("SELECT * FROM users WHERE id=?", (uid,))
        return {"id": r["id"], "displayName": r["display_name"],
                "avatarUrl": r["avatar_url"], "createdAt": r["created_at"]}

    # ------------------------------------------------------------ 邀请码
    async def _check_invitation(self, code, identifier, consume):
        """乐观锁消费：靠 WHERE status='unused' 的受影响行数判断是否抢到。"""
        if not code:
            raise ServiceError("invitation_required")
        row = await self.store.one(
            "SELECT code, status, expires_at, bound_identifier"
            " FROM invitation_codes WHERE code=?", (code,))
        if not row or row["status"] != "unused":
            raise ServiceError("invitation_invalid")
        if row["expires_at"] and row["expires_at"] < now_iso():
            raise ServiceError("invitation_invalid")
        if row["bound_identifier"] and row["bound_identifier"] != identifier:
            raise ServiceError("invitation_invalid")
        if consume:
            n = await self.store.write(
                "UPDATE invitation_codes SET status='consumed', consumed_at=?"
                " WHERE code=? AND status='unused'", (now_iso(), code))
            if n != 1:
                raise ServiceError("invitation_invalid")
