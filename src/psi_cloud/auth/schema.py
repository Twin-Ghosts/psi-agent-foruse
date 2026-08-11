"""认证服务表结构。

两表身份模型:users 是人,identities 是登录方式。将来接微信 /
Google / Apple 是往 identities 加行,不是给 users 加列。
"""

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
      id            TEXT PRIMARY KEY,
      created_at    TEXT NOT NULL DEFAULT (datetime('now')),
      status        TEXT NOT NULL DEFAULT active,
      display_name  TEXT,
      avatar_url    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS identities (
      user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      provider    TEXT NOT NULL,
      identifier  TEXT NOT NULL,
      verified_at TEXT NOT NULL DEFAULT (datetime('now')),
      PRIMARY KEY (provider, identifier)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_identities_user ON identities(user_id)",
    """
    CREATE TABLE IF NOT EXISTS devices (
      id          TEXT PRIMARY KEY,
      user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      device_key  TEXT NOT NULL,
      platform    TEXT,
      name        TEXT,
      created_at  TEXT NOT NULL DEFAULT (datetime('now')),
      last_seen_at TEXT,
      UNIQUE (user_id, device_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
      id           TEXT PRIMARY KEY,
      user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      device_id    TEXT REFERENCES devices(id) ON DELETE SET NULL,
      token_hash   TEXT NOT NULL UNIQUE,
      created_at   TEXT NOT NULL DEFAULT (datetime('now')),
      last_used_at TEXT,
      expires_at   TEXT NOT NULL,
      revoked_at   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)",
    """
    CREATE TABLE IF NOT EXISTS email_codes (
      identifier TEXT PRIMARY KEY,
      code_hash  TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      attempts   INTEGER NOT NULL DEFAULT 0,
      sent_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_email_codes_expires ON email_codes(expires_at)",
    """
    CREATE TABLE IF NOT EXISTS send_quota (
      scope        TEXT NOT NULL,
      key          TEXT NOT NULL,
      window_start TEXT NOT NULL,
      count        INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (scope, key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_send_quota_window ON send_quota(window_start)",
    """
    CREATE TABLE IF NOT EXISTS invitation_codes (
      code              TEXT PRIMARY KEY,
      status            TEXT NOT NULL DEFAULT unused,
      expires_at        TEXT,
      bound_identifier  TEXT,
      consumed_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
      consumed_at       TEXT,
      note              TEXT,
      created_at        TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
)
