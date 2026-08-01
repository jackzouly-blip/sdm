"""材料模板文件与控制卡模板库。

材料模板是**组装式**的：只记"选了哪几张卡"，导出时拼各卡原文。
这里重点验证三道组装校验（单位制/MID/LCID）——它们拦的都是
"语法合法但求解器静默算错"的情况，跑得出结果但结果是错的。
"""
import os
import time

import pytest

from app.sim.db import SimDB
from app.sim.materials import import_material_text
from app.sim.materials.templates import (
    check_material_template,
    lcids_in_card,
    render_control_template,
    render_material_template,
)

REF = os.path.join(os.path.dirname(__file__), "..", "..", "ref")
SEED = os.path.abspath(os.path.join(REF, "06_Material_T_mm_12.k.key"))
CTRL = os.path.abspath(os.path.join(REF, "5P-BAG", "Model", "03_Control_card.k"))


@pytest.fixture()
def db(tmp_path):
    d = SimDB(str(tmp_path / "t.db"))
    yield d
    d.close()


@pytest.fixture()
def seeded(db):
    """用真实材料库做种子——构造的样例盖不住真实文件的形态。"""
    if not os.path.exists(SEED):
        pytest.skip("缺少种子文件 ref/06_Material_T_mm_12.k.key")
    text = open(SEED, encoding="utf-8").read()
    import_material_text(db, text, unit_system="t-mm-s", source="seed", actor="t")
    return db


# ── 卡内曲线号扫描 ──────────────────────────────────────────

def test_lcids_in_card_reads_own_curves():
    """自包含块里的曲线号要扫得出;不能靠查 sim_material_curve——
    主卡与 NULL 变体共用 material_id,按材料查会把别人的曲线算进来。"""
    text = "\n".join([
        "*MAT_ELASTIC_TITLE", "steel", "         1   7.85E-9     210.0       0.3",
        "*DEFINE_CURVE", "        16         0       1.0       1.0",
        "                 0.0                 0.0",
        "*DEFINE_CURVE_TITLE", "rate", "        17         0       1.0       1.0",
        "                 0.0                 1.0",
    ])
    assert lcids_in_card(text) == {16, 17}


def test_lcids_empty_for_card_without_curves():
    assert lcids_in_card("*MAT_NULL\n         9   7.85E-9") == set()


# ── 组装校验 ────────────────────────────────────────────────

def _cards_of(db, n=4):
    out = []
    for m in db.list_materials()[:n]:
        out += [c["id"] for c in db.material_cards(m["id"])]
    return out


def test_check_ok_for_same_unit_system(seeded):
    r = check_material_template(seeded, _cards_of(seeded))
    assert r["ok"] is True
    assert r["unit_system"] == "t-mm-s"
    assert r["problems"] == []


def test_check_rejects_declared_unit_mismatch(seeded):
    """模板声明的单位制与成员卡不符必须拒绝：LS-DYNA 无量纲，
    混用不报错但结果差若干个数量级。"""
    r = check_material_template(seeded, _cards_of(seeded), unit_system="mm-kg-ms")
    assert r["ok"] is False
    assert any(p["level"] == "REJECT" and p["kind"] == "unit_system" for p in r["problems"])


def test_check_detects_mid_collision(db):
    mid = db.create_material("X", category="steel")
    db.replace_material_content(mid, [], [], [
        {"mat_type": "ELASTIC", "title": "A", "variant": "primary",
         "unit_system": "t-mm-s", "source_mid": 100, "params": {},
         "keyword_text": "*MAT_ELASTIC\n       100   7.85E-9"},
        {"mat_type": "ELASTIC", "title": "B", "variant": "alt",
         "unit_system": "t-mm-s", "source_mid": 100, "params": {},
         "keyword_text": "*MAT_ELASTIC\n       100   2.70E-9"},
    ])
    ids = [c["id"] for c in db.material_cards(mid)]
    r = check_material_template(db, ids)
    assert r["ok"] is False
    p = next(p for p in r["problems"] if p["kind"] == "mid_collision")
    assert p["mid"] == 100 and len(p["card_ids"]) == 2


def test_check_detects_lcid_collision_across_cards(db):
    """两张卡各自带着同号曲线 —— 撞号后曲线张冠李戴，求解器照样算完。"""
    m1 = db.create_material("M1", category="steel")
    db.replace_material_content(m1, [], [], [
        {"mat_type": "PIECEWISE_LINEAR_PLASTICITY", "title": "M1", "variant": "primary",
         "unit_system": "t-mm-s", "source_mid": 1, "params": {},
         "keyword_text": "*MAT_PIECEWISE_LINEAR_PLASTICITY\n         1\n"
                         "*DEFINE_CURVE\n        50         0\n           0.0   0.0"},
    ])
    m2 = db.create_material("M2", category="steel")
    db.replace_material_content(m2, [], [], [
        {"mat_type": "PIECEWISE_LINEAR_PLASTICITY", "title": "M2", "variant": "primary",
         "unit_system": "t-mm-s", "source_mid": 2, "params": {},
         "keyword_text": "*MAT_PIECEWISE_LINEAR_PLASTICITY\n         2\n"
                         "*DEFINE_CURVE\n        50         0\n           0.0   1.0"},
    ])
    ids = [c["id"] for c in db.material_cards(m1)] + [c["id"] for c in db.material_cards(m2)]
    r = check_material_template(db, ids)
    assert r["ok"] is False
    p = next(p for p in r["problems"] if p["kind"] == "lcid_collision")
    assert p["lcid"] == 50


def test_check_rejects_missing_card(db):
    r = check_material_template(db, ["不存在的卡"])
    assert r["ok"] is False
    assert r["problems"][0]["kind"] == "missing_card"


def test_check_rejects_empty(db):
    r = check_material_template(db, [])
    assert r["ok"] is False


# ── 模板 CRUD 与导出 ────────────────────────────────────────

def test_template_crud_and_items(seeded):
    tid = seeded.create_material_template("气囊用材料", unit_system="t-mm-s",
                                          description="5P-BAG", created_by="t")
    cards = _cards_of(seeded, 3)
    seeded.set_material_template_items(tid, cards)

    tpl = seeded.get_material_template(tid)
    assert tpl["name"] == "气囊用材料"
    assert tpl["revision"] == 2, "改成员列表应出新修订"

    items = seeded.list_material_template_items(tid)
    assert [i["card_id"] for i in items] == cards, "顺序即导出顺序"
    assert items[0]["material_name"], "列表应带材料名，前端不必再查一遍"

    lst = seeded.list_material_templates()
    assert lst[0]["card_count"] == len(cards)

    seeded.update_material_template(tid, description="改过")
    assert seeded.get_material_template(tid)["description"] == "改过"
    assert seeded.delete_material_template(tid) is True
    assert seeded.get_material_template(tid) is None
    assert seeded.list_material_template_items(tid) == [], "成员应级联删除"


def test_render_carries_full_closure(seeded):
    """导出的 MAT.K 必须自带曲线闭包 —— 少一条曲线,求解器读到的是默认值。"""
    steels = [m for m in seeded.list_materials()
              if "SAPH" in m["name"] or "HC" in m["name"]][:5]
    cards = [c["id"] for m in steels for c in seeded.material_cards(m["id"])]
    r = check_material_template(seeded, cards)
    assert r["ok"] and r["id_ranges"]["LCID"], "这些钢材应带曲线"

    tid = seeded.create_material_template("车身钢材", unit_system="t-mm-s")
    seeded.set_material_template_items(tid, cards)
    out = render_material_template(seeded, tid)

    assert out.startswith("*KEYWORD")
    assert out.rstrip().endswith("*END")
    assert out.count("*MAT_") == len(cards)
    # 闭包:校验阶段数出多少条曲线,导出就该有多少条
    assert out.count("*DEFINE_CURVE") + out.count("*DEFINE_TABLE") == len(r["id_ranges"]["LCID"])
    # 各卡原文里自带的 *KEYWORD/*END 不能重复出现在中间
    assert out.count("*KEYWORD") == 1
    assert out.count("*END") == 1


def test_render_skips_deleted_card(seeded):
    tid = seeded.create_material_template("含失效成员", unit_system="t-mm-s")
    cards = _cards_of(seeded, 2)
    seeded.set_material_template_items(tid, cards)
    # 直接删掉一张卡所属材料，模板成员随之失效
    card = seeded.get_material_card(cards[0])
    seeded.delete_material(card["material_id"])
    out = render_material_template(seeded, tid)
    assert out.startswith("*KEYWORD"), "个别成员失效不应让整个导出崩掉"


def test_render_unknown_template_raises(db):
    with pytest.raises(ValueError):
        render_material_template(db, "nope")


# ── 控制卡模板（整份存档）────────────────────────────────────

def test_control_template_is_verbatim(db):
    if not os.path.exists(CTRL):
        pytest.skip("缺少控制卡样例")
    text = open(CTRL, encoding="utf-8").read()
    tid = db.create_control_template(
        "气囊展开控制卡", unit_system="mm-kg-ms", keyword_text=text,
        analysis_type="气囊展开", source_name="03_Control_card.k", created_by="t")
    assert render_control_template(db, tid) == text, "整份存档必须逐字返回"

    row = db.get_control_template(tid)
    assert row["analysis_type"] == "气囊展开"
    assert row["unit_system"] == "mm-kg-ms"

    lst = db.list_control_templates(analysis_type="气囊展开")
    assert len(lst) == 1
    assert "keyword_text" not in lst[0].keys(), "列表不该带几 KB 正文"

    db.update_control_template(tid, description="120ms/48帧")
    assert db.get_control_template(tid)["description"] == "120ms/48帧"
    assert db.delete_control_template(tid) is True
    assert db.get_control_template(tid) is None


def test_control_template_name_is_unique(db):
    db.create_control_template("同名", unit_system="mm-kg-ms", keyword_text="*KEYWORD\n*END\n")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.create_control_template("同名", unit_system="mm-kg-ms", keyword_text="*KEYWORD\n*END\n")
