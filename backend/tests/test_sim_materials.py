"""材料库：关键字解析、物理材料归一、幂等导入、API 权限。

合成样例覆盖归一与导入语义（显式 LCID 的表）；真实种子文件
（ref/06_Material_T_mm_12.k.key，未随仓库走）存在时补充覆盖按序认领的表
与全量计数——与质量卡测试同理：这套东西的价值在"和真卡对得上"。
"""
import io
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.session import issue_token
from app.sim.db import SimDB
from app.sim.materials import import_material_text, parse_material_file
from app.sim.materials.keyword_import import guess_category, normalize_title
from app.sim.router import router as sim_router

SEED = os.path.join(os.path.dirname(__file__), "..", "..", "ref",
                    "06_Material_T_mm_12.k.key")

# 自由格式（逗号分隔）与定长混用是真实 deck 的常态，样例刻意用自由格式——
# 定长路径由种子文件测试覆盖。
SAMPLE = """*KEYWORD
$ 材料库合成样例
*MAT_PIECEWISE_LINEAR_PLASTICITY_TITLE
FOO235
1001,7.85E-9,210000.0,0.3,235.0
0.0,0.0,900,0,0.0
*MAT_NULL_TITLE
FOO235_NULL
1002,7.85E-9,0.0,0.0,0.0,0.0,210000.0,0.3
*MAT_PIECEWISE_LINEAR_PLASTICITY_TITLE
FOO235.1
1003,7.85E-9,206000.0,0.3,235.0
0.0,0.0,901,0,0.0
*DEFINE_TABLE_TITLE
FOO235
900,1.0,0.0
0.0,801
100.0,802
*DEFINE_CURVE_TITLE
FOO235 = 0 1/S
801,0,1.0,1000.0,0.0,0.0
0.0,0.235
0.1,0.30
*DEFINE_CURVE_TITLE
FOO235 = 100 1/S
802,0,1.0,1000.0,0.0,0.0
0.0,0.28
0.1,0.36
*DEFINE_CURVE_TITLE
BAR curve
901,0,1.0,1.0,0.0,0.0
0.0,1.0
1.0,2.0
*END
"""


@pytest.fixture
def db(tmp_path):
    d = SimDB(str(tmp_path / "sim.db"))
    yield d
    d.close()


# --- 解析与归一 -----------------------------------------------------------

def test_parse_sample():
    pr = parse_material_file(SAMPLE)
    assert len(pr.mats) == 3
    assert set(pr.curves) == {801, 802, 901}
    assert pr.tables[900].member_lcids == [801, 802]
    m24 = pr.mats[0]
    assert m24.mid == 1001
    assert m24.refs == [(900, "stress_strain")]
    assert pr.curves[801].sfo == 1000.0  # SFA/SFO 必须保留，纵坐标按它缩放


def test_normalize_title_strips_variant_suffixes():
    assert normalize_title("SAPH440.2", "PIECEWISE_LINEAR_PLASTICITY") == ("SAPH440", "alt")
    assert normalize_title("SAPH440_NULL", "NULL") == ("SAPH440", "null")
    # MAT_NULL 不管叫什么都是 null 变体，不是独立的物理材料模型
    assert normalize_title("MatNull", "NULL") == ("MatNull", "null")
    assert normalize_title("GLUE_DH", "ELASTIC") == ("GLUE_DH", "primary")
    # 名字里合法的点号不能被当成变体后缀剥掉
    assert normalize_title("GMP.PP.003 (MAT24)", "PIECEWISE_LINEAR_PLASTICITY")[0] \
        == "GMP.PP.003 (MAT24)"


def test_guess_category():
    assert guess_category("HC340/590DP", "PIECEWISE_LINEAR_PLASTICITY") == "steel"
    assert guess_category("RLE15665M-AL-E-6082-T6A-270", "PIECEWISE_LINEAR_PLASTICITY") == "aluminum"
    assert guess_category("GMP.PP.003 (MAT24)", "PIECEWISE_LINEAR_PLASTICITY") == "plastic"
    assert guess_category("GLUE_DH", "ELASTIC") == "adhesive"
    assert guess_category("MAT32_Glass", "LAMINATED_GLASS") == "glass"


# --- 导入语义 ---------------------------------------------------------------

def test_import_groups_variants_under_one_material(db):
    report = import_material_text(db, SAMPLE, source="sample.key", actor="root")
    assert report["materials_created"] == 1
    assert report["cards"] == 3
    assert report["curves"] == 3

    row = db.get_material_by_name("FOO235")
    assert row["revision"] == 1
    assert row["category"] == "other"

    cards = db.material_cards(row["id"])
    # primary 排最前，NULL 伴生卡与 .1 变体都归到同一物理材料
    assert [c["variant"] for c in cards] == ["primary", "alt", "null"] or \
           cards[0]["variant"] == "primary"
    assert {c["variant"] for c in cards} == {"primary", "alt", "null"}

    # 主卡的原文块自包含：MAT + 引用的表 + 族内曲线
    kt = cards[0]["keyword_text"]
    assert "*MAT_PIECEWISE_LINEAR_PLASTICITY_TITLE" in kt
    assert "*DEFINE_TABLE_TITLE" in kt
    assert kt.count("*DEFINE_CURVE_TITLE") == 2

    curves = db.material_curves(row["id"])
    fam = [c for c in curves if c["family_key"] == "table:900"]
    assert len(fam) == 2
    assert '"strain_rate": 0.0' in fam[0]["condition_json"]

    props = {p["name"]: (p["value"], p["unit"]) for p in db.material_properties(row["id"])}
    assert props["density"] == (7.85e-9, "t/mm^3")
    assert props["youngs_modulus"] == (210000.0, "MPa")
    assert props["yield_strength"] == (235.0, "MPa")


def test_import_is_idempotent_and_bumps_revision_on_change(db):
    import_material_text(db, SAMPLE)
    r2 = import_material_text(db, SAMPLE)
    assert (r2["materials_created"], r2["materials_updated"],
            r2["materials_unchanged"]) == (0, 0, 1)

    changed = SAMPLE.replace("0.1,0.30", "0.1,0.31")
    r3 = import_material_text(db, changed)
    assert r3["materials_updated"] == 1
    assert db.get_material_by_name("FOO235")["revision"] == 2
    # 整体替换：不残留旧曲线
    assert len(db.material_curves(db.get_material_by_name("FOO235")["id"])) == 3


# --- API 权限 ---------------------------------------------------------------

@pytest.fixture
def client(tmp_path, restrict_admins):
    app = FastAPI()
    app.include_router(sim_router)
    d = SimDB(str(tmp_path / "sim.db"))
    app.state.sim_db = d
    with TestClient(app) as c:
        yield c
    d.close()


def hdr(user: str) -> dict:
    return {"Authorization": f"Bearer {issue_token(user)}"}


def upload(client, user: str, text: str = SAMPLE):
    return client.post(
        "/sim/materials/import",
        files={"file": ("sample.key", io.BytesIO(text.encode()), "text/plain")},
        headers=hdr(user),
    )


def test_write_is_admin_only(client):
    assert upload(client, "u").status_code == 403
    r = upload(client, "root")
    assert r.status_code == 201, r.text
    assert r.json()["materials_created"] == 1

    mid = client.get("/sim/materials", headers=hdr("u")).json()[0]["id"]
    assert client.patch(f"/sim/materials/{mid}", json={"category": "steel"},
                        headers=hdr("u")).status_code == 403
    assert client.delete(f"/sim/materials/{mid}", headers=hdr("u")).status_code == 403
    # 读对所有登录用户开放
    assert client.get(f"/sim/materials/{mid}", headers=hdr("u")).status_code == 200


def test_material_detail_and_meta_edit(client):
    upload(client, "root")
    lst = client.get("/sim/materials", headers=hdr("u")).json()
    assert len(lst) == 1
    assert lst[0]["card_count"] == 3
    assert lst[0]["curve_count"] == 3

    mid = lst[0]["id"]
    detail = client.get(f"/sim/materials/{mid}", headers=hdr("u")).json()
    assert detail["name"] == "FOO235"
    assert len(detail["cards"]) == 3
    assert detail["cards"][0]["variant"] == "primary"
    c801 = next(c for c in detail["curves"] if c["source_lcid"] == 801)
    assert c801["points"][0] == [0.0, 0.235]
    assert c801["scale"]["sfo"] == 1000.0

    r = client.patch(f"/sim/materials/{mid}",
                     json={"category": "steel", "standard_code": "GB/T 700"},
                     headers=hdr("root"))
    assert r.status_code == 200
    assert r.json()["category"] == "steel"

    r = client.delete(f"/sim/materials/{mid}", headers=hdr("root"))
    assert r.status_code == 204
    assert client.get("/sim/materials", headers=hdr("u")).json() == []


def test_rename_conflict_is_409(client):
    upload(client, "root")
    two = SAMPLE.replace("FOO235", "BAR300")
    upload(client, "root", two)
    mats = {m["name"]: m["id"] for m in client.get("/sim/materials", headers=hdr("u")).json()}
    r = client.patch(f"/sim/materials/{mats['BAR300']}", json={"name": "FOO235"},
                     headers=hdr("root"))
    assert r.status_code == 409


# --- 真实种子文件 -----------------------------------------------------------

@pytest.mark.skipif(not os.path.isfile(SEED), reason="种子文件未随仓库分发")
def test_seed_file_roundtrip(db):
    with open(SEED, encoding="utf-8", errors="replace") as f:
        text = f.read()
    pr = parse_material_file(text)
    assert len(pr.mats) == 76
    assert len(pr.curves) == 220
    assert len(pr.tables) == 30
    # 按序认领：SAPH370 的表没写 LCID，成员曲线跟在表后
    assert pr.tables[1200000].member_lcids == [1200001 + i for i in range(6)]

    report = import_material_text(db, text, source=os.path.basename(SEED))
    assert report["materials_created"] == 68
    assert import_material_text(db, text)["materials_unchanged"] == 68

    saph = db.get_material_by_name("SAPH370")
    curves = db.material_curves(saph["id"])
    assert len(curves) == 6
    assert {c["family_key"] for c in curves} == {"table:1200000"}
    # NULL 伴生卡归并：SAPH440 组里有 primary 与 null 两种变体
    saph440 = db.get_material_by_name("SAPH440")
    assert {c["variant"] for c in db.material_cards(saph440["id"])} == \
        {"primary", "alt", "null"}
