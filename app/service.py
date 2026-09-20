"""核心业务逻辑：拓扑分层、循环检测、回报处理、回滚与断点重试。"""
import datetime
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from . import models

# 状态常量
RELEASE_RUNNING = "running"
RELEASE_COMPLETED = "completed"
RELEASE_ROLLED_BACK = "rolled_back"

NODE_PENDING = "pending"
NODE_SUCCESS = "success"
NODE_FAILED = "failed"
NODE_ROLLED_BACK = "rolled_back"

_TERMINAL_RELEASE_STATUS = {RELEASE_COMPLETED, RELEASE_ROLLED_BACK}


class DomainError(Exception):
    """业务错误，携带 HTTP 状态码与错误码。"""

    def __init__(
        self,
        detail: str,
        status_code: int = 400,
        code: str = "bad_request",
        extra: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.code = code
        self.extra = extra or {}


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


# ---------------------------------------------------------------- 图计算


def compute_layers(
    keys: List[str], deps: Dict[str, List[str]]
) -> Tuple[Dict[str, int], int]:
    """Kahn 拓扑排序并按层级分层。

    layer[node] = max(layer[dep]) + 1，无依赖的节点为第 0 层。
    同一层内没有依赖关系，可并行执行；层间严格按序执行。
    返回 (节点 -> 层级, 总层数)；存在循环时抛出 DomainError。
    """
    indegree = {k: 0 for k in keys}
    dependents: Dict[str, List[str]] = defaultdict(list)
    for k in keys:
        for dep in deps.get(k, []):
            indegree[k] += 1
            dependents[dep].append(k)

    # 用排序列表 / 排序取值，保证结果确定
    ready = deque(sorted(k for k in keys if indegree[k] == 0))
    layer: Dict[str, int] = {}

    while ready:
        node = ready.popleft()
        layer[node] = max(
            (layer[d] for d in deps[node] if d in layer), default=-1
        ) + 1
        for child in dependents[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                # 保持确定性插入
                ready.append(child)
                tmp = sorted(ready)
                ready = deque(tmp)

    if len(layer) != len(keys):
        cycle = _find_cycle_path(keys, deps)
        raise DomainError(
            "检测到循环依赖，无法生成执行计划",
            status_code=422,
            code="cyclic_dependency",
            extra={"cycle_path": cycle},
        )

    total_layers = max(layer.values()) + 1
    return layer, total_layers


def _find_cycle_path(keys: List[str], deps: Dict[str, List[str]]) -> List[str]:
    """在存在循环的图中用迭代 DFS 找出一条环路径。"""
    key_set = set(keys)
    color: Dict[str, int] = {k: 0 for k in keys}  # 0 未访问 / 1 在栈中 / 2 完成

    for start in sorted(keys):
        if color[start] != 0:
            continue
        stack: List[Tuple[str, int]] = [(start, 0)]
        path: List[str] = []
        while stack:
            node, idx = stack[-1]
            if idx == 0:
                if color[node] == 0:
                    color[node] = 1
                    path.append(node)
            neighbors = sorted(d for d in deps.get(node, []) if d in key_set)
            if idx < len(neighbors):
                stack[-1] = (node, idx + 1)
                nxt = neighbors[idx]
                if color[nxt] == 0:
                    stack.append((nxt, 0))
                elif color[nxt] == 1:
                    # 找到回到栈中节点的边，截取成环
                    pos = path.index(nxt)
                    return path[pos:] + [nxt]
            else:
                color[node] = 2
                if path and path[-1] == node:
                    path.pop()
                stack.pop()
    return []


# ---------------------------------------------------------------- 创建发布


def create_release(db: Session, payload: "Any") -> models.Release:
    raw_nodes = payload.nodes

    # 1) 节点 key 唯一性校验
    keys = [n.key for n in raw_nodes]
    if len(set(keys)) != len(keys):
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        raise DomainError(
            f"节点 key 在同一批次内重复: {', '.join(dupes)}",
            status_code=422,
            code="duplicate_node",
        )

    key_set = set(keys)
    deps: Dict[str, List[str]] = {}
    for n in raw_nodes:
        bad = [d for d in n.depends_on if d not in key_set]
        if bad:
            raise DomainError(
                f"节点 {n.key} 依赖了批次中不存在的节点: {', '.join(bad)}",
                status_code=422,
                code="unknown_dependency",
            )
        # 去重并保持稳定顺序
        deps[n.key] = sorted(set(n.depends_on))

    # 2) 自依赖直接视为循环
    self_deps = [k for k in keys if k in deps[k]]
    if self_deps:
        raise DomainError(
            f"节点不能依赖自身: {', '.join(self_deps)}",
            status_code=422,
            code="cyclic_dependency",
            extra={"cycle_path": [self_deps[0], self_deps[0]]},
        )

    # 3) 版本号唯一
    exists = (
        db.query(models.Release)
        .filter(models.Release.version == payload.version)
        .first()
    )
    if exists:
        raise DomainError(
            f"版本号已存在: {payload.version}",
            status_code=409,
            code="version_exists",
        )

    # 4) 拓扑分层（含循环检测）
    layer_of, total_layers = compute_layers(keys, deps)

    # 5) 同层内按 key 排序，得到确定性执行顺序
    ordered = sorted(keys, key=lambda k: (layer_of[k], k))

    release = models.Release(
        version=payload.version,
        note=payload.note,
        status=RELEASE_RUNNING,
        total_layers=total_layers,
    )
    db.add(release)
    db.flush()  # 拿到 release.id

    name_map = {n.key: n.name for n in raw_nodes}
    for idx, key in enumerate(ordered):
        db.add(
            models.Node(
                release_id=release.id,
                node_key=key,
                name=name_map.get(key),
                layer=layer_of[key],
                order_index=idx,
                depends_on=deps[key],
                status=NODE_PENDING,
            )
        )

    edge_seen = set()
    for key in ordered:
        for dep in deps[key]:
            pair = (key, dep)
            if pair in edge_seen:
                continue
            edge_seen.add(pair)
            db.add(
                models.Edge(
                    release_id=release.id,
                    node_key=key,
                    depends_on_key=dep,
                )
            )

    db.commit()
    db.refresh(release)
    return release


# ---------------------------------------------------------------- 序列化


def _progress(nodes: List[models.Node]) -> Dict[str, int]:
    prog = {NODE_PENDING: 0, NODE_SUCCESS: 0, NODE_FAILED: 0, NODE_ROLLED_BACK: 0}
    for n in nodes:
        prog[n.status] += 1
    return {
        "pending": prog[NODE_PENDING],
        "success": prog[NODE_SUCCESS],
        "failed": prog[NODE_FAILED],
        "rolled_back": prog[NODE_ROLLED_BACK],
    }


def serialize_node(n: models.Node) -> Dict[str, Any]:
    return {
        "key": n.node_key,
        "name": n.name,
        "layer": n.layer,
        "order_index": n.order_index,
        "depends_on": list(n.depends_on or []),
        "status": n.status,
        "error_message": n.error_message,
        "reported_at": n.reported_at,
        "rolled_back_at": n.rolled_back_at,
    }


def _plan(nodes: List[models.Node]) -> List[Dict[str, Any]]:
    layers: Dict[int, List[str]] = defaultdict(list)
    for n in sorted(nodes, key=lambda x: x.order_index):
        layers[n.layer].append(n.node_key)
    return [
        {"layer": lvl, "nodes": layers[lvl]}
        for lvl in sorted(layers)
    ]


def serialize_release(release: models.Release) -> Dict[str, Any]:
    nodes = sorted(release.nodes, key=lambda x: x.order_index)
    return {
        "id": release.id,
        "version": release.version,
        "note": release.note,
        "status": release.status,
        "total_layers": release.total_layers,
        "failed_node_key": release.failed_node_key,
        "failed_message": release.failed_message,
        "created_at": release.created_at,
        "updated_at": release.updated_at,
        "progress": _progress(nodes),
        "nodes": [serialize_node(n) for n in nodes],
        "plan": _plan(nodes),
    }


def serialize_list_item(release: models.Release) -> Dict[str, Any]:
    nodes = sorted(release.nodes, key=lambda x: x.order_index)
    return {
        "id": release.id,
        "version": release.version,
        "status": release.status,
        "total_layers": release.total_layers,
        "failed_node_key": release.failed_node_key,
        "created_at": release.created_at,
        "updated_at": release.updated_at,
        "progress": _progress(nodes),
    }


def get_release_or_404(db: Session, release_id: int) -> models.Release:
    release = db.get(models.Release, release_id)
    if release is None:
        raise DomainError(
            f"发布批次不存在: id={release_id}", status_code=404, code="not_found"
        )
    return release


# ---------------------------------------------------------------- 回报处理


def submit_report(
    db: Session,
    release: models.Release,
    node_key: str,
    status: str,
    message: Optional[str],
) -> Dict[str, Any]:
    """接收节点执行回报。返回响应数据（失败时带 rollback 信息）。"""
    if status not in (NODE_SUCCESS, NODE_FAILED):
        raise DomainError(
            f"非法回报状态: {status}（仅支持 success / failed）",
            status_code=422,
            code="invalid_status",
        )

    # 1) 批次已结束：拒绝回报（含对其他版本批次的乱序回报场景由调用方路由拦截）
    if release.status in _TERMINAL_RELEASE_STATUS:
        raise DomainError(
            f"发布批次已结束（{release.status}），不再接受回报",
            status_code=409,
            code="release_closed",
        )

    nodes_by_key = {n.node_key: n for n in release.nodes}
    node = nodes_by_key.get(node_key)
    if node is None:
        raise DomainError(
            f"节点不属于该发布批次: {node_key}（版本 {release.version}）",
            status_code=422,
            code="node_not_in_release",
        )

    # 2) 重复回报
    if node.status in (NODE_SUCCESS, NODE_FAILED, NODE_ROLLED_BACK):
        raise DomainError(
            f"节点 {node_key} 已回报过（{node.status}），禁止重复回报",
            status_code=409,
            code="duplicate_report",
        )

    # 3) 跨版本乱序 / 越级回报：依赖必须全部成功后才允许回报该节点
    pending_deps = [
        dep
        for dep in (node.depends_on or [])
        if nodes_by_key[dep].status != NODE_SUCCESS
    ]
    if pending_deps:
        raise DomainError(
            f"节点 {node_key} 的依赖尚未全部成功，属于越级/乱序回报；"
            f"未完成依赖: {', '.join(pending_deps)}",
            status_code=422,
            code="out_of_order",
        )

    now = _utcnow()
    node.reported_at = now
    rollback_info: Optional[Dict[str, Any]] = None

    if status == NODE_SUCCESS:
        node.status = NODE_SUCCESS
        node.error_message = None

        all_done = all(
            n.status in (NODE_SUCCESS, NODE_FAILED, NODE_ROLLED_BACK)
            for n in release.nodes
        )
        # 存在失败节点时不会走到“全部成功”，这里仅在没有失败时完成
        if all_done and not any(n.status != NODE_SUCCESS for n in release.nodes):
            release.status = RELEASE_COMPLETED
    else:
        # ---- 失败：仅回滚本批次“已成功”的节点（按执行层级逆序） ----
        node.status = NODE_FAILED
        node.error_message = message
        release.failed_node_key = node.node_key
        release.failed_message = message

        succeeded = sorted(
            (n for n in release.nodes if n.status == NODE_SUCCESS),
            key=lambda x: (-x.layer, -x.order_index),
        )
        rolled_back_keys: List[str] = []
        for n in succeeded:
            n.status = NODE_ROLLED_BACK
            n.rolled_back_at = _utcnow()
            rolled_back_keys.append(n.node_key)

        release.status = RELEASE_ROLLED_BACK

        rollback_info = {
            "rolled_back_nodes": rolled_back_keys,
            "rolled_back_count": len(rolled_back_keys),
            "failed_node": node.node_key,
            "failed_layer": node.layer,
            "message": message,
            "retry": {
                "method": "POST",
                "path": f"/api/releases/{release.id}/retry",
                "description": "重置本批次后从失败断点重新执行",
                "resume_from_layer": node.layer,
                "resume_from_node": node.node_key,
            },
        }

    db.commit()
    db.refresh(release)

    nodes = sorted(release.nodes, key=lambda x: x.order_index)
    prog = _progress(nodes)
    return {
        "release_id": release.id,
        "version": release.version,
        "release_status": release.status,
        "node_key": node.node_key,
        "node_status": node.status,
        "success_count": prog["success"],
        "failed_count": prog["failed"],
        "pending_count": prog["pending"],
        "rolled_back_count": prog["rolled_back"],
        "rollback": rollback_info,
    }


# ---------------------------------------------------------------- 断点重试


def retry_release(db: Session, release: models.Release) -> Dict[str, Any]:
    """失败回滚后重置本批次：回滚节点/失败节点/未执行节点全部回到 pending，
    保留版本号与执行计划，从失败节点所在层重新开始。"""
    if release.status != RELEASE_ROLLED_BACK:
        raise DomainError(
            f"仅已回滚（rolled_back）的批次允许重试，当前状态: {release.status}",
            status_code=409,
            code="retry_not_allowed",
        )

    failed_node = next(
        (n for n in release.nodes if n.node_key == release.failed_node_key), None
    )
    resume_layer = failed_node.layer if failed_node else 0
    resume_node = failed_node.node_key if failed_node else None

    # 回滚后批次内不存在 success 节点：失败节点与被回滚节点全部重置，
    # 未执行的 pending 节点保持 pending，整份计划重新逐级执行
    reset_nodes: List[str] = []
    for n in sorted(release.nodes, key=lambda x: x.order_index):
        reset_nodes.append(n.node_key)
        n.status = NODE_PENDING
        n.error_message = None
        n.reported_at = None
        n.rolled_back_at = None

    release.status = RELEASE_RUNNING
    release.failed_node_key = None
    release.failed_message = None

    db.commit()
    db.refresh(release)

    nodes = sorted(release.nodes, key=lambda x: x.order_index)
    return {
        "release_id": release.id,
        "version": release.version,
        "release_status": release.status,
        "reset_nodes": reset_nodes,
        "retry_from": {
            "resume_from_layer": resume_layer,
            "resume_from_node": resume_node,
            "description": (
                f"从第 {resume_layer} 层断点节点 {resume_node} 重新执行；"
                "其下层节点已回滚并重置为 pending"
            ),
        },
        "plan": _plan(nodes),
    }
