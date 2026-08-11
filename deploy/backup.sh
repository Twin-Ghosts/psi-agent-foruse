#!/bin/sh
# SQLite 热备。
#
# 必须用 VACUUM INTO（或 .backup），**不能直接 cp** —— WAL 模式下 cp 可能拿到
# 一个不含最新事务、甚至结构损坏的文件。VACUUM INTO 产出的是一致快照。
#
# 备份完立即校验：备份文件存在不等于可用，integrity_check 过了才算备份成功。

set -eu

DB="${AUTH_DB:-/data/auth.db}"
DIR="${BACKUP_DIR:-/backups}"
INTERVAL="${BACKUP_INTERVAL:-3600}"
KEEP="${BACKUP_KEEP:-48}"

mkdir -p "$DIR"

backup_once() {
	ts="$(date -u +%Y%m%dT%H%M%SZ)"
	out="$DIR/auth-$ts.db"

	if [ ! -f "$DB" ]; then
		echo "[backup] 源库不存在，跳过：$DB"
		return 0
	fi

	python - "$DB" "$out" <<'PY'
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
try:
    # VACUUM INTO 要求目标不存在
    conn.execute("VACUUM INTO ?", (dst,))
finally:
    conn.close()

# 备份文件存在 != 可用：必须校验完整性，否则等于没有备份
chk = sqlite3.connect(dst)
try:
    ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    n = chk.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
finally:
    chk.close()
if ok != "ok":
    raise SystemExit(f"备份完整性校验失败：{ok}")
if n < 7:
    raise SystemExit(f"备份表数异常：{n}（应有 7 张）")
print(f"[backup] 完成 {dst}（integrity_check={ok}, 表数={n}）")
PY

	# 只保留最近 KEEP 份
	ls -1t "$DIR"/auth-*.db 2>/dev/null | tail -n "+$((KEEP + 1))" \
		| while read -r old; do
			echo "[backup] 清理旧备份 $old"
			rm -f "$old"
		done
}

echo "[backup] 启动：每 ${INTERVAL}s 一次，保留 ${KEEP} 份"
while true; do
	backup_once || echo "[backup] 本次失败，等下一轮"
	sleep "$INTERVAL"
done
