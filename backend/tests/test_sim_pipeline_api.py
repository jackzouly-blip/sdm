"""编排路由的 HTTP 层测试：节点类型声明、定义 CRUD、运行与外部回流、属主隔离。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.session import issue_token
from app.sim.db import SimDB
from app.sim.engine import PipelineEngine
from app.sim.nodes import register_builtin_node_types
from app.sim.pipeline_router import router as pipeline_router


@pytest.fixture(autouse=True)
def restrict_admins(monkeypatch):
    """不配置白名单时 is_admin() 对所有人为真，隔离用例会假通过。"""
    from app import config

    monkeypatch.setenv("HPC_ADMIN_USERS", "root")
    monkeypatch.setattr(config, "_settings", None)
    yield
    monkeypatch.setattr(config, "_settings", None)


@pytest.fixture
def client(tmp_path):
    register_builtin_node_types()
    app = FastAPI()
    app.include_router(pipeline_router)
    db = SimDB(str(tmp_path / "sim.db"))
    app.state.sim_db = db
    app.state.pipeline_engine = PipelineEngine(db)
    with TestClient(app) as c:
        yield c
    db.close()


def hdr(user: str) -> dict:
    return {"Authorization": f"Bearer {issue_token(user)}"}


def simple_doc():
    return {
        "nodes": [
            {"id": "m", "type": "manual.confirm", "params": {"prompt": "请确认"}},
            {"id": "v", "type": "internal.validate_config", "params": {}},
        ],
        "edges": [{"from": "m", "to": "v"}],
    }


def test_node_types_expose_schema_for_editor(client):
    """画布完全靠这个接口生成节点面板与参数表单。"""
    types = client.get("/sim/node-types", headers=hdr("u")).json()
    ids = {t["type_id"] for t in types}
    assert {"internal.validate_config", "internal.export_deck",
            "capability.invoke", "hpc.submit", "manual.confirm"} <= ids

    cap = next(t for t in types if t["type_id"] == "capability.invoke")
    assert cap["category"] == "capability"
    assert "capability_id" in cap["params_schema"]["properties"]
    assert cap["params_schema"]["required"] == ["capability_id"]


def test_pipeline_crud_and_version_bump(client):
    r = client.post("/sim/pipelines", json={"name": "流程", "doc": simple_doc()},
                    headers=hdr("u"))
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["version"] == 1
    assert len(r.json()["doc"]["nodes"]) == 2

    r = client.patch(f"/sim/pipelines/{pid}",
                     json={"doc": {"nodes": [{"id": "m", "type": "manual.confirm"}],
                                   "edges": []}},
                     headers=hdr("u"))
    assert r.json()["version"] == 2, "文档变更应使版本自增"

    # 仅改名不应影响版本
    r = client.patch(f"/sim/pipelines/{pid}", json={"name": "改名"}, headers=hdr("u"))
    assert r.json()["version"] == 2 and r.json()["name"] == "改名"

    assert client.delete(f"/sim/pipelines/{pid}", headers=hdr("u")).status_code == 204


def test_invalid_dag_is_rejected_with_reason(client):
    cyclic = {
        "nodes": [{"id": "a", "type": "manual.confirm"},
                  {"id": "b", "type": "manual.confirm"}],
        "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
    }
    r = client.post("/sim/pipelines", json={"name": "环", "doc": cyclic}, headers=hdr("u"))
    assert r.status_code == 400
    assert "环" in r.json()["detail"]


def test_validate_endpoint_does_not_persist(client):
    r = client.post("/sim/pipelines/validate", json={"doc": simple_doc()},
                    headers=hdr("u"))
    assert r.json() == {"ok": True}

    bad = {"nodes": [{"id": "a", "type": "no.such.type"}], "edges": []}
    r = client.post("/sim/pipelines/validate", json={"doc": bad}, headers=hdr("u"))
    assert r.json()["ok"] is False and "未注册" in r.json()["error"]

    assert client.get("/sim/pipelines", headers=hdr("u")).json() == []


def test_run_parks_at_manual_node_then_completes(client):
    pid = client.post("/sim/pipelines", json={"name": "流程", "doc": simple_doc()},
                      headers=hdr("u")).json()["id"]

    run = client.post(f"/sim/pipelines/{pid}/runs", json={}, headers=hdr("u")).json()
    assert run["status"] == "waiting"
    by_id = {n["node_id"]: n for n in run["nodes"]}
    assert by_id["m"]["status"] == "waiting"
    assert by_id["m"]["wait_hint"] == "请确认"
    assert by_id["v"]["status"] == "pending"

    # 人工确认后继续推进；下游因未绑定工况而失败——这正是期望的行为
    r = client.post(f"/sim/runs/{run['id']}/nodes/m/complete",
                    json={"outputs": {"confirmed_by": "u"}}, headers=hdr("u"))
    assert r.status_code == 200
    by_id = {n["node_id"]: n for n in r.json()["nodes"]}
    assert by_id["m"]["status"] == "done"
    assert by_id["v"]["status"] == "failed"
    assert "未绑定工况" in by_id["v"]["error_message"]


def test_completing_twice_is_rejected(client):
    pid = client.post("/sim/pipelines", json={"name": "流程", "doc": simple_doc()},
                      headers=hdr("u")).json()["id"]
    rid = client.post(f"/sim/pipelines/{pid}/runs", json={}, headers=hdr("u")).json()["id"]

    assert client.post(f"/sim/runs/{rid}/nodes/m/complete", json={},
                       headers=hdr("u")).status_code == 200
    r = client.post(f"/sim/runs/{rid}/nodes/m/complete", json={}, headers=hdr("u"))
    assert r.status_code == 400 and "不是等待中" in r.json()["detail"]


def test_claim_registers_external_ref(client):
    """HPC 作业号 / 能力作业号登记后，回流时才能定位到节点。"""
    doc = {"nodes": [{"id": "j", "type": "hpc.submit", "params": {}}], "edges": []}
    pid = client.post("/sim/pipelines", json={"name": "求解", "doc": doc},
                      headers=hdr("u")).json()["id"]
    rid = client.post(f"/sim/pipelines/{pid}/runs", json={}, headers=hdr("u")).json()["id"]

    r = client.post(f"/sim/runs/{rid}/nodes/j/claim",
                    json={"external_ref": "2434.hpcmaster"}, headers=hdr("u"))
    node = next(n for n in r.json()["nodes"] if n["node_id"] == "j")
    assert node["external_ref"] == "2434.hpcmaster"
    assert node["status"] == "waiting"


def test_capability_nodes_appear_in_browser_broker_queue(client):
    """能力节点要出现在待办里，浏览器才知道该替它调本机 vektor3d。"""
    doc = {"nodes": [{"id": "mesh", "type": "capability.invoke",
                      "params": {"capability_id": "mesh.generate",
                                 "input": {"quality": "fine"}}}],
           "edges": []}
    pid = client.post("/sim/pipelines", json={"name": "网格", "doc": doc},
                      headers=hdr("u")).json()["id"]
    client.post(f"/sim/pipelines/{pid}/runs", json={}, headers=hdr("u"))

    todo = client.get("/sim/pending-capability-nodes", headers=hdr("u")).json()
    assert len(todo) == 1
    assert todo[0]["node_id"] == "mesh"
    assert todo[0]["params"]["capability_id"] == "mesh.generate"
    assert todo[0]["params"]["input"] == {"quality": "fine"}

    # 他人的待办里不该出现，避免替别人执行
    assert client.get("/sim/pending-capability-nodes", headers=hdr("other")).json() == []


def test_cancel_run(client):
    pid = client.post("/sim/pipelines", json={"name": "流程", "doc": simple_doc()},
                      headers=hdr("u")).json()["id"]
    rid = client.post(f"/sim/pipelines/{pid}/runs", json={}, headers=hdr("u")).json()["id"]

    r = client.post(f"/sim/runs/{rid}/cancel", headers=hdr("u"))
    assert r.json()["status"] == "canceled"
    assert next(n for n in r.json()["nodes"] if n["node_id"] == "v")["status"] == "skipped"


def test_owner_isolation(client):
    pid = client.post("/sim/pipelines", json={"name": "私有", "doc": simple_doc()},
                      headers=hdr("u")).json()["id"]
    rid = client.post(f"/sim/pipelines/{pid}/runs", json={}, headers=hdr("u")).json()["id"]

    assert client.get("/sim/pipelines", headers=hdr("other")).json() == []
    assert client.get(f"/sim/pipelines/{pid}", headers=hdr("other")).status_code == 404
    assert client.get("/sim/runs", headers=hdr("other")).json() == []
    assert client.get(f"/sim/runs/{rid}", headers=hdr("other")).status_code == 404
    assert client.post(f"/sim/runs/{rid}/nodes/m/complete", json={},
                       headers=hdr("other")).status_code == 404

    # 管理员跨用户可见
    assert len(client.get("/sim/pipelines", headers=hdr("root")).json()) == 1
