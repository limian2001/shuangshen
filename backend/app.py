"""
替身 (Shuangshen) — Flask 应用入口
"""
import sys
import os
from pathlib import Path

# 确保项目根目录在 Python 路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, send_from_directory
from backend.core.config import config
from backend.db.database import init_db

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# 注册所有路由蓝图
from backend.api.routes.auth import auth_bp
from backend.api.routes.avatars import avatars_bp
from backend.api.routes.memories import memories_bp
from backend.api.routes.ingest import ingest_bp
from backend.api.routes.chat import chat_bp
from backend.api.routes.topics import topics_bp
from backend.api.routes.admin import admin_bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH

    # 注册蓝图
    app.register_blueprint(auth_bp)
    app.register_blueprint(avatars_bp)
    app.register_blueprint(memories_bp)
    app.register_blueprint(ingest_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(topics_bp)
    app.register_blueprint(admin_bp)

    # CORS — 允许前端页面调用 API
    @app.after_request
    def add_cors(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        return response

    @app.before_request
    def handle_options():
        from flask import request
        if request.method == "OPTIONS":
            from flask import Response
            return Response(status=200)

    # 前端页面
    @app.get("/app")
    @app.get("/app/")
    def frontend():
        return send_from_directory(str(FRONTEND_DIR), "index.html")

    # 管理后台页面
    @app.get("/admin")
    @app.get("/admin/")
    def admin_frontend():
        return send_from_directory(str(FRONTEND_DIR), "admin.html")

    # 微信业务域名校验文件
    @app.get("/cp52rCayvx.txt")
    def wx_verify():
        from flask import Response
        return Response("8b94997c641f54463a380a27a70d7b5a", mimetype="text/plain")

    # 健康检查
    @app.get("/")
    def health():
        return jsonify({
            "status": "ok",
            "service": "Shuangshen API",
            "version": "0.1.0",
            "llm_provider": config.LLM_PROVIDER,
            "db": str(config.DB_PATH),
        })

    # 统一错误处理
    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "接口不存在"}), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify({"error": "请求方法不允许"}), 405

    @app.errorhandler(413)
    def too_large(e):
        return jsonify({"error": f"文件过大，最大 {config.MAX_CONTENT_LENGTH // 1024 // 1024}MB"}), 413

    @app.errorhandler(500)
    def server_error(e):
        return jsonify({"error": "服务器内部错误", "detail": str(e)}), 500

    return app


if __name__ == "__main__":
    # 首次启动自动初始化数据库
    init_db()

    app = create_app()
    print(f"""
╔══════════════════════════════════════════╗
║    替身 (Shuangshen) API Server v0.1    ║
╠══════════════════════════════════════════╣
║  地址: http://localhost:{config.PORT}            ║
║  数据库: {str(config.DB_PATH)[-30:]:30s} ║
║  LLM:  {config.LLM_PROVIDER:10s}                        ║
╚══════════════════════════════════════════╝
""")
    app.run(host="0.0.0.0", port=config.PORT, debug=config.DEBUG)
