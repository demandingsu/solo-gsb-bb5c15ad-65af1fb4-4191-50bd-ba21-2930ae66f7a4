"""Pydantic 请求 / 响应模型。"""
import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------- 请求 ----------------


class NodeIn(BaseModel):
    key: str = Field(..., description="节点唯一标识（同批次内唯一）")
    name: Optional[str] = Field(None, description="节点可读名称")
    depends_on: List[str] = Field(
        default_factory=list, description="该节点依赖的其他节点 key"
    )


class ReleaseCreate(BaseModel):
    version: str = Field(..., description="发布版本号，全局唯一")
    note: Optional[str] = Field(None, description="发布备注")
    nodes: List[NodeIn] = Field(..., min_length=1, description="服务节点及依赖关系")


class ReportIn(BaseModel):
    status: str = Field(..., description="执行结果：success / failed")
    message: Optional[str] = Field(None, description="附加信息（失败原因等）")


# ---------------- 响应 ----------------


class NodeOut(BaseModel):
    key: str
    name: Optional[str] = None
    layer: int
    order_index: int
    depends_on: List[str]
    status: str
    error_message: Optional[str] = None
    reported_at: Optional[datetime.datetime] = None
    rolled_back_at: Optional[datetime.datetime] = None


class PlanLayer(BaseModel):
    layer: int
    nodes: List[str]


class ReleaseOut(BaseModel):
    id: int
    version: str
    note: Optional[str] = None
    status: str
    total_layers: int
    failed_node_key: Optional[str] = None
    failed_message: Optional[str] = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    progress: Dict[str, int]
    nodes: List[NodeOut]
    plan: List[PlanLayer]


class ReleaseListItem(BaseModel):
    id: int
    version: str
    status: str
    total_layers: int
    failed_node_key: Optional[str] = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    progress: Dict[str, int]


class ReportOut(BaseModel):
    release_id: int
    version: str
    release_status: str
    node_key: str
    node_status: str
    success_count: int
    failed_count: int
    pending_count: int
    rolled_back_count: int
    rollback: Optional[Dict[str, Any]] = Field(
        None, description="失败时的回滚结果与可重试断点"
    )


class RetryOut(BaseModel):
    release_id: int
    version: str
    release_status: str
    reset_nodes: List[str]
    retry_from: Dict[str, Any]
    plan: List[PlanLayer]


class ErrorOut(BaseModel):
    error: str
    detail: Optional[str] = None
    cycle_path: Optional[List[str]] = None
