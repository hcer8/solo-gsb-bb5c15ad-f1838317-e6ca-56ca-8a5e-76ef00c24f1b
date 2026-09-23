"""预算账本服务：原子预留、幂等判定与预算汇总。"""
from __future__ import annotations

from typing import Any

import aiosqlite

from . import rdp
from .config import settings
from .db import db_fetchall, db_fetchone, immediate_transaction
from .models import AnalysisRequest, DatasetRegister


class ConflictError(Exception):
    """同一 request_id 但申请内容不一致（HTTP 409）。"""


class NotFoundError(Exception):
    """数据集不存在（HTTP 404）。"""


class DatasetExistsError(Exception):
    """数据集已存在（HTTP 409）。"""


async def register_dataset(conn: aiosqlite.Connection, payload: DatasetRegister) -> dict[str, Any]:
    try:
        await conn.execute(
            "INSERT INTO datasets (id, epsilon_limit, delta_limit) VALUES (?, ?, ?)",
            (payload.dataset_id, payload.epsilon_limit, payload.delta_limit),
        )
        await conn.commit()
    except aiosqlite.IntegrityError:
        # PRIMARY KEY 冲突：并发登记同一 id 时由约束兜底
        raise DatasetExistsError(payload.dataset_id) from None
    created = await db_fetchone(conn, "SELECT * FROM datasets WHERE id = ?", (payload.dataset_id,))
    return dict(created)  # type: ignore[arg-type]


async def list_datasets(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    rows = await db_fetchall(conn, "SELECT * FROM datasets ORDER BY id")
    return [dict(r) for r in rows]


async def _load_dataset(conn: aiosqlite.Connection, dataset_id: str) -> aiosqlite.Row:
    row = await db_fetchone(conn, "SELECT * FROM datasets WHERE id = ?", (dataset_id,))
    if row is None:
        raise NotFoundError(dataset_id)
    return row


def _same_payload(existing: aiosqlite.Row, payload: AnalysisRequest) -> bool:
    tol = 1e-12
    return (
        existing["query_type"] == payload.query_type
        and abs(existing["epsilon"] - payload.epsilon) <= tol
        and abs(existing["delta"] - payload.delta) <= tol
    )


async def reserve(conn: aiosqlite.Connection, dataset_id: str, payload: AnalysisRequest) -> dict[str, Any]:
    """原子地提交一次分析申请。

    返回 {"status": "accepted"|"rejected"|"replay_accepted"|"replay_rejected",
          "request": <行>}。

    临界区内（BEGIN IMMEDIATE）：
      1. 幂等检查：request_id 已存在则按内容一致性决定重放或 409；
      2. 汇总所有已接受申请的 RDP 组合消耗与 delta 组合；
      3. 模拟加入本次申请，超预算则记 rejected，否则记 accepted。
    """
    async with immediate_transaction(conn):
        dataset = await _load_dataset(conn, dataset_id)

        existing = await db_fetchone(
            conn,
            "SELECT * FROM requests WHERE dataset_id = ? AND request_id = ?",
            (dataset_id, payload.request_id),
        )
        if existing is not None:
            if not _same_payload(existing, payload):
                raise ConflictError(
                    f"request_id={payload.request_id} 已存在但参数不同；"
                    "同一 request_id 必须携带完全相同的 query_type/epsilon/delta"
                )
            result_status = "replay_" + existing["status"]
            return {"status": result_status, "request": dict(existing)}

        rows = await db_fetchall(
            conn,
            "SELECT epsilon, delta FROM requests WHERE dataset_id = ? AND status = 'accepted'",
            (dataset_id,),
        )
        eps_hist = [r["epsilon"] for r in rows]
        delta_hist = [r["delta"] for r in rows]

        eps_limit = float(dataset["epsilon_limit"])
        delta_limit = float(dataset["delta_limit"])

        # 模拟加入本次申请后的组合消耗（按实际组合 delta 折算 epsilon）
        eps_candidate = eps_hist + [payload.epsilon]
        delta_candidate = delta_hist + [payload.delta]
        eps_consumed, delta_consumed, _ = rdp.consumed(eps_candidate, delta_candidate)

        over_eps = eps_consumed > eps_limit + settings.budget_tolerance
        over_delta = delta_consumed > delta_limit + settings.budget_tolerance

        if over_eps or over_delta:
            reasons = []
            if over_eps:
                reasons.append(
                    f"epsilon 超预算：组合后 {eps_consumed:.6g} > 上限 {eps_limit:.6g}"
                )
            if over_delta:
                reasons.append(
                    f"delta 超预算：组合后 {delta_consumed:.6g} > 上限 {delta_limit:.6g}"
                )
            reason = "；".join(reasons)
            cur = await conn.execute(
                """
                INSERT INTO requests
                    (request_id, dataset_id, query_type, epsilon, delta, status, reason)
                VALUES (?, ?, ?, ?, ?, 'rejected', ?)
                """,
                (
                    payload.request_id,
                    dataset_id,
                    payload.query_type,
                    payload.epsilon,
                    payload.delta,
                    reason,
                ),
            )
            row = await db_fetchone(conn, "SELECT * FROM requests WHERE id = ?", (cur.lastrowid,))
            return {"status": "rejected", "request": dict(row)}  # type: ignore[arg-type]

        cur = await conn.execute(
            """
            INSERT INTO requests
                (request_id, dataset_id, query_type, epsilon, delta, status, reason)
            VALUES (?, ?, ?, ?, ?, 'accepted', NULL)
            """,
            (
                payload.request_id,
                dataset_id,
                payload.query_type,
                payload.epsilon,
                payload.delta,
            ),
        )
        row = await db_fetchone(conn, "SELECT * FROM requests WHERE id = ?", (cur.lastrowid,))
        return {"status": "accepted", "request": dict(row)}  # type: ignore[arg-type]


async def get_request(
    conn: aiosqlite.Connection, dataset_id: str, request_id: str
) -> dict[str, Any] | None:
    await _load_dataset(conn, dataset_id)
    row = await db_fetchone(
        conn,
        "SELECT * FROM requests WHERE dataset_id = ? AND request_id = ?",
        (dataset_id, request_id),
    )
    return dict(row) if row is not None else None


async def list_requests(conn: aiosqlite.Connection, dataset_id: str) -> list[dict[str, Any]]:
    await _load_dataset(conn, dataset_id)
    rows = await db_fetchall(
        conn,
        "SELECT * FROM requests WHERE dataset_id = ? ORDER BY id",
        (dataset_id,),
    )
    return [dict(r) for r in rows]


async def budget_summary(conn: aiosqlite.Connection, dataset_id: str) -> dict[str, Any]:
    dataset = await _load_dataset(conn, dataset_id)
    rows = await db_fetchall(
        conn,
        "SELECT epsilon, delta, status FROM requests WHERE dataset_id = ?",
        (dataset_id,),
    )
    accepted = [r for r in rows if r["status"] == "accepted"]
    rejected = [r for r in rows if r["status"] == "rejected"]

    eps_limit = float(dataset["epsilon_limit"])
    delta_limit = float(dataset["delta_limit"])

    eps_consumed, delta_consumed, curve = rdp.consumed(
        [r["epsilon"] for r in accepted],
        [r["delta"] for r in accepted],
    )

    eps_remaining = max(0.0, eps_limit - eps_consumed)
    delta_remaining = max(0.0, delta_limit - delta_consumed)

    return {
        "dataset_id": dataset_id,
        "epsilon_limit": eps_limit,
        "delta_limit": delta_limit,
        "epsilon_consumed": eps_consumed,
        "delta_consumed": delta_consumed,
        "epsilon_remaining": eps_remaining,
        "delta_remaining": delta_remaining,
        "accepted_requests": len(accepted),
        "rejected_requests": len(rejected),
        "rdp_curve": [
            {"order": alpha, "epsilon": value}
            for alpha, value in zip(rdp.ORDERS, curve)
        ],
    }
