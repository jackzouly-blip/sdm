"""几何文件上传 / 下载 / 轻量化回传测试。

这三个端点正是 vektor3d geometry.convert 能力要对接的接口
（见 docs/vektor3d-geometry-capability-contract.md）：
它从 download 拉源文件，转换后 POST 到 lightweight。
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
    """启用管理员白名单（定义见 conftest）：不配置的话隔离用例是假通过。"""


@pytest.fixture
def real_fs(monkeypatch, tmp_path, patch_or_stub):
    """把降权写入替换成直写：本测试关心的是端点逻辑，不是 setuid。"""

    def fake_write_file(user, parent, name, data, roots):
        target = os.path.join(parent, name)
        os.makedirs(parent, exist_ok=True)
        if os.path.exists(target):
            raise RuntimeError("目标已存在")  # 与 O_EXCL 行为一致
        with open(target, "wb") as f:
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


def hdr(user: str) -> dict:
    return {"Authorization": f"Bearer {issue_token(user)}"}


@pytest.fixture
def target(client, tmp_path):
    """建一个带 workdir 的项目与分析对象。"""
    wd = tmp_path / "work"
    wd.mkdir()
    pid = client.post("/sim/projects", json={"name": "P", "workdir": str(wd)},
                      headers=hdr("u")).json()["id"]
    tid = client.post(f"/sim/projects/{pid}/targets", json={"name": "座椅骨架"},
                      headers=hdr("u")).json()["id"]
    return {"pid": pid, "tid": tid, "workdir": str(wd)}


def upload(client, tid, name, data=b"ISO-10303-21;\n", user="u"):
    return client.post(
        f"/sim/targets/{tid}/geometries/upload",
        files={"file": (name, io.BytesIO(data), "application/octet-stream")},
        headers=hdr(user),
    )


# --- 上传 ---------------------------------------------------------------

def test_upload_creates_geometry_version(client, target):
    r = upload(client, target["tid"], "seat.stp")
    assert r.status_code == 201, r.text
    g = r.json()
    assert g["version_no"] == 1
    assert g["source_file"]["name"] == "seat.stp"
    assert g["source_file"]["size"] == 14
    # STEP 会顺带记入 step_file 列
    assert g["step_file"] == g["source_file"]["path"]
    assert os.path.isfile(g["source_file"]["path"])
    # 落在项目 workdir 下，与仿真的其它输入输出同处一盘
    assert g["source_file"]["path"].startswith(
        os.path.join(target["workdir"], "sdm_geometry", target["tid"]))


def test_same_filename_uploaded_twice_does_not_collide(client, target):
    """write_file 用 O_EXCL 拒绝覆盖，故每次上传必须落在独立子目录。"""
    a = upload(client, target["tid"], "seat.stp", b"AAA").json()
    b = upload(client, target["tid"], "seat.stp", b"BBB").json()

    assert a["version_no"] == 1 and b["version_no"] == 2
    assert a["source_file"]["path"] != b["source_file"]["path"]
    assert open(a["source_file"]["path"], "rb").read() == b"AAA"
    assert open(b["source_file"]["path"], "rb").read() == b"BBB"


def test_catia_upload_is_archived_without_lightweight(client, target):
    """CAD 原生格式此时只归档；轻量化要等 vektor3d 能力。"""
    g = upload(client, target["tid"], "seat.CATPart", b"\x00CATIA").json()
    assert g["lightweight_file"] is None
    assert g["step_file"] is None


def test_upload_requires_project_workdir(client, tmp_path):
    pid = client.post("/sim/projects", json={"name": "无工作目录"},
                      headers=hdr("u")).json()["id"]
    tid = client.post(f"/sim/projects/{pid}/targets", json={"name": "x"},
                      headers=hdr("u")).json()["id"]
    r = upload(client, tid, "seat.stp")
    assert r.status_code == 400
    assert "工作目录" in r.json()["detail"]


def test_upload_rejects_other_users(client, target):
    assert upload(client, target["tid"], "seat.stp", user="intruder").status_code == 404


def test_path_traversal_in_filename_is_neutralised(client, target):
    """文件名里的路径成分必须被剥掉，不能写到目标目录之外。"""
    g = upload(client, target["tid"], "../../evil.stp").json()
    assert os.path.basename(g["source_file"]["path"]) == "evil.stp"
    assert g["source_file"]["path"].startswith(target["workdir"])


# --- 下载（vektor3d 拉源文件走这里）-------------------------------------

def test_download_returns_source_file(client, target):
    g = upload(client, target["tid"], "seat.stp", b"ISO-10303-21;").json()
    r = client.get(f"/sim/geometries/{g['id']}/download", headers=hdr("u"))
    assert r.status_code == 200
    assert r.content == b"ISO-10303-21;"


def test_download_rejects_other_users(client, target):
    g = upload(client, target["tid"], "seat.stp").json()
    assert client.get(f"/sim/geometries/{g['id']}/download",
                      headers=hdr("intruder")).status_code == 404


# --- 轻量化回传（vektor3d 转换完推这里）---------------------------------

def test_lightweight_roundtrip(client, target):
    g = upload(client, target["tid"], "seat.stp").json()
    glb = b"glTF\x02\x00\x00\x00fake"

    r = client.post(
        f"/sim/geometries/{g['id']}/lightweight",
        files={"file": ("seat.glb", io.BytesIO(glb), "model/gltf-binary")},
        data={"meta": json.dumps({"triangleCount": 120000, "partCount": 37,
                                  "unit": "mm"})},
        headers=hdr("u"),
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["lightweight_file"].endswith("seat.glb")
    assert out["topo_summary"]["triangleCount"] == 120000
    assert out["topo_summary"]["partCount"] == 37
    assert out["topo_summary"]["lightweight_bytes"] == len(glb)
    # 产物与源文件同处一目录，便于一并归档/清理
    assert os.path.dirname(out["lightweight_file"]) == os.path.dirname(
        out["source_file"]["path"])

    r = client.get(f"/sim/geometries/{g['id']}/lightweight", headers=hdr("u"))
    assert r.status_code == 200 and r.content == glb


def test_lightweight_rejects_private_formats(client, target):
    """必须是 glTF/GLB —— 私有格式(.3dix)浏览器渲染不了，这是契约的核心一条。"""
    g = upload(client, target["tid"], "seat.stp").json()
    r = client.post(
        f"/sim/geometries/{g['id']}/lightweight",
        files={"file": ("seat.3dix", io.BytesIO(b"x"), "application/octet-stream")},
        headers=hdr("u"),
    )
    assert r.status_code == 400
    assert "glTF/GLB" in r.json()["detail"]


def test_lightweight_download_404_before_conversion(client, target):
    g = upload(client, target["tid"], "seat.stp").json()
    r = client.get(f"/sim/geometries/{g['id']}/lightweight", headers=hdr("u"))
    assert r.status_code == 404
    assert "尚无轻量化产物" in r.json()["detail"]


def test_geometry_list_reflects_uploads(client, target):
    upload(client, target["tid"], "a.stp")
    upload(client, target["tid"], "b.stp")
    rows = client.get(f"/sim/targets/{target['tid']}/geometries",
                      headers=hdr("u")).json()
    assert [r["version_no"] for r in rows] == [1, 2]
