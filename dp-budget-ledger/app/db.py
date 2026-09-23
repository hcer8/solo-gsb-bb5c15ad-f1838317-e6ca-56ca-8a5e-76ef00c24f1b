"""SQLite 异步持久层（aiosqlite）。

并发安全策略：
* 每个预留事务先执行 ``BEGIN IMMEDIATE`` 立即获取写锁（RESERVED），
  使"读取当前消耗 → 判定预算 → 写入预留"成为串行化临界区；
* 配合 requests.request_id 上的 UNIQUE 约束，同一 request_id 的
  并发提交由数据库唯一索引兜底，保证幂等；
* 遇到 SQLITE_BUSY 时指数退避重试。
"""
from __future__ import annotations

import json
import os
import random
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import aiosqlite

from .config import settings
SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id              TEXT PRIMARY KEY,
    epsilon_limit   REAL NOT NULL,
    delta_limit     REAL NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT NOT NULL,
    dataset_id      TEXT NOT NULL REFERENCES datasets(id),
    query_type      TEXT NOT NULL,
    epsilon         REAL NOT NULL,
    delta           REAL NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('accepted', 'rejected')),
    reason          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (dataset_id, request_id)
);

CREATE INDEX IF NOT EXISTS idx_requests_dataset ON requests(dataset_id);
"""


async def init_db() -> None:
    """建库建表；数据目录不存在时自动创建。"""
    db_dir = os.path.dirname(settings.db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    async with aiosqlite.connect(settings.db_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def get_connection() -> AsyncIterator[aiosqlite.Connection]:
    """FastAPI 依赖：每请求一个连接（yield 风格，退出时自动关闭）。"""
    conn = await aiosqlite.connect(settings.db_path)
    conn.row_factory = aiosqlite.Row
    # WAL 提升读写并发；busy_timeout 作为应用层重试之外的第二道保险
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        await conn.close()


async def db_fetchone(conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
    async with conn.execute(sql, params) as cur:
        return await cur.fetchone()


async def db_fetchall(conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
    async with conn.execute(sql, params) as cur:
        return list(await cur.fetchall())


@asynccontextmanager
async def immediate_transaction(conn: aiosqlite.Connection) -> AsyncIterator[None]:
    """BEGIN IMMEDIATE 临界区，SQLITE_BUSY 时指数退避重试。

    进入即持有写锁，保证事务内的"读消耗-判预算-写预留"对其他
    写事务原子可见。
    """
    attempt = 0
    while True:
        try:
            await conn.execute("BEGIN IMMEDIATE")
            break
        except aiosqlite.OperationalError as exc:  # database is locked
            if "locked" not in str(exc).lower() or attempt >= settings.max_tx_retries:
                raise
            delay = min(0.001 * (2 ** attempt) + random.uniform(0, 0.002), 0.2)
            attempt += 1
            await _sleep(delay)
    try:
        yield
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
