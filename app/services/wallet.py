from __future__ import annotations

from uuid import uuid4

import asyncpg

from app.db import get_pool


async def get_wallet_summary(user_id: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        balance = await conn.fetchval("SELECT vpn_balance_stars FROM users WHERE id = $1", user_id)
        tx_rows = await conn.fetch(
            """
            SELECT id, kind, status, amount, currency, provider_amount, provider_currency, description, created_at, paid_at
            FROM wallet_transactions
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT 20
            """,
            user_id,
        )
    return {
        "balance": int(balance or 0),
        "transactions": [
            {
                "id": row["id"],
                "kind": row["kind"],
                "status": row["status"],
                "amount": row["amount"],
                "currency": row["currency"],
                "provider_amount": row["provider_amount"],
                "provider_currency": row["provider_currency"],
                "description": row["description"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "paid_at": row["paid_at"].isoformat() if row["paid_at"] else None,
            }
            for row in tx_rows
        ],
    }


async def create_pending_topup(
    user_id: int,
    amount_stars: int,
) -> asyncpg.Record:
    pool = await get_pool()
    payload = f"wallet_topup:{user_id}:{uuid4().hex}"
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            INSERT INTO wallet_transactions (
                user_id, kind, status, amount, currency, provider_amount, provider_currency, description, payload
            ) VALUES ($1, 'topup', 'pending', $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            user_id,
            amount_stars,
            "XTR",
            amount_stars,
            "XTR",
            f"Пополнение VPN-баланса на {amount_stars} Stars",
            payload,
        )


async def mark_topup_paid(
    *,
    payload: str,
    telegram_payment_charge_id: str | None,
    total_amount: int,
    currency: str,
) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, user_id, status, amount, provider_amount
                FROM wallet_transactions
                WHERE payload = $1 AND kind = 'topup'
                FOR UPDATE
                """,
                payload,
            )
            if not row:
                return False
            if row["status"] == "paid":
                return True
            expected_provider_amount = int(row["provider_amount"] or 0)
            if expected_provider_amount and int(total_amount or 0) and expected_provider_amount != int(total_amount):
                return False
            amount = int(row["amount"] or total_amount or 0)
            await conn.execute(
                """
                UPDATE wallet_transactions
                SET status = 'paid',
                    paid_at = NOW(),
                    provider_amount = COALESCE(provider_amount, $4),
                    provider_currency = $2,
                    telegram_payment_charge_id = $3
                WHERE id = $1
                """,
                row["id"],
                currency,
                telegram_payment_charge_id,
                total_amount,
            )
            await conn.execute(
                "UPDATE users SET vpn_balance_stars = vpn_balance_stars + $1 WHERE id = $2",
                amount,
                row["user_id"],
            )
    return True


async def spend_balance_for_purchase(user_id: int, amount: int, description: str) -> tuple[bool, int]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE users
                SET vpn_balance_stars = vpn_balance_stars - $1
                WHERE id = $2 AND vpn_balance_stars >= $1
                RETURNING vpn_balance_stars
                """,
                amount,
                user_id,
            )
            if not row:
                current_balance = await conn.fetchval("SELECT vpn_balance_stars FROM users WHERE id = $1", user_id)
                return False, int(current_balance or 0)
            await conn.execute(
                """
                INSERT INTO wallet_transactions (
                    user_id, kind, status, amount, currency, description
                ) VALUES ($1, 'purchase', 'paid', $2, 'XTR', $3)
                """,
                user_id,
                -abs(amount),
                description,
            )
            return True, int(row["vpn_balance_stars"] or 0)


async def refund_purchase(user_id: int, amount: int, description: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE users
                SET vpn_balance_stars = vpn_balance_stars + $1
                WHERE id = $2
                RETURNING vpn_balance_stars
                """,
                amount,
                user_id,
            )
            await conn.execute(
                """
                INSERT INTO wallet_transactions (
                    user_id, kind, status, amount, currency, description
                ) VALUES ($1, 'refund', 'paid', $2, 'XTR', $3)
                """,
                user_id,
                abs(amount),
                description,
            )
            return int(row["vpn_balance_stars"] or 0)
