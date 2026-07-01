#!/bin/sh
set -e

echo "=== 替身 Shuangshen ==="
echo "初始化数据库..."
python -c "from backend.db.database import init_db; init_db()"

echo "启动服务器 (gunicorn, port ${PORT:-5000})..."
exec gunicorn \
  --bind "0.0.0.0:${PORT:-5000}" \
  --workers 1 \
  --threads 8 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  "backend.app:create_app()"
