"""端到端演示：登记、组合消耗、幂等重放、并发原子判定。

仅使用标准库（urllib + 线程），容器外运行：
    python scripts/demo.py [base_url]
默认 http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n== {title} ==> HTTP {status}")
    print(json.dumps(body, indent=2, ensure_ascii=False))


def main() -> None:
    dataset_id = "demo-" + uuid.uuid4().hex[:8]

    # 1. 登记：epsilon 总预算 1.0，delta 总预算 1e-5
    show("登记数据集", *call(
        "POST", "/v1/datasets",
        {"dataset_id": dataset_id, "epsilon_limit": 1.0, "delta_limit": 1e-5},
    ))

    # 2. 第一次申请（纯 DP）
    rid1 = "req-" + uuid.uuid4().hex[:8]
    show("申请 #1 (eps=0.4, delta=0)", *call(
        "POST", f"/v1/datasets/{dataset_id}/requests",
        {"request_id": rid1, "query_type": "count", "epsilon": 0.4, "delta": 0.0},
    ))

    # 3. 幂等重放同一 request_id -> 200，replayed=true
    show("幂等重放 #1", *call(
        "POST", f"/v1/datasets/{dataset_id}/requests",
        {"request_id": rid1, "query_type": "count", "epsilon": 0.4, "delta": 0.0},
    ))

    # 4. 同 ID 不同参数 -> 409
    show("冲突 payload", *call(
        "POST", f"/v1/datasets/{dataset_id}/requests",
        {"request_id": rid1, "query_type": "sum", "epsilon": 0.9, "delta": 0.0},
    ))

    # 5. 第二次申请
    rid2 = "req-" + uuid.uuid4().hex[:8]
    show("申请 #2 (eps=0.5, delta=0)", *call(
        "POST", f"/v1/datasets/{dataset_id}/requests",
        {"request_id": rid2, "query_type": "histogram", "epsilon": 0.5, "delta": 0.0},
    ))

    # 6. 第三次申请会超预算（0.4+0.5+0.4 = 1.3 > 1.0）-> 400 rejected
    rid3 = "req-" + uuid.uuid4().hex[:8]
    show("申请 #3 (eps=0.4) 预期被拒", *call(
        "POST", f"/v1/datasets/{dataset_id}/requests",
        {"request_id": rid3, "query_type": "mean", "epsilon": 0.4, "delta": 0.0},
    ))

    # 7. 预算明细
    show("预算明细", *call("GET", f"/v1/datasets/{dataset_id}/budget"))

    # 8. 并发原子性：10 个线程同时申请 eps=0.3（纯 DP，预算 2.0）
    cid = "conc-" + uuid.uuid4().hex[:8]
    call("POST", "/v1/datasets",
         {"dataset_id": cid, "epsilon_limit": 2.0, "delta_limit": 0.0})
    results: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        st, body = call(
            "POST", f"/v1/datasets/{cid}/requests",
            {"request_id": f"c-{i}", "query_type": "gaussian",
             "epsilon": 0.3, "delta": 0.0},
        )
        with lock:
            results.append((st, body))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    accepted = sum(1 for st, _ in results if st == 201)
    rejected = sum(1 for st, _ in results if st == 400)
    print(f"\n== 并发 10x eps=0.3，预算 eps=2.0，耗时 {time.time()-t0:.2f}s ==")
    print(f"accepted={accepted}（预期 6），rejected={rejected}（预期 4）")
    assert accepted == 6 and rejected == 4, "并发原子判定结果不符合预期"
    print("OK：组合消耗 6*0.3=1.8 <= 2.0，第 7 个起被原子拒绝")

    st, summary = call("GET", f"/v1/datasets/{cid}/budget")
    print(f"并发后已消耗 epsilon={summary['epsilon_consumed']:.4f}，"
          f"剩余={summary['epsilon_remaining']:.4f}")


if __name__ == "__main__":
    main()
