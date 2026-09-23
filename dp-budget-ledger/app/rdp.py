"""Rényi DP 组合数学：(epsilon, delta)-DP 与 RDP 互转及多次申请的线性组合。

实现依据（Mironov, "Rényi Differential Privacy", 2017）：

* 一个 (eps, delta)-DP 机制在 order alpha 上满足
      eps_rdp(alpha) = eps + log(1/delta) / (alpha - 1)
  （Proposition 3；当 delta = 0 时退化为 eps）。
* RDP 对顺序组合是线性的：同一 alpha 下各机制的 RDP epsilon 直接相加。
* 由 RDP 转回 (eps, delta)-DP（Proposition 3）：
      eps(alpha) = eps_rdp(alpha) + log(1/delta) / (alpha - 1)
  对所有 order 取最小值，即整体的组合消耗。

总 delta 使用基本组合定理（或等价的 (1 - prod(1-d_i))）求和：
      delta_total = 1 - Π_i (1 - delta_i)
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

from .config import settings

ORDERS: tuple[float, ...] = settings.orders


def _safe_div_delta(delta: float) -> float:
    """log(1/delta)；delta 为 0 时该项贡献为 0（纯 DP）。"""
    if delta <= 0.0:
        return 0.0
    return math.log(1.0 / delta)


def rdp_from_dp(epsilon: float, delta: float, orders: Sequence[float] = ORDERS) -> list[float]:
    """单个 (eps, delta)-DP 机制在各 alpha 上的 RDP epsilon 曲线。"""
    log_term = _safe_div_delta(delta)
    return [epsilon + log_term / (alpha - 1.0) for alpha in orders]


def compose_rdp(
    per_order_epsilons: Iterable[Sequence[float]],
    orders: Sequence[float] = ORDERS,
) -> list[float]:
    """多条 RDP 曲线的线性组合（同一 alpha 上逐点相加）。"""
    total = [0.0] * len(orders)
    count = 0
    for curve in per_order_epsilons:
        if len(curve) != len(orders):
            raise ValueError("RDP 曲线长度与 orders 不一致")
        for i, value in enumerate(curve):
            total[i] += value
        count += 1
    if count == 0:
        # 没有任何机制：定义为全 0 曲线
        return [0.0] * len(orders)
    return total


def epsilon_from_rdp(
    rdp_epsilons: Sequence[float],
    target_delta: float,
    orders: Sequence[float] = ORDERS,
) -> float:
    """给定目标 delta，由组合后的 RDP 曲线求最紧的 (eps, delta)-DP epsilon。

    当 target_delta = 0（纯 DP）时，无法走 delta 转换，直接取所有
    order 上 RDP epsilon 的最小值（alpha→∞ 时收敛到纯 DP epsilon）。
    """
    if target_delta <= 0.0:
        return min(rdp_epsilons) if rdp_epsilons else 0.0
    log_term = math.log(1.0 / target_delta)
    candidates = [
        rdp_eps + log_term / (alpha - 1.0)
        for alpha, rdp_eps in zip(orders, rdp_epsilons)
    ]
    return min(candidates)


def compose_deltas(deltas: Iterable[float]) -> float:
    """delta 的精确基本组合：1 - Π(1 - delta_i)，数值上防止超过 1。"""
    product = 1.0
    for d in deltas:
        product *= 1.0 - d
    return 1.0 - product


def consumed(
    epsilons: Sequence[float],
    deltas: Sequence[float],
    orders: Sequence[float] = ORDERS,
) -> tuple[float, float, list[float]]:
    """汇总已接受申请的组合消耗。

    返回 (epsilon_consumed, delta_consumed, composed_rdp_curve)。

    口径：组合后的机制满足 (eps, delta_total)-DP，其中 delta_total 为
    精确基本组合值；epsilon 在 *实际* delta_total 处由 RDP 曲线折算
    （delta_total=0 即纯 DP，直接取曲线最小值，对任意 delta 都成立）。
    预算判定时分别比较 eps_consumed≤上限 与 delta_total≤上限。
    """
    curves = [rdp_from_dp(e, d, orders) for e, d in zip(epsilons, deltas)]
    rdp_curve = compose_rdp(curves, orders)
    delta_total = compose_deltas(deltas)
    if not curves:
        # 没有任何已接受机制：消耗严格为 0
        return 0.0, 0.0, rdp_curve
    eps_total = epsilon_from_rdp(rdp_curve, delta_total, orders)
    return eps_total, delta_total, rdp_curve
