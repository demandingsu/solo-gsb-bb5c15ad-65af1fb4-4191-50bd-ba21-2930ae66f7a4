# 依赖图发布回滚 API（FastAPI + SQLite）

客户端提交服务节点及依赖关系，创建带**版本号**的发布批次；系统按
**拓扑层级**生成执行计划，接收每个节点的成功/失败回报；节点失败时
**仅回滚本批次已成功节点**并返回**可重试的断点**；自动拒绝循环依赖与
跨版本/越级乱序回报。提供发布状态与计划查询接口。

## 一条命令启动

```bash
docker compose up -d --build
```

启动后（首次启动自动完成依赖安装与 SQLite 建表初始化）：

| 用途 | 地址 |
| --- | --- |
| API 根地址 | http://localhost:8000 |
| 健康检查 | http://localhost:8000/health |
| Swagger 文档（可在线调试） | http://localhost:8000/docs |
| OpenAPI Schema | http://localhost:8000/openapi.json |

停止：`docker compose down`（保留数据卷）；清空数据：
`docker compose down -v`。

## 本地（非 Docker）运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 配置

均通过环境变量配置，均为可选项：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | Docker 内 `/app/data/release.db`；本地运行 `./data/release.db` | SQLite 数据库文件路径，启动时自动建表 |
| `PORT`（仅 compose 映射） | `8000` | 宿主机映射端口，修改 `docker-compose.yml` 的 `ports` 即可 |

Docker 部署下数据库保存在命名卷 `release-data` 中，容器重建不丢数据。

## 核心规则

1. **创建发布批次**：`version` 全局唯一（重复返回 `409`）；同批次节点 `key` 唯一；
   依赖只能指向批次内的节点。
2. **拓扑分层**：无依赖节点为第 0 层，其余节点层级为其依赖最大层级 + 1；
   同层节点无依赖关系、可并行，层间严格按序执行。
3. **循环依赖**：创建时检测，返回 `422 cyclic_dependency` 并附带一条环路径 `cycle_path`。
4. **节点回报**：`POST .../nodes/{key}/report`，body 为 `{"status": "success|failed", "message": "..."}`。
5. **乱序拒绝**：
   - 节点的依赖尚未全部成功就回报 → `422 out_of_order`（越级回报）；
   - 向其他版本批次回报不属于它的节点 → `422 node_not_in_release`（跨版本乱序）；
   - 批次已结束 / 节点已回报 → `409 release_closed` / `409 duplicate_report`。
6. **失败回滚**：任一节点失败，立即把**本批次已成功**的节点按执行层级逆序
   标记为 `rolled_back`，失败节点标记 `failed`，批次置为 `rolled_back`，
   响应中给出回滚清单与可重试断点（失败节点所在层）。
7. **断点重试**：`POST .../retry` 将批次重置为 `running`（计划与版本号不变），
   从失败节点所在层重新执行。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/releases` | 创建发布批次（提交节点与依赖，生成分层计划） |
| `GET` | `/api/releases` | 发布批次列表（含各状态计数） |
| `GET` | `/api/releases/{id}` | 查询发布状态（节点状态、进度、计划） |
| `GET` | `/api/releases/{id}/plan` | 仅查询拓扑执行计划 |
| `POST` | `/api/releases/{id}/nodes/{nodeKey}/report` | 上报节点成功/失败 |
| `POST` | `/api/releases/{id}/retry` | 失败后从断点重置重试 |

## 使用示例（curl）

### 1. 创建发布批次

依赖关系：`gateway` → `api` → `auth`、`db`（第 0 层：`auth`、`db`）。

```bash
curl -s -X POST http://localhost:8000/api/releases \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "v2026.09.20-1",
    "note": "首次发布",
    "nodes": [
      {"key": "db",      "name": "数据库",  "depends_on": []},
      {"key": "auth",    "name": "认证服务", "depends_on": []},
      {"key": "api",     "name": "业务API", "depends_on": ["db", "auth"]},
      {"key": "gateway", "name": "网关",    "depends_on": ["api"]}
    ]
  }'
```

返回中的 `plan` 形如：

```json
{"plan": [
  {"layer": 0, "nodes": ["auth", "db"]},
  {"layer": 1, "nodes": ["api"]},
  {"layer": 2, "nodes": ["gateway"]}
]}
```

### 2. 按层级回报

```bash
# 第 0 层（顺序无关）
curl -s -X POST http://localhost:8000/api/releases/1/nodes/db/report   \
  -H 'Content-Type: application/json' -d '{"status":"success"}'
curl -s -X POST http://localhost:8000/api/releases/1/nodes/auth/report \
  -H 'Content-Type: application/json' -d '{"status":"success"}'

# 此时回报 api 合法（依赖均成功）；若先回报 gateway 则被拒绝 out_of_order
curl -s -X POST http://localhost:8000/api/releases/1/nodes/api/report  \
  -H 'Content-Type: application/json' -d '{"status":"failed","message":"健康检查未通过"}'
```

失败响应示例（已自动回滚本批次成功节点）：

```json
{
  "release_status": "rolled_back",
  "node_key": "api",
  "node_status": "failed",
  "rolled_back_count": 2,
  "rollback": {
    "rolled_back_nodes": ["auth", "db"],
    "failed_node": "api",
    "failed_layer": 1,
    "retry": {
      "method": "POST",
      "path": "/api/releases/1/retry",
      "resume_from_layer": 1,
      "resume_from_node": "api"
    }
  }
}
```

### 3. 断点重试

```bash
curl -s -X POST http://localhost:8000/api/releases/1/retry
```

批次重新变为 `running`，所有节点回到 `pending`，断点指向第 1 层 `api`，
按原计划重新逐级回报即可。

### 4. 查询状态 / 计划

```bash
curl -s http://localhost:8000/api/releases/1
curl -s http://localhost:8000/api/releases/1/plan
curl -s http://localhost:8000/api/releases
```

### 5. 循环依赖被拒绝

```bash
curl -i -X POST http://localhost:8000/api/releases \
  -H 'Content-Type: application/json' \
  -d '{"version":"bad","nodes":[
        {"key":"a","depends_on":["b"]},
        {"key":"b","depends_on":["c"]},
        {"key":"c","depends_on":["a"]}]}'
# HTTP 422，body: {"error":"cyclic_dependency","cycle_path":["a","b","c","a"], ...}
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```
