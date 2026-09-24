#!/bin/sh
# 容器启动初始化：
# 1. 依赖已在镜像构建时装好（requirements.txt）
# 2. 创建数据目录并初始化数据库表（幂等，已存在则跳过）
set -e

mkdir -p /app/data

echo "[entrypoint] 初始化数据库表 ..."
python -c "import app.main; from app.database import Base, engine; Base.metadata.create_all(bind=engine); print('[entrypoint] 数据库就绪')"

echo "[entrypoint] 启动服务: $*"
exec "$@"
