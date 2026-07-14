from __future__ import annotations
"""
支付路由 — WeChat Pay 存根
实际支付需申请微信商户号后接入 wechatpay-python 库。
当前为测试模式：下单后直接发币，不经过真实支付流程。
"""
from flask import Blueprint, request, jsonify, g

from backend.utils.auth import require_auth, new_id
from backend.db.database import get_db
from backend.services.coins import award_coins

payment_bp = Blueprint("payment", __name__, url_prefix="/api/payment")

PACKAGES = [
    {"id": "p10",  "label": "10 币",  "coins": 10,  "price_fen": 990,  "price_display": "¥9.9"},
    {"id": "p20",  "label": "20 币",  "coins": 20,  "price_fen": 1880, "price_display": "¥18.8"},
    {"id": "p50",  "label": "50 币",  "coins": 50,  "price_fen": 3990, "price_display": "¥39.9"},
    {"id": "p200", "label": "200 币", "coins": 200, "price_fen": 9990, "price_display": "¥99.9"},
]


@payment_bp.get("/packages")
def get_packages():
    return jsonify(PACKAGES)


@payment_bp.post("/create-order")
@require_auth
def create_order():
    """
    创建购买订单（存根模式 — 直接发币）。
    真实模式：此处返回微信支付 prepay 参数，前端拉起支付，
    支付成功后微信异步回调 /api/payment/callback 才真正发币。
    """
    data = request.get_json() or {}
    pkg_id = data.get("package_id")
    pkg = next((p for p in PACKAGES if p["id"] == pkg_id), None)
    if not pkg:
        return jsonify({"error": "无效的套餐"}), 400

    order_id = new_id()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO purchase_records (id, user_id, amount_fen, coins, status) VALUES (?, ?, ?, ?, 'paid')",
            (order_id, g.user_id, pkg["price_fen"], pkg["coins"]),
        )

    balance = award_coins(g.user_id, pkg["coins"], "purchase", order_id)

    return jsonify({
        "order_id": order_id,
        "coins": pkg["coins"],
        "balance": balance,
        "stub": True,
        "message": f"成功购买 {pkg['coins']} 言己币（测试模式，未实际扣款）",
    })


@payment_bp.post("/callback")
def wx_pay_callback():
    """微信支付异步回调（存根，待真实接入后实现签名验证 + 发币逻辑）"""
    return jsonify({"return_code": "SUCCESS", "return_msg": "OK"})
