"""端到端冒烟测试：拓扑分层、乱序拒绝、失败回滚与断点重试。"""
import pytest
from fastapi.testclient import TestClient

from app.database import engine
from app.main import app
from app.models import Base


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c


DIAMOND_NODES = [
    {"key": "db", "depends_on": []},
    {"key": "auth", "depends_on": []},
    {"key": "api", "depends_on": ["db", "auth"]},
    {"key": "gateway", "depends_on": ["api"]},
]


def _create(client, version="v1", nodes=DIAMOND_NODES, note=None):
    return client.post(
        "/api/releases",
        json={"version": version, "note": note, "nodes": nodes},
    )


def _report(client, rid, key, status, message=None):
    body = {"status": status}
    if message is not None:
        body["message"] = message
    return client.post(f"/api/releases/{rid}/nodes/{key}/report", json=body)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_release_generates_topological_plan(client):
    r = _create(client)
    assert r.status_code == 201
    data = r.json()
    assert data["version"] == "v1"
    assert data["status"] == "running"
    assert data["total_layers"] == 3
    assert data["plan"] == [
        {"layer": 0, "nodes": ["auth", "db"]},
        {"layer": 1, "nodes": ["api"]},
        {"layer": 2, "nodes": ["gateway"]},
    ]
    assert data["progress"] == {
        "pending": 4,
        "success": 0,
        "failed": 0,
        "rolled_back": 0,
    }


def test_duplicate_version_rejected(client):
    assert _create(client, "v1").status_code == 201
    r = _create(client, "v1")
    assert r.status_code == 409
    assert r.json()["error"] == "version_exists"


def test_unknown_dependency_rejected(client):
    r = _create(
        client,
        "bad-dep",
        [{"key": "a", "depends_on": ["ghost"]}],
    )
    assert r.status_code == 422
    assert r.json()["error"] == "unknown_dependency"


def test_cyclic_dependency_rejected_with_path(client):
    r = _create(
        client,
        "cycle",
        [
            {"key": "a", "depends_on": ["b"]},
            {"key": "b", "depends_on": ["c"]},
            {"key": "c", "depends_on": ["a"]},
        ],
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "cyclic_dependency"
    # 环路径首尾相接
    assert body["cycle_path"][0] == body["cycle_path"][-1]
    assert set(body["cycle_path"]) == {"a", "b", "c"}


def test_self_dependency_rejected(client):
    r = _create(client, "self", [{"key": "a", "depends_on": ["a"]}])
    assert r.status_code == 422
    assert r.json()["error"] == "cyclic_dependency"


def test_out_of_order_report_rejected(client):
    rid = _create(client).json()["id"]
    # gateway 依赖链均未成功：越级回报
    r = _report(client, rid, "gateway", "success")
    assert r.status_code == 422
    assert r.json()["error"] == "out_of_order"

    # api 的依赖尚未全部成功
    r = _report(client, rid, "api", "success")
    assert r.status_code == 422


def test_cross_version_report_rejected(client):
    rid = _create(client, "v1").json()["id"]
    other = _create(
        client,
        "v2",
        [
            {"key": "x", "depends_on": []},
            {"key": "y", "depends_on": ["x"]},
        ],
    ).json()["id"]
    # v1 的节点不能挂到 v2 批次上回报（跨版本乱序）
    r = _report(client, other, "db", "success")
    assert r.status_code == 422
    assert r.json()["error"] == "node_not_in_release"

    # v2 自己的节点越级回报仍受层级校验
    assert _report(client, other, "y", "success").status_code == 422

    # 未知 release 返回 404
    assert client.post("/api/releases/999/nodes/db/report",
                       json={"status": "success"}).status_code == 404
    # rid 已在上面正常存在
    assert rid != other


def test_success_flow_completes_release(client):
    rid = _create(client).json()["id"]
    for key in ("db", "auth", "api", "gateway"):
        r = _report(client, rid, key, "success")
        assert r.status_code == 200, r.text

    final = r.json()
    assert final["release_status"] == "completed"
    assert final["success_count"] == 4

    # 结束后拒绝继续回报
    dup = _report(client, rid, "db", "success")
    assert dup.status_code == 409
    assert dup.json()["error"] == "release_closed"


def test_duplicate_node_report_rejected(client):
    rid = _create(client).json()["id"]
    assert _report(client, rid, "db", "success").status_code == 200
    r = _report(client, rid, "db", "success")
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_report"


def test_failure_rolls_back_only_succeeded_and_returns_breakpoint(client):
    rid = _create(client, "v-fail").json()["id"]

    assert _report(client, rid, "db", "success").status_code == 200
    assert _report(client, rid, "auth", "success").status_code == 200

    r = _report(client, rid, "api", "failed", message="健康检查失败")
    assert r.status_code == 200
    body = r.json()
    assert body["release_status"] == "rolled_back"
    assert body["node_status"] == "failed"
    assert body["rolled_back_count"] == 2

    rb = body["rollback"]
    # 仅回滚本批次已成功节点，按层级逆序；未执行的 gateway 保持 pending
    assert set(rb["rolled_back_nodes"]) == {"db", "auth"}
    assert rb["failed_node"] == "api"
    assert rb["failed_layer"] == 1
    assert rb["retry"]["path"] == f"/api/releases/{rid}/retry"
    assert rb["retry"]["resume_from_node"] == "api"

    detail = client.get(f"/api/releases/{rid}").json()
    statuses = {n["key"]: n["status"] for n in detail["nodes"]}
    assert statuses == {
        "db": "rolled_back",
        "auth": "rolled_back",
        "api": "failed",
        "gateway": "pending",
    }
    assert detail["status"] == "rolled_back"
    assert detail["failed_node_key"] == "api"

    # 回滚后不允许直接回报
    assert _report(client, rid, "auth", "success").status_code == 409


def test_retry_resets_and_can_complete(client):
    rid = _create(client, "v-retry").json()["id"]
    _report(client, rid, "db", "success")
    _report(client, rid, "auth", "success")
    _report(client, rid, "api", "failed", message="boom")

    # 运行中的批次不允许重试
    other = _create(client, "v-running").json()["id"]
    assert client.post(f"/api/releases/{other}/retry").status_code == 409

    r = client.post(f"/api/releases/{rid}/retry")
    assert r.status_code == 200
    body = r.json()
    assert body["release_status"] == "running"
    assert body["retry_from"]["resume_from_layer"] == 1
    assert body["retry_from"]["resume_from_node"] == "api"
    assert set(body["reset_nodes"]) == {"db", "auth", "api", "gateway"}

    detail = client.get(f"/api/releases/{rid}").json()
    assert all(n["status"] == "pending" for n in detail["nodes"])

    # 重新执行：db/auth 必须重新逐级回报（断点要求有序）
    assert _report(client, rid, "gateway", "success").status_code == 422
    for key in ("db", "auth", "api", "gateway"):
        last = _report(client, rid, key, "success")
    assert last.json()["release_status"] == "completed"


def test_plan_endpoint(client):
    rid = _create(client, "v-plan").json()["id"]
    r = client.get(f"/api/releases/{rid}/plan")
    assert r.status_code == 200
    assert r.json()["total_layers"] == 3
    assert len(r.json()["plan"]) == 3


def test_list_releases(client):
    _create(client, "v1")
    _create(client, "v2")
    r = client.get("/api/releases")
    assert r.status_code == 200
    versions = [item["version"] for item in r.json()]
    assert versions == ["v2", "v1"]  # 新批次在前
