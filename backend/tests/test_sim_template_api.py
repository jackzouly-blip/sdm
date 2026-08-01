"""材料模板 / 控制卡模板 API。

重点验证两件事：
  1. **写操作管理员限定**——模板是组织级资产，与材料库一致；
  2. **组装校验挡在入库之前**——单位制不一致或 ID 撞车必须 422，
     不能先落库再说：落了库的模板会被人拿去跑仿真。
"""
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.session import issue_token
from app.sim.db import SimDB
from app.sim.materials import import_material_text
from app.sim.router import router as sim_router

REF = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "ref"))
SEED = os.path.join(REF, "06_Material_T_mm_12.k.key")
CTRL = os.path.join(REF, "5P-BAG", "Model", "03_Control_card.k")


@pytest.fixture()
def client(tmp_path, restrict_admins):
    db = SimDB(str(tmp_path / "t.db"))
    if os.path.exists(SEED):
        import_material_text(db, open(SEED, encoding="utf-8").read(),
                             unit_system="t-mm-s", source="seed", actor="seed")
    app = FastAPI()
    app.state.sim_db = db
    app.include_router(sim_router)
    with TestClient(app) as c:
        c.db = db
        yield c
    db.close()


def _hdr(user="root"):
    """root 在 restrict_admins 的白名单里；其它用户即普通用户。"""
    return {"Authorization": f"Bearer {issue_token(user)}"}


def _cards(db, n=3):
    out = []
    for m in db.list_materials()[:n]:
        out += [c["id"] for c in db.material_cards(m["id"])]
    return out


# ── 材料模板 ────────────────────────────────────────────────

def test_write_is_admin_only(client):
    r = client.post("/sim/material-templates",
                    json={"name": "X", "unit_system": "t-mm-s"},
                    headers=_hdr("bob"))
    assert r.status_code == 403


def test_create_list_get_export(client):
    if not os.path.exists(SEED):
        pytest.skip("缺少种子文件")
    cards = _cards(client.db)
    r = client.post("/sim/material-templates",
                    json={"name": "气囊用材料", "unit_system": "t-mm-s",
                          "description": "5P-BAG", "card_ids": cards},
                    headers=_hdr())
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    lst = client.get("/sim/material-templates", headers=_hdr()).json()
    assert len(lst) == 1 and lst[0]["card_count"] == len(cards)

    detail = client.get(f"/sim/material-templates/{tid}", headers=_hdr()).json()
    assert [i["card_id"] for i in detail["items"]] == cards
    assert detail["items"][0]["material_name"]

    exp = client.get(f"/sim/material-templates/{tid}/export", headers=_hdr())
    assert exp.status_code == 200
    assert "attachment" in exp.headers["content-disposition"]
    text = exp.text
    assert text.startswith("*KEYWORD") and text.rstrip().endswith("*END")
    assert text.count("*MAT_") == len(cards)


def test_create_rejects_unit_mismatch(client):
    """声明 mm-kg-ms 但成员卡是 t-mm-s —— 必须在入库前挡住。"""
    if not os.path.exists(SEED):
        pytest.skip("缺少种子文件")
    r = client.post("/sim/material-templates",
                    json={"name": "混用", "unit_system": "mm-kg-ms",
                          "card_ids": _cards(client.db)},
                    headers=_hdr())
    assert r.status_code == 422
    assert "无法组装" in str(r.json())
    assert client.get("/sim/material-templates", headers=_hdr()).json() == [], \
        "校验失败不应留下半成品模板"


def test_check_endpoint_reports_problems(client):
    if not os.path.exists(SEED):
        pytest.skip("缺少种子文件")
    r = client.post("/sim/material-templates/check?unit_system=mm-kg-ms",
                    json={"card_ids": _cards(client.db)}, headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert any(p["kind"] == "unit_system" for p in body["problems"])


def test_duplicate_name_is_409(client):
    client.post("/sim/material-templates",
                json={"name": "同名", "unit_system": "t-mm-s"}, headers=_hdr())
    r = client.post("/sim/material-templates",
                    json={"name": "同名", "unit_system": "t-mm-s"}, headers=_hdr())
    assert r.status_code == 409


def test_set_items_revalidates(client):
    if not os.path.exists(SEED):
        pytest.skip("缺少种子文件")
    tid = client.post("/sim/material-templates",
                      json={"name": "空模板", "unit_system": "mm-kg-ms"},
                      headers=_hdr()).json()["id"]
    # 模板声明 mm-kg-ms,塞 t-mm-s 的卡进去要被拒
    r = client.put(f"/sim/material-templates/{tid}/items",
                   json={"card_ids": _cards(client.db)}, headers=_hdr())
    assert r.status_code == 422


def test_patch_and_delete(client):
    tid = client.post("/sim/material-templates",
                      json={"name": "待改", "unit_system": "t-mm-s"},
                      headers=_hdr()).json()["id"]
    r = client.patch(f"/sim/material-templates/{tid}",
                     json={"description": "改过了"}, headers=_hdr())
    assert r.json()["description"] == "改过了"
    assert client.delete(f"/sim/material-templates/{tid}", headers=_hdr()).status_code == 204
    assert client.get(f"/sim/material-templates/{tid}", headers=_hdr()).status_code == 404


# ── 控制卡模板 ──────────────────────────────────────────────

def test_control_template_roundtrip_is_verbatim(client):
    if not os.path.exists(CTRL):
        pytest.skip("缺少控制卡样例")
    text = open(CTRL, encoding="utf-8").read()
    r = client.post("/sim/control-templates",
                    json={"name": "气囊展开控制卡", "unit_system": "mm-kg-ms",
                          "keyword_text": text, "analysis_type": "气囊展开",
                          "source_name": "03_Control_card.k"},
                    headers=_hdr())
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert r.json()["source_sha256"], "应记录内容指纹"

    lst = client.get("/sim/control-templates?analysis_type=气囊展开", headers=_hdr()).json()
    assert len(lst) == 1
    assert "keyword_text" not in lst[0], "列表不该带几 KB 正文"

    exp = client.get(f"/sim/control-templates/{tid}/export", headers=_hdr())
    assert exp.text == text, "整份存档必须逐字返回"


def test_control_template_admin_only_and_404(client):
    r = client.post("/sim/control-templates",
                    json={"name": "X", "unit_system": "mm-kg-ms", "keyword_text": "*KEYWORD\n*END\n"},
                    headers=_hdr("bob"))
    assert r.status_code == 403
    assert client.get("/sim/control-templates/nope", headers=_hdr()).status_code == 404


# ── 解析票据 ────────────────────────────────────────────────
# vektor3d 装在用户桌面、拿不到会话令牌，只能靠票据自取正文。票据的全部价值
# 在于"能做的只有那一件事"，所以越权用法必须逐条挡住。

def _mk_control(client, name="控制卡A", text="*KEYWORD\n*CONTROL_TERMINATION\n  120.0\n*END\n"):
    return client.post("/sim/control-templates",
                       json={"name": name, "unit_system": "mm-kg-ms",
                             "keyword_text": text, "source_name": f"{name}.k"},
                       headers=_hdr()).json()["id"]


def test_parse_ticket_can_fetch_export(client):
    """票据的正路：签出来能且只能 GET 那份导出正文。"""
    text = "*KEYWORD\n*CONTROL_TERMINATION\n  120.0\n*END\n"
    tid = _mk_control(client, text=text)
    t = client.post(f"/sim/control-templates/{tid}/parse-ticket", headers=_hdr()).json()
    assert t["kind"] == "control" and t["unit_system"] == "mm-kg-ms"
    assert t["source_path_suffix"] == f"/sim/control-templates/{tid}/export"
    assert t["expected_sha256"], "应带上内容指纹供能力侧判缓存脏否"

    # 只带票据、不带会话令牌 —— 正是 vektor3d 的处境
    r = client.get(f"/sim/control-templates/{tid}/export",
                   headers={"Authorization": f"Bearer {t['token']}"})
    assert r.status_code == 200 and r.text == text


def test_parse_ticket_is_bound_to_its_own_template(client):
    """换个 tid 就失效：一张票据只能读它签发时那一份。"""
    a, b = _mk_control(client, "甲"), _mk_control(client, "乙")
    t = client.post(f"/sim/control-templates/{a}/parse-ticket", headers=_hdr()).json()
    r = client.get(f"/sim/control-templates/{b}/export",
                   headers={"Authorization": f"Bearer {t['token']}"})
    assert r.status_code in (401, 403)


def test_parse_ticket_is_bound_to_kind(client):
    """材料票据读不了控制卡：两边 id 空间独立，不绑 kind 就会留下这个缺口。"""
    mid = client.post("/sim/material-templates",
                      json={"name": "材料模板", "unit_system": "t-mm-s"},
                      headers=_hdr()).json()["id"]
    ctl = _mk_control(client, "控制卡B")
    t = client.post(f"/sim/material-templates/{mid}/parse-ticket", headers=_hdr()).json()
    assert t["kind"] == "material"
    r = client.get(f"/sim/control-templates/{ctl}/export",
                   headers={"Authorization": f"Bearer {t['token']}"})
    assert r.status_code in (401, 403)


def test_parse_ticket_cannot_write(client):
    """票据只读：拿它去 PATCH 必须被拒——解析结果由浏览器带用户令牌写回。"""
    tid = _mk_control(client, "控制卡C")
    t = client.post(f"/sim/control-templates/{tid}/parse-ticket", headers=_hdr()).json()
    r = client.patch(f"/sim/control-templates/{tid}",
                     json={"summary_json": '{"strategy":{}}'},
                     headers={"Authorization": f"Bearer {t['token']}"})
    assert r.status_code in (401, 403)


def test_parse_result_writes_back(client):
    """解析结果的落点：控制卡与材料模板都要能收下 summary_json。"""
    tid = _mk_control(client, "控制卡D")
    summary = '{"strategy":{"求解时长":"120 ms"},"keywordCount":1}'
    r = client.patch(f"/sim/control-templates/{tid}",
                     json={"summary_json": summary}, headers=_hdr())
    assert r.status_code == 200 and r.json()["summary_json"] == summary

    mid = client.post("/sim/material-templates",
                      json={"name": "材料模板2", "unit_system": "t-mm-s"},
                      headers=_hdr()).json()["id"]
    r = client.patch(f"/sim/material-templates/{mid}",
                     json={"summary_json": '{"issues":[]}'}, headers=_hdr())
    assert r.status_code == 200 and r.json()["summary_json"] == '{"issues":[]}'


def test_parse_ticket_404_on_missing(client):
    assert client.post("/sim/control-templates/nope/parse-ticket",
                       headers=_hdr()).status_code == 404
    assert client.post("/sim/material-templates/nope/parse-ticket",
                       headers=_hdr()).status_code == 404
