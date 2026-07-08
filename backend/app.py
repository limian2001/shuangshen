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

    # 用户协议
    @app.get("/terms")
    def terms():
        from flask import Response
        html = """<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>用户协议</title>
<style>body{font-family:-apple-system,sans-serif;padding:24px 20px;color:#333;line-height:1.8;font-size:15px}h1{font-size:20px;color:#534AB7;margin-bottom:8px}h2{font-size:16px;margin-top:24px;margin-bottom:8px}p{margin:8px 0}small{color:#999}</style>
</head><body>
<h1>用户协议</h1>
<small>生效日期：2025年1月1日</small>
<h2>一、服务说明</h2>
<p>言己（以下简称"本产品"）是一款 AI 数字分身服务，允许用户创建并与 AI 角色进行对话。</p>
<h2>二、用户责任</h2>
<p>用户须遵守中华人民共和国相关法律法规，不得利用本产品从事违法违规活动。</p>
<p>用户上传的聊天记录等内容，仅用于训练对应的 AI 角色，不会用于其他用途。</p>
<h2>三、服务变更与终止</h2>
<p>本产品保留在必要时修改、暂停或终止服务的权利，并提前告知用户。</p>
<h2>四、免责声明</h2>
<p>AI 生成内容仅供娱乐参考，不代表真实人物观点，请理性使用。</p>
<h2>五、联系我们</h2>
<p>如有疑问，请联系：mianmianlife@gmail.com</p>
</body></html>"""
        return Response(html, mimetype="text/html")

    # 隐私政策
    @app.get("/privacy")
    def privacy():
        from flask import Response
        html = """<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>隐私政策</title>
<style>body{font-family:-apple-system,sans-serif;padding:24px 20px;color:#333;line-height:1.8;font-size:15px}h1{font-size:20px;color:#534AB7;margin-bottom:8px}h2{font-size:16px;margin-top:24px;margin-bottom:8px}p{margin:8px 0}small{color:#999}</style>
</head><body>
<h1>隐私政策</h1>
<small>生效日期：2025年1月1日</small>
<h2>一、我们收集的信息</h2>
<p><strong>手机号码：</strong>用于账号注册与身份识别，不会对外泄露。</p>
<p><strong>剪贴板内容：</strong>仅在您主动粘贴聊天记录时读取，用于导入数据。</p>
<p><strong>图片：</strong>目前暂不支持图片上传，相关权限为未来功能预留。</p>
<p><strong>麦克风：</strong>目前暂不支持语音功能，相关权限为未来功能预留。</p>
<h2>二、信息使用方式</h2>
<p>您上传的聊天记录仅用于生成对应 AI 角色的风格特征，存储在您的账号下，不共享给第三方。</p>
<h2>三、信息存储与安全</h2>
<p>数据存储于中国大陆服务器，采用加密传输（HTTPS）保护。</p>
<h2>四、信息删除</h2>
<p>您可随时在应用内删除替身及其全部数据，数据将被彻底清除，不可恢复。</p>
<h2>五、联系我们</h2>
<p>如需行使数据权利或有隐私疑问，请联系：mianmianlife@gmail.com</p>
</body></html>"""
        return Response(html, mimetype="text/html")

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
