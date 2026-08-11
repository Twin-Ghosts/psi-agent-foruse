-- psi-agent 认证服务 SQLite schema（第 1 步产出）
--
-- 逐条对应《psi-agent C 端注册登录方案》「数据模型」一节，未自行增删表。
--
-- 关键约束都由数据库保证，不靠应用层自觉：
--   identities 的复合主键     防重复注册（应用层先查后插在并发下会建出两个账号）
--   devices 的 UNIQUE         重装不刷出新设备
--   sessions.token_hash       只存哈希，不存 token 原文
--
-- 过期数据没有 TTL 兜底机制，必须自己清（见 cleanup.sql 与验收标准）。
--
-- PRAGMA 分两类，务必分清，否则会以为设了、其实没生效：
--   journal_mode 是**数据库级**且持久化的，在这里设一次即可。
--   foreign_keys 与 busy_timeout 是**连接级**的，每开一个新连接都要重设；
--     写在本文件里只对建库那一个连接有效，对应用的其它连接毫无作用。
--     应用层必须在每次 connect 之后执行这两行——不是可选优化，漏掉就等于
--     没有外键约束（SQLite 默认 foreign_keys=OFF）。
--   参见 自检_schema.py 的 connect()，以及"新连接默认 foreign_keys=0"那条断言。

PRAGMA journal_mode = WAL;      -- 数据库级、持久化：设一次即可
PRAGMA busy_timeout = 5000;     -- 连接级：应用每个连接都要重设
PRAGMA foreign_keys = ON;       -- 连接级：应用每个连接都要重设

-- ---------------------------------------------------------------- 账号

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'disabled')),
    display_name  TEXT,
    avatar_url    TEXT
);

-- 账号与登录方式分离：一个 user 可绑多种身份。
-- PRIMARY KEY(provider, identifier) 是防重复注册的唯一保障。
-- 用本表而非在 users 上加 phone/email 两列：路线图有微信小程序，
-- 加一行 provider 即可，两列形态则要改表。
CREATE TABLE IF NOT EXISTS identities (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider    TEXT NOT NULL
                CHECK (provider IN ('phone', 'email', 'wechat', 'google',
                                    'apple')),
    identifier  TEXT NOT NULL,
    verified_at TEXT,
    PRIMARY KEY (provider, identifier)
);

CREATE INDEX IF NOT EXISTS idx_identities_user ON identities(user_id);

-- ---------------------------------------------------------------- 设备与会话

-- device_key 由客户端本地生成并持久化，保证重装不刷出新设备。
CREATE TABLE IF NOT EXISTS devices (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_key   TEXT NOT NULL,
    platform     TEXT NOT NULL,
    name         TEXT,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT,
    UNIQUE (user_id, device_key)
);

CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);

-- token_hash 只存哈希（SHA256），不存原文；撤销标 revoked_at，即时生效。
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id    TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_device ON sessions(device_id);
-- 清理过期会话用
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

-- ---------------------------------------------------------------- 验证码与配额

-- 仅邮箱路径需要：手机号验证码由阿里云 PNVS 托管，我们零存储。
-- code_hash 为 HMAC + 服务端 salt，不存明文。
CREATE TABLE IF NOT EXISTS email_codes (
    identifier TEXT PRIMARY KEY,
    code_hash  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    sent_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_email_codes_expires ON email_codes(expires_at);

-- 限频计数。scope='identifier' 按手机号/邮箱，scope='ip' 按客户端 IP。
-- 归一化必须在写入本表之前完成，否则同一个人能绕过限频。
CREATE TABLE IF NOT EXISTS send_quota (
    scope        TEXT NOT NULL CHECK (scope IN ('identifier', 'ip')),
    key          TEXT NOT NULL,
    window_start TEXT NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, key, window_start)
);

CREATE INDEX IF NOT EXISTS idx_send_quota_window ON send_quota(window_start);

-- ---------------------------------------------------------------- 邀请码

-- 消费用乐观锁：UPDATE ... WHERE status='unused'，靠受影响行数判断是否抢到。
CREATE TABLE IF NOT EXISTS invitation_codes (
    code                TEXT PRIMARY KEY,
    status              TEXT NOT NULL DEFAULT 'unused'
                        CHECK (status IN ('unused', 'consumed', 'revoked')),
    expires_at          TEXT,
    bound_identifier    TEXT,
    consumed_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    consumed_at         TEXT,
    note                TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_invitation_status ON invitation_codes(status);
