#!/bin/bash
# ─────────────────────────────────────────────
# 替身 (Shuangshen) — 一键部署 / 升级脚本
# 在服务器上运行：bash deploy.sh
# ─────────────────────────────────────────────
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE_NAME="shuangshen"
CONTAINER_NAME="shuangshen"
DATA_DIR="$APP_DIR/data"

echo "🔨 [1/4] 构建 Docker 镜像..."
docker build -t "$IMAGE_NAME" .

echo "🔄 [2/4] 停止旧容器..."
docker stop "$CONTAINER_NAME" 2>/dev/null || true
docker rm   "$CONTAINER_NAME" 2>/dev/null || true

echo "🚀 [3/3] 启动新容器..."
docker run -d \
  --name "$CONTAINER_NAME" \
  -p 5000:5000 \
  -v "$DATA_DIR:/app/data" \
  --env-file "$APP_DIR/.env" \
  --restart unless-stopped \
  "$IMAGE_NAME"

echo ""
echo "✅ 部署完成！"
echo "   容器状态：$(docker ps --filter name=$CONTAINER_NAME --format '{{.Status}}')"
echo "   查看日志：docker logs -f $CONTAINER_NAME"
