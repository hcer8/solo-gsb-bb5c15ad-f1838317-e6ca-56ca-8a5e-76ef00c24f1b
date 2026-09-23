"""单元测试：RDP 数学 + ASGI 端到端（预算、幂等、并发）。"""
from __future__ import annotations

import asyncio
import math

import pytest
from fastapi.testclient import TestClient

from app import rdp
from app.config import settings
from app.main import app


# ---------- RDP 数学性质 ----------

def test_pure_dp_rdp_curve_constant():
    curve = rdp.rdp_from_dp(0.5, 0.0)
    assert all(abs(v - 0.5) < 1e-12 for v in curve)


def test_approx_dp_rdp_formula():
    eps, delta = 0.3, 1e-6
    curve = rdp.rdp_from_dp(eps, delta)
    for alpha, got in zip(rdp.ORDERS, curve):
        expected = eps + math.log(1 / delta) / (alpha - 1)
        assert got == pytest.approx(expected)


def test_compose_linear_and_recovers_sum_pure_dp():
    curves = [rdp.rdp_from_dp(e, 0.0) for e in (0.2, 0.3, 0.5)]
    total = rdp.compose_rdp(curves)
    assert all(v == pytest.approx(1.0) for v in total)
    assert rdp.epsilon_from_rdp(total, 0.0) == pytest.approx(1.0)


def test_compose_deltas():
    assert rdp.compose_deltas([]) == 0.0
    assert rdp.compose_deltas([0.1, 0.2]) == pytest.approx(1 - 0.9 * 0.8)


def test_consumed_matches_manual_conversion():
    # 两个 (0.4, 1e-6) 机制，按实际组合 delta 折算
    eps, d, curve = rdp.consumed([0.4, 0.4], [1e-6, 1e-6])
    delta_total = 1 - (1 - 1e-6) ** 2
    manual = min(
        2 * (0.4 + math.log(1 / 1e-6) / (a - 1)) + math.log(1 / delta_total) / (a - 1)
        for a in rdp.ORDERS
    )
    assert eps == pytest.approx(manual)
    assert d == pytest.approx(delta_total)


def test_consumed_pure_dp_reports_exact_epsilon():
    # 纯 DP 申请：组合消耗就是朴素求和，不因登记 delta 被放大
    eps, d, curve = rdp.consumed([0.4, 0.5], [0.0, 0.0])
    assert eps == pytest.approx(0.9, abs=1e-12)
    assert d == 0.0
    assert all(v == pytest.approx(0.9) for v in curve)


# ---------- API 端到端 ----------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 每个测试独立数据库
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"), raising=False)
    with TestClient(app) as c:
        yield c


def _register(client, ds="ds1", eps=1.0, delta=1e-5):
    return client.post("/v1/datasets", json={
        "dataset_id": ds, "epsilon_limit": eps, "delta_limit": delta})


def test_register_and_budget_empty(client):
    assert _register(client).status_code == 201
    r = client.get("/v1/datasets/ds1/budget")
    assert r.status_code == 200
    body = r.json()
    assert body["epsilon_consumed"] == 0.0
    assert body["epsilon_remaining"] == pytest.approx(1.0)
    assert body["rdp_curve"]


def test_duplicate_dataset_409(client):
    _register(client)
    assert _register(client).status_code == 409


def test_accept_reject_and_idempotent_replay(client):
    _register(client, eps=1.0, delta=0.0)

    r1 = client.post("/v1/datasets/ds1/requests", json={
        "request_id": "a", "query_type": "count", "epsilon": 0.6, "delta": 0.0})
    assert r1.status_code == 201 and r1.json()["status"] == "accepted"

    # 幂等重放：200 + replayed
    r1b = client.post("/v1/datasets/ds1/requests", json={
        "request_id": "a", "query_type": "count", "epsilon": 0.6, "delta": 0.0})
    assert r1b.status_code == 200 and r1b.json()["replayed"] is True

    # 同 ID 不同参数：409
    rconf = client.post("/v1/datasets/ds1/requests", json={
        "request_id": "a", "query_type": "sum", "epsilon": 0.1, "delta": 0.0})
    assert rconf.status_code == 409

    # 超预算：400 rejected 并落库
    r2 = client.post("/v1/datasets/ds1/requests", json={
        "request_id": "b", "query_type": "mean", "epsilon": 0.5, "delta": 0.0})
    assert r2.status_code == 400 and r2.json()["status"] == "rejected"
    assert "epsilon" in r2.json()["reason"]

    # rejected 也幂等：重放仍是 400 + replayed
    r2b = client.post("/v1/datasets/ds1/requests", json={
        "request_id": "b", "query_type": "mean", "epsilon": 0.5, "delta": 0.0})
    assert r2b.status_code == 400 and r2b.json()["replayed"] is True

    # 被拒后小额度申请仍可成功（拒绝不消耗预算）
    r3 = client.post("/v1/datasets/ds1/requests", json={
        "request_id": "c", "query_type": "count", "epsilon": 0.4, "delta": 0.0})
    assert r3.status_code == 201

    summary = client.get("/v1/datasets/ds1/budget").json()
    assert summary["accepted_requests"] == 2
    assert summary["rejected_requests"] == 1
    assert summary["epsilon_consumed"] == pytest.approx(1.0, abs=1e-9)


def test_delta_budget_enforced(client):
    _register(client, eps=10.0, delta=1e-5)
    r = client.post("/v1/datasets/ds1/requests", json={
        "request_id": "d", "query_type": "gaussian", "epsilon": 1.0, "delta": 1e-3})
    assert r.status_code == 400 and "delta" in r.json()["reason"]


def test_dataset_not_found(client):
    assert client.get("/v1/datasets/nope/budget").status_code == 404


def test_concurrent_requests_are_atomic(client):
    _register(client, ds="conc", eps=2.0, delta=0.0)

    def send(i):
        return client.post("/v1/datasets/conc/requests", json={
            "request_id": f"k-{i}", "query_type": "gaussian",
            "epsilon": 0.3, "delta": 0.0})

    # TestClient 基于门户；用线程池制造并发
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=10) as pool:
        statuses = [r.status_code for r in pool.map(send, range(10))]

    assert statuses.count(201) == 6
    assert statuses.count(400) == 4

    summary = client.get("/v1/datasets/conc/budget").json()
    assert summary["epsilon_consumed"] == pytest.approx(1.8, abs=1e-9)
    assert summary["epsilon_remaining"] == pytest.approx(0.2, abs=1e-9)


def test_concurrent_same_request_id_collapses(client):
    _register(client, ds="idem", eps=2.0, delta=0.0)
    payload = {"request_id": "same", "query_type": "count",
               "epsilon": 0.3, "delta": 0.0}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(
            lambda _: client.post("/v1/datasets/idem/requests", json=payload),
            range(10)))

    codes = sorted(r.status_code for r in results)
    # 恰好一次 201，其余全部幂等 200
    assert codes == [200] * 9 + [201]
    summary = client.get("/v1/datasets/idem/budget").json()
    assert summary["accepted_requests"] == 1
