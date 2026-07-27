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


def test_upload_works_without_manually_set_workdir(client, tmp_path, monkeypatch):
    """不再要求人工填工作目录：按系统配置派生。

    这条曾断言"没设 workdir 就 400"——那是让每个新项目都必须先去手填一个集群
    绝对路径的年代。现在 workdir 由 HPC_SIM_WORKDIR_ROOT 派生,该报错不复存在。
    """
    monkeypatch.setenv("HPC_SIM_WORKDIR_ROOT", str(tmp_path / "simroot"))
    from app import config
    monkeypatch.setattr(config, "_settings", None)

    pid = client.post("/sim/projects", json={"name": "没手填工作目录"},
                      headers=hdr("u")).json()["id"]
    tid = client.post(f"/sim/projects/{pid}/targets", json={"name": "x"},
                      headers=hdr("u")).json()["id"]
    r = upload(client, tid, "seat.stp")
    assert r.status_code == 201, r.text
    assert r.json()["source_file"]["path"].startswith(str(tmp_path / "simroot"))


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


# --- 转换票据（浏览器发起、vektor3d 执行的那条链）-----------------------
#
# vektor3d 在用户桌面上、只监听 localhost，集群调不通它；所以调用由浏览器发起，
# 文件由 vektor3d 直连本服务收发。它需要一份凭据——但不能是用户的会话 JWT。

def test_convert_ticket_is_scoped_to_one_geometry(client, target):
    g = upload(client, target["tid"], "seat.CATPart", b"\x00CATIA").json()
    r = client.post(f"/sim/geometries/{g['id']}/convert-ticket", headers=hdr("u"))
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["gid"] == g["id"]
    assert t["source_name"] == "seat.CATPart"
    # URL 由前端用 window.location.origin 拼；后端只给路径后缀
    assert t["source_path_suffix"] == f"/sim/geometries/{g['id']}/download"
    assert t["upload_path_suffix"] == f"/sim/geometries/{g['id']}/lightweight"

    # 票据能拉源文件、能回传产物——这正是 vektor3d 要做的两件事
    tok = {"Authorization": f"Bearer {t['token']}"}
    assert client.get(f"/sim/geometries/{g['id']}/download", headers=tok).content == b"\x00CATIA"
    r = client.post(
        f"/sim/geometries/{g['id']}/lightweight",
        files={"file": ("seat.glb", io.BytesIO(b"glTF\x02\x00\x00\x00"), "model/gltf-binary")},
        data={"meta": json.dumps({"partCount": 3})},
        headers=tok,
    )
    assert r.status_code == 200, r.text
    assert r.json()["topo_summary"]["partCount"] == 3


def test_convert_ticket_cannot_touch_other_geometries(client, target):
    """绑定 gid 是关键：票据换个 gid 就该被拒，否则等于给了整个账号。"""
    a = upload(client, target["tid"], "a.stp").json()
    b = upload(client, target["tid"], "b.stp").json()
    t = client.post(f"/sim/geometries/{a['id']}/convert-ticket", headers=hdr("u")).json()
    tok = {"Authorization": f"Bearer {t['token']}"}

    assert client.get(f"/sim/geometries/{a['id']}/download", headers=tok).status_code == 200
    r = client.get(f"/sim/geometries/{b['id']}/download", headers=tok)
    assert r.status_code == 403
    assert "不匹配" in r.json()["detail"]


def test_convert_ticket_is_useless_elsewhere(client, target):
    """受限令牌默认被所有普通接口拒绝——否则漏掉一个接口就成了通行证。"""
    g = upload(client, target["tid"], "seat.stp").json()
    t = client.post(f"/sim/geometries/{g['id']}/convert-ticket", headers=hdr("u")).json()
    tok = {"Authorization": f"Bearer {t['token']}"}

    assert client.get("/sim/projects", headers=tok).status_code == 401
    assert client.get(f"/sim/targets/{target['tid']}/geometries", headers=tok).status_code == 401
    # 也不能拿票据再签一张新票据（否则有效期形同虚设）
    r = client.post(f"/sim/geometries/{g['id']}/convert-ticket", headers=tok)
    assert r.status_code == 401
    assert "仅限" in r.json()["detail"]


def test_convert_ticket_expires(client, target, monkeypatch):
    """票据是短期的：过期后 vektor3d 再拿它拉源文件就该 401。

    过期用"签发时把时钟拨回去"来构造,而不是拨快校验端的时钟——
    exp 由 PyJWT 用它自己的时钟校验,拨 session 的时钟对校验没有影响。
    """
    from app.auth import session

    g = upload(client, target["tid"], "seat.stp").json()
    real_time = session.time.time
    monkeypatch.setattr(session.time, "time", lambda: real_time() - 3600)
    t = client.post(f"/sim/geometries/{g['id']}/convert-ticket",
                    params={"ttl_seconds": 60}, headers=hdr("u")).json()
    monkeypatch.undo()

    r = client.get(f"/sim/geometries/{g['id']}/download",
                   headers={"Authorization": f"Bearer {t['token']}"})
    assert r.status_code == 401
    assert "过期" in r.json()["detail"]


def test_convert_ticket_rejects_other_users(client, target):
    g = upload(client, target["tid"], "seat.stp").json()
    r = client.post(f"/sim/geometries/{g['id']}/convert-ticket", headers=hdr("intruder"))
    assert r.status_code == 404


# --- 工作目录：来自系统配置，不该要求人工填 -----------------------------

def test_new_project_gets_workdir_from_system_config(client, tmp_path, monkeypatch):
    """建项目就把 workdir 定下来——等到导入数模才报"未设置"是最差的顺序。"""
    monkeypatch.setenv("HPC_SIM_WORKDIR_ROOT", str(tmp_path / "simroot"))
    from app import config
    monkeypatch.setattr(config, "_settings", None)

    p = client.post("/sim/projects", json={"name": "无需填工作目录"},
                    headers=hdr("u")).json()
    assert p["workdir"] == os.path.join(str(tmp_path / "simroot"), "u", p["id"])


def test_legacy_project_without_workdir_is_backfilled_on_use(client, tmp_path, monkeypatch):
    """老项目 workdir 为空:首次用到时按同一规则派生并回写,不需要数据迁移。"""
    monkeypatch.setenv("HPC_SIM_WORKDIR_ROOT", str(tmp_path / "simroot"))
    from app import config
    monkeypatch.setattr(config, "_settings", None)

    pid = client.post("/sim/projects", json={"name": "老项目"}, headers=hdr("u")).json()["id"]
    client.app.state.sim_db.update_project(pid, workdir=None)  # 还原成历史状态
    tid = client.post(f"/sim/projects/{pid}/targets", json={"name": "座椅"},
                      headers=hdr("u")).json()["id"]

    g = upload(client, tid, "seat.stp").json()
    expected = os.path.join(str(tmp_path / "simroot"), "u", pid, "sdm_geometry", tid)
    assert g["source_file"]["path"].startswith(expected)
    # 已回写,后续不再重算
    assert client.get(f"/sim/projects/{pid}", headers=hdr("u")).json()["workdir"] \
        == os.path.join(str(tmp_path / "simroot"), "u", pid)


def test_explicit_workdir_still_wins(client, tmp_path, monkeypatch):
    """个别项目要指向既有分析目录时,显式值不能被派生值覆盖。"""
    monkeypatch.setenv("HPC_SIM_WORKDIR_ROOT", str(tmp_path / "simroot"))
    from app import config
    monkeypatch.setattr(config, "_settings", None)

    custom = str(tmp_path / "existing-analysis")
    p = client.post("/sim/projects", json={"name": "指定目录", "workdir": custom},
                    headers=hdr("u")).json()
    assert p["workdir"] == custom


def test_lightweight_download_accepts_query_token(client, target):
    """three.js 的 GLTFLoader 拿 URL 直接 fetch,设不了 Authorization 头,
    令牌只能走 query——这条曾经因为管理员依赖只读请求头而 401,渲染全线打不开。"""
    g = upload(client, target["tid"], "seat.stp").json()
    glb = b"glTF\x02\x00\x00\x00fake"
    client.post(
        f"/sim/geometries/{g['id']}/lightweight",
        files={"file": ("seat.glb", io.BytesIO(glb), "model/gltf-binary")},
        headers=hdr("u"),
    )
    from app.auth.session import issue_token

    r = client.get(f"/sim/geometries/{g['id']}/lightweight",
                   params={"token": issue_token("u")})  # 刻意不带 Authorization 头
    assert r.status_code == 200, r.text
    assert r.content == glb


def test_lightweight_download_still_rejects_other_users(client, target):
    from app.auth.session import issue_token

    g = upload(client, target["tid"], "seat.stp").json()
    r = client.get(f"/sim/geometries/{g['id']}/lightweight",
                   params={"token": issue_token("intruder")})
    assert r.status_code == 404
