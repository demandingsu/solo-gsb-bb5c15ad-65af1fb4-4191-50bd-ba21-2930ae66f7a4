"""FastAPI 入口：依赖图发布回滚 API。"""
import threading
from contextlib import asynccontextmanager
from typing import Dict

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from . import service
from .database import get_db, init_db
from .schemas import (
    ErrorOut,
    ReleaseCreate,
    ReleaseListItem,
    ReleaseOut,
    ReportIn,
    ReportOut,
    RetryOut,
)
from .service import DomainError


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时完成 SQLite 建表初始化
    init_db()
    yield


app = FastAPI(
    title="依赖图发布回滚 API",
    description=(
        "客户端提交服务节点及依赖关系，创建带版本号的发布批次；"
        "系统按拓扑层级生成执行计划，接收节点成功/失败回报，"
        "失败时仅回滚本批次已成功节点并返回可重试断点。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# 按发布批次互斥：同一批次的回报/重试串行化，避免并发乱序
_locks_meta = threading.Lock()
_locks: Dict[int, threading.RLock] = {}


def _release_lock(release_id: int) -> threading.RLock:
    with _locks_meta:
        lk = _locks.get(release_id)
        if lk is None:
            lk = threading.RLock()
            _locks[release_id] = lk
        return lk


@app.exception_handler(DomainError)
def _domain_error_handler(request, exc: DomainError):  # noqa: ANN001
    content = {"error": exc.code, "detail": exc.detail}
    content.update(exc.extra)
    return JSONResponse(status_code=exc.status_code, content=content)


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "dependency-release-rollback-api",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/health",
    }


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.post(
    "/api/releases",
    response_model=ReleaseOut,
    status_code=201,
    tags=["releases"],
    responses={409: {"model": ErrorOut}, 422: {"model": ErrorOut}},
)
def create_release(payload: ReleaseCreate, db: Session = Depends(get_db)):
    release = service.create_release(db, payload)
    return service.serialize_release(release)


@app.get(
    "/api/releases",
    response_model=list[ReleaseListItem],
    tags=["releases"],
)
def list_releases(db: Session = Depends(get_db)):
    from . import models

    releases = db.query(models.Release).order_by(models.Release.id.desc()).all()
    return [service.serialize_list_item(r) for r in releases]


@app.get(
    "/api/releases/{release_id}",
    response_model=ReleaseOut,
    tags=["releases"],
    responses={404: {"model": ErrorOut}},
)
def get_release(release_id: int, db: Session = Depends(get_db)):
    release = service.get_release_or_404(db, release_id)
    return service.serialize_release(release)


@app.get(
    "/api/releases/{release_id}/plan",
    tags=["releases"],
    responses={404: {"model": ErrorOut}},
)
def get_plan(release_id: int, db: Session = Depends(get_db)):
    """查询发布执行计划（按拓扑层级）。"""
    release = service.get_release_or_404(db, release_id)
    return {
        "release_id": release.id,
        "version": release.version,
        "status": release.status,
        "total_layers": release.total_layers,
        "plan": service._plan(  # noqa: SLF001
            sorted(release.nodes, key=lambda x: x.order_index)
        ),
    }


@app.post(
    "/api/releases/{release_id}/nodes/{node_key}/report",
    response_model=ReportOut,
    tags=["reports"],
    responses={
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
        422: {"model": ErrorOut},
    },
)
def report_node(
    release_id: int,
    node_key: str,
    payload: ReportIn,
    db: Session = Depends(get_db),
):
    """接收某节点在指定版本批次上的成功/失败回报。

    - 依赖未全部成功即回报 → 拒绝（越级 / 乱序）
    - 向其他版本批次回报不属于它的节点 → 拒绝（跨版本乱序）
    - 失败 → 自动逆序回滚本批次已成功节点，返回断点与重试入口
    """
    release = service.get_release_or_404(db, release_id)
    with _release_lock(release_id):
        # 拿锁后重新读取，保证状态最新
        db.refresh(release)
        return service.submit_report(
            db, release, node_key, payload.status, payload.message
        )


@app.post(
    "/api/releases/{release_id}/retry",
    response_model=RetryOut,
    tags=["releases"],
    responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}},
)
def retry_release(release_id: int, db: Session = Depends(get_db)):
    """从失败断点重试：重置本批次后重新执行。"""
    release = service.get_release_or_404(db, release_id)
    with _release_lock(release_id):
        db.refresh(release)
        return service.retry_release(db, release)
