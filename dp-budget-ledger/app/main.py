"""FastAPI 入口：路由、依赖注入与异常映射。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import ledger
from .db import get_connection, init_db
from .models import (
    AnalysisRequest,
    BudgetSummary,
    DatasetInfo,
    DatasetRegister,
    Message,
    RequestDetail,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="差分隐私预算账本 API",
    description=(
        "基于 Rényi DP 的 epsilon/delta 预算账本：数据集登记、分析申请预留、"
        "request_id 幂等、并发原子预算判定、预算明细与剩余额度查询。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ledger.ConflictError)
async def _conflict_handler(_: Request, exc: ledger.ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ledger.NotFoundError)
async def _not_found_handler(_: Request, exc: ledger.NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": f"数据集不存在: {exc}"})


@app.exception_handler(ledger.DatasetExistsError)
async def _exists_handler(_: Request, exc: ledger.DatasetExistsError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": f"数据集已存在: {exc}"})


def _to_request_detail(row: dict, replayed: bool) -> RequestDetail:
    return RequestDetail(
        request_id=row["request_id"],
        dataset_id=row["dataset_id"],
        query_type=row["query_type"],
        epsilon=row["epsilon"],
        delta=row["delta"],
        status=row["status"],
        reason=row["reason"],
        created_at=row["created_at"],
        replayed=replayed,
    )


@app.get("/health", tags=["meta"], response_model=Message)
async def health() -> Message:
    return Message(detail="ok")


@app.post("/v1/datasets", tags=["datasets"], response_model=DatasetInfo, status_code=201)
async def register_dataset(
    payload: DatasetRegister,
    conn=Depends(get_connection),
) -> DatasetInfo:
    row = await ledger.register_dataset(conn, payload)
    return DatasetInfo(
        dataset_id=row["id"],
        epsilon_limit=row["epsilon_limit"],
        delta_limit=row["delta_limit"],
        created_at=row["created_at"],
    )


@app.get("/v1/datasets", tags=["datasets"], response_model=list[DatasetInfo])
async def list_datasets(conn=Depends(get_connection)) -> list[DatasetInfo]:
    rows = await ledger.list_datasets(conn)
    return [
        DatasetInfo(
            dataset_id=r["id"],
            epsilon_limit=r["epsilon_limit"],
            delta_limit=r["delta_limit"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@app.post(
    "/v1/datasets/{dataset_id}/requests",
    tags=["requests"],
    response_model=RequestDetail,
    responses={
        200: {"description": "重复 request_id，幂等重放原有结果"},
        201: {"description": "预留成功（accepted）"},
        400: {"description": "预算不足，申请被拒绝（rejected）"},
        409: {"description": "request_id 冲突（同 ID 不同参数）"},
    },
)
async def submit_request(
    dataset_id: str,
    payload: AnalysisRequest,
    conn=Depends(get_connection),
) -> RequestDetail:
    result = await ledger.reserve(conn, dataset_id, payload)
    status = result["status"]
    row = result["request"]

    if status == "accepted":
        detail = _to_request_detail(row, replayed=False)
        # 预留成功返回 201
        return JSONResponse(  # type: ignore[return-value]
            status_code=201, content=detail.model_dump()
        )
    if status == "rejected":
        # 原子拒绝：HTTP 400，同时落库 rejected 记录（重复提交仍幂等）
        detail = _to_request_detail(row, replayed=False)
        return JSONResponse(  # type: ignore[return-value]
            status_code=400, content=detail.model_dump()
        )

    # 幂等重放：保持与首次完全一致的语义状态码
    replayed = True
    detail = _to_request_detail(row, replayed=replayed)
    code = 200 if row["status"] == "accepted" else 400
    return JSONResponse(status_code=code, content=detail.model_dump())  # type: ignore[return-value]


@app.get(
    "/v1/datasets/{dataset_id}/requests/{request_id}",
    tags=["requests"],
    response_model=RequestDetail,
    responses={404: {}},
)
async def get_request(dataset_id: str, request_id: str, conn=Depends(get_connection)) -> RequestDetail:
    row = await ledger.get_request(conn, dataset_id, request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="request_id 不存在")
    return _to_request_detail(row, replayed=False)


@app.get(
    "/v1/datasets/{dataset_id}/requests",
    tags=["requests"],
    response_model=list[RequestDetail],
)
async def list_requests(dataset_id: str, conn=Depends(get_connection)) -> list[RequestDetail]:
    rows = await ledger.list_requests(conn, dataset_id)
    return [_to_request_detail(r, replayed=False) for r in rows]


@app.get(
    "/v1/datasets/{dataset_id}/budget",
    tags=["budget"],
    response_model=BudgetSummary,
)
async def get_budget(dataset_id: str, conn=Depends(get_connection)) -> BudgetSummary:
    summary = await ledger.budget_summary(conn, dataset_id)
    return BudgetSummary(**summary)
