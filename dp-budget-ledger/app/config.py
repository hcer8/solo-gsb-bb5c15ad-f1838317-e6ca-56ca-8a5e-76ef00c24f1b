"""服务配置，全部可通过环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _as_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw is not None else default


@dataclass
class Settings:
    db_path: str = os.getenv("DP_LEDGER_DB_PATH", "/data/ledger.db")
    host: str = os.getenv("DP_LEDGER_HOST", "0.0.0.0")
    port: int = int(os.getenv("DP_LEDGER_PORT", "8000"))

    # Rényi DP 计算使用的 alpha orders（Opacus / Tumult 常用集合）
    orders: tuple[float, ...] = (2, 3, 4, 5, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 256)

    # 浮点比较容忍度
    budget_tolerance: float = _as_float("DP_LEDGER_TOLERANCE", 1e-12)

    # SQLite 写事务冲突时的最大重试次数
    max_tx_retries: int = int(os.getenv("DP_LEDGER_TX_RETRIES", "50"))


settings = Settings()
