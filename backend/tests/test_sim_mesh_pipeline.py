"""网格能力的文件通道测试（票据 / 产物回传 / 检出检入 / 几何分析回写）。

对接的是 vektor3d 的 mesh.* 能力族（见 docs/vektor3d-geometry-capability-contract.md
2.3~2.11）：它拿网格票据从 download 拉源文件或从 artifact 拉 .ansa 正本，
产物 POST 回 artifact。

守的关键性质：
- .ansa 是正本、solver/preview/report 是派生物，四类分槽存放不互相覆盖；
- 网格票据只在本 gid 生效，换 gid 403、换接口 401（默认拒绝的 scope 语义）；
- 检出是排他的——两个人各改各的、谁后检入谁覆盖是静默丢工作。
"""
import io
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.session import issue_token
from app.sim.db import SimDB
from app.sim.router import router as sim_router


@pytest.fixture(autouse=True)
def _admins(restrict_admins):
    """启用管理员白名单(定义见 conftest):不配置的话隔离用例是假通过。"""


@pytest.fixture
def real_fs(monkeypatch, tmp_path, patch_or_stub):
    def fake_write_file(user, parent, name, data, roots):
        target = os.path.join(parent, name)
        os.makedirs(parent, exist_ok=True)
        with open(target, "wb") as f:      # 网格产物允许覆盖(同版本反复检入)
            f.write(data)
        return {"path": target}

    patch_or_stub("app.fs.browser", {"write_file": fake_write_file})
    monkeypatch.setenv("HPC_FS_ROOTS", str(tmp_path))
    from app import config

    monkeypatch.setattr(config, "_settings", None)


@pytest.fixture
def client(tmp_path, real_fs):
    app = FastAPI()
    app.include_router(sim_router)
    db = SimDB(str(tmp_path / "sim.db"))
    app.state.sim_db = db
    with TestClient(app) as c:
        yield c
    db.close()


def hdr(user: str = "u") -> dict:
    return {"Authorization": f"Bearer {issue_token(user)}"}


@pytest.fixture
def geometry(client, tmp_path):
    """建项目 → 分析对象 → 上传一份 STEP,返回 gid。"""
    wd = tmp_path / "work"
    wd.mkdir()
    pid = client.post("/sim/projects", json={"name": "P", "workdir": str(wd)},
                      headers=hdr()).json()["id"]
    tid = client.post(f"/sim/projects/{pid}/targets", json={"name": "相机支架"},
                      headers=hdr()).json()["id"]
    g = client.post(
        f"/sim/targets/{tid}/geometries/upload",
        files={"file": ("bracket.stp", io.BytesIO(b"ISO-10303-21;\n"), "application/octet-stream")},
        headers=hdr(),
    ).json()
    return g["id"]


def make_mesh(client, gid, **kw):
    body = {"mesh_type": "surface", "mesh_engine": "vektor3d:mesh.generate",
            "status": "generating", **kw}
    r = client.post(f"/sim/geometries/{gid}/meshes", json=body, headers=hdr())
    assert r.status_code == 201, r.text
    return r.json()


def artifact(client, gid, mid, kind, name, data=b"x", meta=None, token=None):
    files = {"file": (name, io.BytesIO(data), "application/octet-stream")}
    form = {"meta": json.dumps(meta)} if meta else None
    headers = {"Authorization": f"Bearer {token}"} if token else hdr()
    return client.post(f"/sim/geometries/{gid}/meshes/{mid}/artifact/{kind}",
                       files=files, data=form, headers=headers)


# --- 登记与状态 ----------------------------------------------------------

def test_generating_mesh_is_registered_before_artifacts_arrive(client, geometry):
    """能力作业动辄几十分钟,必须先落一行让页面能显示进度。"""
    m = make_mesh(client, geometry, part_filter="相机支架改3")
    assert m["status"] == "generating"
    assert m["part_filter"] == "相机支架改3"
    assert m["ansa_file"] is None and m["solver_file"] is None


def test_failed_job_writes_reason_into_quality(client, geometry):
    m = make_mesh(client, geometry)
    r = client.patch(f"/sim/geometries/{geometry}/meshes/{m['id']}",
                     json={"status": "failed",
                           "quality": {"summary": "中面抽取超时"}},
                     headers=hdr())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed"
    assert r.json()["quality"]["summary"] == "中面抽取超时"


# --- 产物四类分槽 --------------------------------------------------------

def test_four_artifact_kinds_land_in_separate_slots(client, geometry):
    """.ansa 是正本、其余是派生物 —— 不能互相覆盖。"""
    m = make_mesh(client, geometry)
    mid = m["id"]
    assert artifact(client, geometry, mid, "ansa", "mesh.ansa", b"ANSA-DB").status_code == 200
    assert artifact(client, geometry, mid, "solver", "mesh.nas", b"GRID",
                    meta={"solverFormat": "nastran", "elementCount": 114301}).status_code == 200
    assert artifact(client, geometry, mid, "preview", "p.glb", b"glTF").status_code == 200
    assert artifact(client, geometry, mid, "report", "r.html", b"<html>").status_code == 200

    row = client.get(f"/sim/geometries/{geometry}/meshes", headers=hdr()).json()[0]
    assert row["ansa_file"] and row["solver_file"] and row["preview_file"] and row["report_file"]
    assert len({row["ansa_file"], row["solver_file"], row["preview_file"], row["report_file"]}) == 4
    assert row["solver_format"] == "nastran"
    # 主产物到位即视为可用,质量数据随 meta 落库
    assert row["status"] == "ready"
    assert row["quality"]["elementCount"] == 114301


def test_artifact_extension_is_enforced(client, geometry):
    """预览必须是 glTF/GLB —— 私有格式塞进来浏览器根本渲染不了。"""
    m = make_mesh(client, geometry)
    r = artifact(client, geometry, m["id"], "preview", "mesh.ansa", b"x")
    assert r.status_code == 400
    assert "glb" in r.text.lower()
    assert artifact(client, geometry, m["id"], "ansa", "x.nas", b"y").status_code == 400
    assert artifact(client, geometry, m["id"], "nope", "x.bin", b"y").status_code == 400


def test_artifact_download_roundtrip(client, geometry):
    m = make_mesh(client, geometry)
    artifact(client, geometry, m["id"], "solver", "mesh.nas", b"GRID 1")
    r = client.get(f"/sim/geometries/{geometry}/meshes/{m['id']}/artifact/solver",
                   headers=hdr())
    assert r.status_code == 200
    assert r.content == b"GRID 1"
    # 尚未回传的类型 404,而不是给个空文件
    assert client.get(f"/sim/geometries/{geometry}/meshes/{m['id']}/artifact/preview",
                      headers=hdr()).status_code == 404


# --- 票据 ---------------------------------------------------------------

def test_mesh_ticket_scope_is_bound_to_geometry(client, geometry, tmp_path):
    """票据只在本 gid 生效:换 gid 403、换别的接口 401。"""
    t = client.post(f"/sim/geometries/{geometry}/mesh-ticket", headers=hdr())
    assert t.status_code == 200, t.text
    tk = t.json()
    assert tk["source_path_suffix"].endswith(f"/geometries/{geometry}/download")
    assert tk["mesh_path_prefix"].endswith(f"/geometries/{geometry}/meshes")

    m = make_mesh(client, geometry)
    # ① 本 gid 的产物回传:放行
    assert artifact(client, geometry, m["id"], "ansa", "m.ansa", b"A",
                    token=tk["token"]).status_code == 200
    # ② 网格票据也能拉几何源文件(mesh.generate 的输入)
    r = client.get(f"/sim/geometries/{geometry}/download?token={tk['token']}")
    assert r.status_code == 200
    # ③ 换个 gid:403
    pid = client.post("/sim/projects", json={"name": "P2", "workdir": str(tmp_path / "w2")},
                      headers=hdr()).json()["id"]
    tid = client.post(f"/sim/projects/{pid}/targets", json={"name": "T2"},
                      headers=hdr()).json()["id"]
    other = client.post(
        f"/sim/targets/{tid}/geometries/upload",
        files={"file": ("b.stp", io.BytesIO(b"ISO"), "application/octet-stream")},
        headers=hdr(),
    ).json()["id"]
    r = client.get(f"/sim/geometries/{other}/download?token={tk['token']}")
    assert r.status_code == 403
    # ④ 拿票据去调别的接口(签新票据):401 —— 票据不能自我续签
    r = client.post(f"/sim/geometries/{geometry}/mesh-ticket",
                    headers={"Authorization": f"Bearer {tk['token']}"})
    assert r.status_code == 401


# --- 检出 / 检入 ---------------------------------------------------------

def test_checkout_is_exclusive(client, geometry):
    """两个人各改各的、谁后检入谁覆盖 —— 那是静默丢工作,必须拦。

    用管理员(root,见 conftest 白名单)扮演"另一个人":普通用户连别人的项目
    都看不见(404,不泄露存在性),能真正撞上占用的只有管理员这类跨属主视角。
    """
    m = make_mesh(client, geometry)
    mid = m["id"]
    # 没有 .ansa 正本时不能检出(ANSA 那边打不开)
    assert client.post(f"/sim/geometries/{geometry}/meshes/{mid}/checkout",
                       json={"checkout_id": "c1"}, headers=hdr()).status_code == 400

    artifact(client, geometry, mid, "ansa", "m.ansa", b"A")
    r = client.post(f"/sim/geometries/{geometry}/meshes/{mid}/checkout",
                    json={"checkout_id": "c1"}, headers=hdr("u"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "checked-out"
    assert r.json()["checkout_id"] == "c1"

    # 同一人重复检出放行(换台机器继续改)
    assert client.post(f"/sim/geometries/{geometry}/meshes/{mid}/checkout",
                       json={"checkout_id": "c2"}, headers=hdr("u")).status_code == 200
    # 管理员想插队改同一份:409,并告知被谁占着
    r = client.post(f"/sim/geometries/{geometry}/meshes/{mid}/checkout",
                    json={"checkout_id": "c3"}, headers=hdr("root"))
    assert r.status_code == 409, r.text
    assert "u" in r.json()["detail"]

    r = client.post(f"/sim/geometries/{geometry}/meshes/{mid}/checkin", headers=hdr("u"))
    assert r.status_code == 200
    assert r.json()["status"] == "ready" and r.json()["checkout_id"] is None
    # 释放后管理员可以接手
    assert client.post(f"/sim/geometries/{geometry}/meshes/{mid}/checkout",
                       json={"checkout_id": "c4"}, headers=hdr("root")).status_code == 200


# --- 几何分析回写 --------------------------------------------------------

def test_geometry_analysis_writeback(client, geometry):
    """mesh.inventory / mesh.classify 的结论落在几何版本上(网格生成前就该可见)。"""
    r = client.put(
        f"/sim/geometries/{geometry}/analysis",
        json={
            "part_inventory": [{"index": 0, "name": "相机支架改3", "faceCount": 255}],
            "mesh_strategy": {"summary": {"partCount": 2, "needsReviewCount": 0},
                              "parts": [{"partId": "p1", "meshType": "midsurface",
                                         "recommendedMinThickness": 2.18}]},
        },
        headers=hdr(),
    )
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["part_inventory"][0]["name"] == "相机支架改3"
    assert g["mesh_strategy"]["parts"][0]["recommendedMinThickness"] == 2.18
    # 只传一项时不覆盖另一项
    client.put(f"/sim/geometries/{geometry}/analysis",
               json={"mesh_strategy": {"summary": {"partCount": 3}}}, headers=hdr())
    g2 = client.get(f"/sim/geometries/{geometry}", headers=hdr()).json()
    assert g2["part_inventory"][0]["name"] == "相机支架改3"


# --- 隔离 ---------------------------------------------------------------

def test_other_user_cannot_touch_mesh(client, geometry):
    """非属主一律 404 —— 沿用 SDM 既有策略:不泄露资源是否存在。"""
    m = make_mesh(client, geometry)
    mid = m["id"]
    assert client.get(f"/sim/geometries/{geometry}/meshes",
                      headers=hdr("intruder")).status_code == 404
    assert artifact(client, geometry, mid, "ansa", "m.ansa", b"A").status_code == 200
    r = client.get(f"/sim/geometries/{geometry}/meshes/{mid}/artifact/ansa",
                   headers=hdr("intruder"))
    assert r.status_code == 404
    # 写侧同样挡住
    assert client.patch(f"/sim/geometries/{geometry}/meshes/{mid}",
                        json={"status": "ready"}, headers=hdr("intruder")).status_code == 404
