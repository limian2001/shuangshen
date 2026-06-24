#!/usr/bin/env python3
"""
数据库初始化脚本
运行：python3 scripts/init_db.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.db.database import init_db
from backend.core.config import config

if __name__ == "__main__":
    print(f"初始化数据库: {config.DB_PATH}")
    init_db()
    print("✅ 完成！可以运行: python3 backend/app.py")
