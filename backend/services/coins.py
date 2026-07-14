from __future__ import annotations
"""
言己币（YanJi Coins）服务：充值 / 扣款 / 余额查询
"""
from backend.db.database import get_db
from backend.utils.auth import new_id


def get_balance(user_id: str) -> int:
    with get_db() as conn:
        row = conn.execute("SELECT coins FROM users WHERE id = ?", (user_id,)).fetchone()
    return (row["coins"] or 0) if row else 0


def award_coins(user_id: str, amount: int, reason: str, ref_id: str = "") -> int:
    """给用户增加言己币。返回操作后余额。"""
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET coins = COALESCE(coins, 0) + ? WHERE id = ?",
            (amount, user_id),
        )
        row = conn.execute("SELECT coins FROM users WHERE id = ?", (user_id,)).fetchone()
        balance = row["coins"] if row else 0
        conn.execute(
            """INSERT INTO coin_transactions (id, user_id, amount, reason, ref_id, balance_after)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (new_id(), user_id, amount, reason, ref_id or "", balance),
        )
    return balance


def spend_coins(user_id: str, amount: int, reason: str, ref_id: str = "") -> tuple[bool, int]:
    """
    扣减言己币。返回 (success, current_balance)。
    余额不足时返回 (False, current_balance)，不扣款。
    """
    with get_db() as conn:
        row = conn.execute("SELECT coins FROM users WHERE id = ?", (user_id,)).fetchone()
        current = (row["coins"] or 0) if row else 0
        if current < amount:
            return False, current
        new_balance = current - amount
        conn.execute("UPDATE users SET coins = ? WHERE id = ?", (new_balance, user_id))
        conn.execute(
            """INSERT INTO coin_transactions (id, user_id, amount, reason, ref_id, balance_after)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (new_id(), user_id, -amount, reason, ref_id or "", new_balance),
        )
    return True, new_balance
