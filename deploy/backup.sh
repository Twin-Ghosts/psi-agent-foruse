#!/usr/bin/env bash
# SQLite 热备。
#
# ** 必须用 VACUUM INTO,不能 cp **:WAL 模式下 .db 文件本身不含最新
# 提交(在 -wal 里),cp 可能拿到撕裂状态。VACUUM INTO 产出的是一致快照。
#
# 跑在宿主机上,不依赖容器 —— 容器删了备份照常。
set -euo pipefail

ROOT=/srv/psi-cloud
DEST="$ROOT/backups"
KEEP_DAYS=14
STAMP=$(date +%Y%m%d-%H%M%S)

mkdir -p "$DEST"

for name in auth analytics; do
  src="$ROOT/data/$name/$name.db"
  [ -f "$src" ] || { echo "skip $name (no db yet)"; continue; }
  out="$DEST/$name-$STAMP.db"
  python3 - "$src" "$out" <<'PY'
import sqlite3, sys
src, out = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(src)
try:
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("VACUUM INTO ?", (out,))
finally:
    conn.close()
PY
  gzip -f "$out"
  echo "backed up $name -> $out.gz"
done

# 过期清理
find "$DEST" -name "*.db.gz" -mtime +$KEEP_DAYS -delete
echo "retention: kept last $KEEP_DAYS days"
