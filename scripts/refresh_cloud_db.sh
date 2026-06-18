#!/usr/bin/env bash
# 刷新 Streamlit Cloud 上的只读 DB 快照。
#
# 用途：本地跑完采集后，把最新的 competitor_intel.db 提交进 git 并 push，
#       云上 dashboard 是只读展示，数据靠这个脚本更新。
#
# 用法：
#   ./scripts/refresh_cloud_db.sh            # 跳过采集，只刷新已生成的 DB
#   ./scripts/refresh_cloud_db.sh --collect  # 先跑 collector 再刷新
#
# 背景：Streamlit Cloud 从 git 克隆部署，运行时写入的文件会在重建时丢失，
#       所以 DB 必须进 git；data/*.db 在 .gitignore 里，用 -f 绕过。

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

DB_PATH="data/competitor_intel.db"

if [[ ! -f "$DB_PATH" ]]; then
  echo "❌ 找不到 $DB_PATH，请先跑采集器。" >&2
  exit 1
fi

# 可选：跑采集器
if [[ "${1:-}" == "--collect" ]]; then
  echo "▶ 跑采集器 ..."
  python3 src/collector.py
fi

# 把 WAL 里的数据落进主 db 文件，保证提交的快照自包含
echo "▶ checkpoint WAL ..."
python3 -c "import sqlite3; c=sqlite3.connect('$DB_PATH'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"

# 只提交主 db，-wal/-shm 保持 gitignore
echo "▶ 提交 DB ..."
git add -f "$DB_PATH"
git commit -m "chore: refresh cloud DB snapshot

Co-Authored-By: Claude <noreply@anthropic.com>"

echo "▶ push ..."
git push

echo "✅ 完成。去 Streamlit Cloud → Manage app → Reboot 即可生效。"
