"""SDM 仿真路由的 HTTP 层测试，重点是属主隔离。

不挂载完整 app：真实 lifespan 依赖 PBS / PAM / geteuid，与本层无关。
这里只装配 sim 路由并注入内存库，测的是路由自身的权限与语义。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.session import issue_token
from app.sim.db import SimDB
from app.sim.router import router as sim_router


@pytest.fixture(autouse=True)
def _admins(restrict_admins):
    """启用管理员白名单（定义见 conftest）：不配置的话隔离用例是假通过。"""


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    app.include_router(sim_router)
    app.state.sim_db = SimDB(str(tmp_path / "sim.db"))
    with TestClient(app) as c:
        yield c
    app.state.sim_db.close()


def hdr(user: str) -> dict:
    return {"Authorization": f"Bearer {issue_token(user)}"}


def test_project_lifecycle(client):
    r = client.post("/sim/projects", json={"name": "整椅碰撞", "default_solver": "ls-dyna"},
                    headers=hdr("user07"))
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["owner"] == "user07"

    r = client.get("/sim/projects", headers=hdr("user07"))
    assert [p["id"] for p in r.json()] == [pid]
    assert r.json()[0]["stats"] == {"targets": 0, "subjects": 0, "jobs": 0}

    r = client.patch(f"/sim/projects/{pid}", json={"description": "改了"},
                     headers=hdr("user07"))
    assert r.json()["description"] == "改了"

    assert client.delete(f"/sim/projects/{pid}", headers=hdr("user07")).status_code == 204
    assert client.get(f"/sim/projects/{pid}", headers=hdr("user07")).status_code == 404


def test_owner_isolation_hides_other_users_projects(client):
    pid = client.post("/sim/projects", json={"name": "私有"},
                      headers=hdr("user07")).json()["id"]

    # 他人列表里看不到
    assert client.get("/sim/projects", headers=hdr("intruder")).json() == []
    # 直接按 id 取也应 404 而非 403：不泄露"该项目存在"
    assert client.get(f"/sim/projects/{pid}", headers=hdr("intruder")).status_code == 404
    assert client.patch(f"/sim/projects/{pid}", json={"name": "x"},
                        headers=hdr("intruder")).status_code == 404
    assert client.delete(f"/sim/projects/{pid}", headers=hdr("intruder")).status_code == 404


def test_child_entities_inherit_project_ownership(client):
    """下级实体不各自实现权限，一律由所属项目的 owner 决定。"""
    pid = client.post("/sim/projects", json={"name": "私有"},
                      headers=hdr("user07")).json()["id"]
    tid = client.post(f"/sim/projects/{pid}/targets", json={"name": "骨架"},
                      headers=hdr("user07")).json()["id"]
    sid = client.post(f"/sim/projects/{pid}/subjects",
                      json={"name": "正碰", "subject_type": "crash",
                            "solver_type": "ls-dyna"},
                      headers=hdr("user07")).json()["id"]

    for path in (f"/sim/projects/{pid}/targets", f"/sim/projects/{pid}/subjects",
                 f"/sim/projects/{pid}/jobs", f"/sim/projects/{pid}/results",
                 f"/sim/subjects/{sid}", f"/sim/subjects/{sid}/jobs",
                 f"/sim/targets/{tid}/geometries"):
        assert client.get(path, headers=hdr("intruder")).status_code == 404, path

    assert client.delete(f"/sim/targets/{tid}", headers=hdr("intruder")).status_code == 404
    assert client.delete(f"/sim/subjects/{sid}", headers=hdr("intruder")).status_code == 404


def test_admin_sees_all_projects(client):
    """管理员跨用户可见，与现有 jobs 列表的语义一致。"""
    client.post("/sim/projects", json={"name": "A"}, headers=hdr("user07"))
    client.post("/sim/projects", json={"name": "B"}, headers=hdr("user08"))

    assert len(client.get("/sim/projects", headers=hdr("user07")).json()) == 1
    assert len(client.get("/sim/projects", headers=hdr("root")).json()) == 2


def test_unauthenticated_is_rejected(client):
    assert client.get("/sim/projects").status_code in (401, 403)


def test_geometry_mesh_chain(client):
    pid = client.post("/sim/projects", json={"name": "P"}, headers=hdr("u")).json()["id"]
    tid = client.post(f"/sim/projects/{pid}/targets", json={"name": "骨架"},
                      headers=hdr("u")).json()["id"]

    g = client.post(f"/sim/targets/{tid}/geometries",
                    json={"source_type": "upload", "step_file": "/data/a.step"},
                    headers=hdr("u")).json()
    assert g["version_no"] == 1

    # 一期网格由人工上传登记；vektor3d 能力就绪后由编排节点写同一接口
    m = client.post(f"/sim/geometries/{g['id']}/meshes",
                    json={"mesh_type": "shell", "mesh_engine": "manual",
                          "mesh_file": "/data/a.k"},
                    headers=hdr("u")).json()
    assert m["version_no"] == 1 and m["mesh_engine"] == "manual"

    assert len(client.get(f"/sim/geometries/{g['id']}/meshes",
                          headers=hdr("u")).json()) == 1


def test_job_is_registered_but_not_submitted(client):
    """建作业只落编排侧记录，不触发 PBS 提交——hpc_jobid 必须为空。"""
    pid = client.post("/sim/projects", json={"name": "P"}, headers=hdr("u")).json()["id"]
    sid = client.post(f"/sim/projects/{pid}/subjects",
                      json={"name": "正碰", "subject_type": "crash",
                            "solver_type": "ls-dyna"},
                      headers=hdr("u")).json()["id"]

    j = client.post(f"/sim/subjects/{sid}/jobs", json={"submit_mode": "pbs"},
                    headers=hdr("u")).json()
    assert j["status"] == "draft"
    assert j["hpc_jobid"] is None

    rows = client.get(f"/sim/projects/{pid}/jobs", headers=hdr("u")).json()
    assert len(rows) == 1 and rows[0]["subject_name"] == "正碰"


def test_json_columns_are_returned_parsed(client):
    """*_json 列以对象返回，前端不必二次解析。"""
    t = client.post("/sim/templates",
                    json={"name": "正碰模板", "subject_type": "crash",
                          "solver_type": "ls-dyna",
                          "schema": {"velocity": {"type": "number"}},
                          "validation_rules": {"velocity": {"max": 120}}},
                    headers=hdr("u")).json()
    assert t["schema"] == {"velocity": {"type": "number"}}
    assert t["validation_rules"] == {"velocity": {"max": 120}}
    assert t["export_mapping"] is None


def test_builtin_template_delete_is_rejected(client, tmp_path):
    db = client.app.state.sim_db
    builtin = db.create_template("内置", "crash", "ls-dyna", is_builtin=True)
    assert client.delete(f"/sim/templates/{builtin}", headers=hdr("u")).status_code == 400
