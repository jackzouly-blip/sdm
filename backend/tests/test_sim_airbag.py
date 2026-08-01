"""气囊平面图 → deck 的落库链路。

重点验三件事：
  1. **票据只认 IGES 输入**——气囊网格化的输入是平面展开图，拿一份 STEP
     去签票据必须当场拒绝，而不是等 vektor3d 跑到一半才失败；
  2. **deck 登记成新的几何版本**（而非挂在平面图那条记录下），并留下溯源；
  3. 票据绑 gid，换个 gid 就失效。
"""
import io
import json
import os
import sys

import pytest

# 写盘要经 fs.browser → privilege.actas，后者 import fcntl（Unix 专有）。
# 与 test_fs / test_packaging 同因，在 Windows 开发机上跳过；生产是 CentOS，照常覆盖。
needs_posix = pytest.mark.skipif(sys.platform == "win32",
                                 reason="写盘路径依赖 fcntl（Unix 专有）")
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.session import issue_token
from app.sim.db import SimDB
from app.sim.router import router as sim_router


@pytest.fixture()
def client(tmp_path, restrict_admins):
    db = SimDB(str(tmp_path / "t.db"))
    # 源文件落在 tmp 沙盒里，写盘走真实的 fs.browser（以项目属主身份）
    root = tmp_path / "work"
    root.mkdir()
    from app.config import get_settings
    old_roots = get_settings().fs_roots
    get_settings().fs_roots = str(root)

    app = FastAPI()
    app.state.sim_db = db
    app.include_router(sim_router)
    with TestClient(app) as c:
        c.db = db
        c.root = root
        yield c
    db.close()
    get_settings().fs_roots = old_roots


def _hdr(user="root"):
    return {"Authorization": f"Bearer {issue_token(user)}"}


def _mk_geometry(client, name="5P-BAG.igs", content=b"IGES stub\n"):
    """建项目/对象/几何版本，源文件真实落盘。"""
    db = client.db
    pid = db.create_project("气囊", owner="root")
    tid = db.create_target(pid, name="BAG", target_type="assembly")
    p = client.root / name
    p.write_bytes(content)
    gid = db.add_geometry(tid, source_type="airbag_flat",
                          source_file={"name": name, "size": len(content), "path": str(p)})
    return pid, tid, gid


def test_ticket_rejects_non_iges(client):
    """输入不是 IGES 就当场拒绝，不要等能力跑到一半才发现。"""
    _pid, _tid, gid = _mk_geometry(client, name="part.step")
    r = client.post(f"/sim/geometries/{gid}/airbag-ticket", headers=_hdr())
    assert r.status_code == 400
    assert "IGES" in r.text


def test_ticket_and_source_fetch(client):
    _pid, _tid, gid = _mk_geometry(client)
    r = client.post(f"/sim/geometries/{gid}/airbag-ticket", headers=_hdr())
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["source_path_suffix"] == f"/sim/geometries/{gid}/download"
    assert t["deck_path_suffix"] == f"/sim/geometries/{gid}/airbag-deck"

    # 只带票据（vektor3d 的处境）也能拉到平面图
    d = client.get(f"/sim/geometries/{gid}/download",
                   headers={"Authorization": f"Bearer {t['token']}"})
    assert d.status_code == 200
    assert d.content.startswith(b"IGES")


def test_ticket_bound_to_gid(client):
    _pid, tid, gid_a = _mk_geometry(client, name="a.igs")
    p = client.root / "b.igs"
    p.write_bytes(b"IGES b\n")
    gid_b = client.db.add_geometry(tid, source_type="airbag_flat",
                                   source_file={"name": "b.igs", "size": 7, "path": str(p)})
    t = client.post(f"/sim/geometries/{gid_a}/airbag-ticket", headers=_hdr()).json()
    r = client.get(f"/sim/geometries/{gid_b}/download",
                   headers={"Authorization": f"Bearer {t['token']}"})
    assert r.status_code in (401, 403)


@needs_posix
def test_deck_lands_as_new_geometry_version(client):
    """回传的 deck 应当是**新的几何版本**，且带溯源指回平面图。"""
    _pid, tid, gid = _mk_geometry(client)
    t = client.post(f"/sim/geometries/{gid}/airbag-ticket", headers=_hdr()).json()

    deck = b"*KEYWORD\n*NODE\n*END\n"
    r = client.post(
        f"/sim/geometries/{gid}/airbag-deck?convert=false",
        files={"file": ("airbag.k", io.BytesIO(deck), "text/plain")},
        data={"meta": json.dumps({"nodes": 83137, "elements": 161268,
                                  "volumeL": 1.578, "nonmanifold": 0})},
        headers={"Authorization": f"Bearer {t['token']}"},
    )
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["id"] != gid, "deck 不该覆盖平面图那条记录"
    assert new["source_type"] == "deck"
    assert new["derived_from_id"] == gid
    assert new["derived_by"] == "vektor3d:mesh.airbag.generate"
    assert new["version_no"] > 1, "应当是同一 target 下的新版本"
    # 能力回传的度量随几何一起落库，页面据此显示"体积/非流形边"
    assert new["topo_summary"]["nonmanifold"] == 0
    assert new["topo_summary"]["nodes"] == 83137

    # 文件真的写下去了
    path = new["source_file"]["path"]
    assert os.path.isfile(path)
    assert open(path, "rb").read() == deck

    # 两个版本并存于同一个 target
    geoms = client.db.list_geometries(tid)
    assert {g["source_type"] for g in geoms} == {"airbag_flat", "deck"}


@needs_posix
def test_deck_rejects_bad_extension(client):
    _pid, _tid, gid = _mk_geometry(client)
    t = client.post(f"/sim/geometries/{gid}/airbag-ticket", headers=_hdr()).json()
    r = client.post(
        f"/sim/geometries/{gid}/airbag-deck?convert=false",
        files={"file": ("airbag.txt", io.BytesIO(b"x"), "text/plain")},
        headers={"Authorization": f"Bearer {t['token']}"},
    )
    assert r.status_code == 400


def test_deck_upload_needs_ticket_or_user(client):
    _pid, _tid, gid = _mk_geometry(client)
    r = client.post(f"/sim/geometries/{gid}/airbag-deck?convert=false",
                    files={"file": ("a.k", io.BytesIO(b"x"), "text/plain")})
    assert r.status_code in (401, 403)
