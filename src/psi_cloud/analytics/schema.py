"""埋点表结构。与原 collector.py 建的 events 表完全一致,不做变更。

另外三张表(access_metrics / download_metrics / behavior_metrics)是
指标说明文档,由人工维护,不由本服务建表或写入,故此处不声明。
"""

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS events (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      name       TEXT NOT NULL,
      page       TEXT,
      url        TEXT,
      referrer   TEXT,
      os         TEXT,
      device     TEXT,
      lang       TEXT,
      client_id  TEXT,
      session_id TEXT,
      ip         TEXT,
      region     TEXT,
      props      TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_name ON events(name)",
)
