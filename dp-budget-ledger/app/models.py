"""请求/响应数据模型与校验。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

_QUERY_TYPES = {"count", "sum", "mean", "histogram", "variance", "gaussian", "laplace", "custom"}


class DatasetRegister(BaseModel):
    dataset_id: str = Field(..., min_length=1, max_length=200, description="数据集唯一标识")
    epsilon_limit: float = Field(..., strict=True, description="epsilon 总预算上限")
    delta_limit: float = Field(..., strict=True, description="delta 总预算上限，允许 0（纯 DP）")

    @field_validator("epsilon_limit")
    @classmethod
    def _eps_nonneg(cls, v: float) -> float:
        if v < 0 or v != v:
            raise ValueError("epsilon_limit 必须为非负有限数")
        return v

    @field_validator("delta_limit")
    @classmethod
    def _delta_range(cls, v: float) -> float:
        if v < 0 or v > 1 or v != v:
            raise ValueError("delta_limit 必须在 [0, 1] 范围内")
        return v


class AnalysisRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=200, description="客户端幂等键")
    query_type: str = Field(..., min_length=1, max_length=100, description="查询类型，如 count/gaussian/...")
    epsilon: float = Field(..., strict=True)
    delta: float = Field(..., strict=True)

    @field_validator("epsilon")
    @classmethod
    def _eps_nonneg(cls, v: float) -> float:
        if v < 0 or v != v:
            raise ValueError("epsilon 必须为非负有限数")
        return v

    @field_validator("delta")
    @classmethod
    def _delta_range(cls, v: float) -> float:
        if v < 0 or v > 1 or v != v:
            raise ValueError("delta 必须在 [0, 1] 范围内")
        return v


class RequestDetail(BaseModel):
    request_id: str
    dataset_id: str
    query_type: str
    epsilon: float
    delta: float
    status: Literal["accepted", "rejected"]
    reason: Optional[str] = None
    created_at: str
    replayed: bool = False


class RdpPoint(BaseModel):
    order: float
    epsilon: float


class BudgetSummary(BaseModel):
    dataset_id: str
    epsilon_limit: float
    delta_limit: float
    epsilon_consumed: float
    delta_consumed: float
    epsilon_remaining: float
    delta_remaining: float
    accepted_requests: int
    rejected_requests: int
    rdp_curve: list[RdpPoint]


class DatasetInfo(BaseModel):
    dataset_id: str
    epsilon_limit: float
    delta_limit: float
    created_at: str


class Message(BaseModel):
    detail: str
