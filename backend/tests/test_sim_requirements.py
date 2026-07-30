"""客户需求文档 + 项目质量卡实例。

这两块是同一条链的两端：
  建项目 → 传需求文档 → （AI 分析，尚未接入）→ 选模板派生质量卡实例 → 调参 → 导出给 ANSA

AI 那一步现在是空的（缺真实需求文档样本，输入形态未知），所以这里覆盖的是
"人工走完这条链"——AI 到位后是替人填「依据」，不是换一条链。
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
    """启用管理员白名单：不配置的话隔离用例是假通过。"""


@pytest.fixture
def real_fs(monkeypatch, tmp_path, patch_or_stub):
    def fake_write_file(user, parent, name, data, roots):
        target = os.path.join(parent, name)
        os.makedirs(parent, exist_ok=True)
        if os.path.exists(target):
            raise RuntimeError("目标已存在")
        with open(target, "wb") as f:
            f.write(data)
        return {"path": target}

    patch_or_stub("app.fs.browser", {"write_file": fake_write_file})
    monkeypatch.setenv("HPC_FS_ROOTS", str(tmp_path))
    monkeypatch.setenv("HPC_SIM_WORKDIR_ROOT", str(tmp_path / "simroot"))
    # 用户质量卡模板落在 db_path 同级目录。不隔离它，导入用例会把模板写进
    # 仓库的 backend/state/ 并在下次运行时 409——测试污染工作区是硬伤。
    monkeypatch.setenv("HPC_DB_PATH", str(tmp_path / "portal.db"))
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
def project(client):
    return client.post("/sim/projects", json={"name": "X90座椅碰撞"}, headers=hdr()).json()["id"]


def upload_req(client, pid, name="技术协议.pdf", data=b"%PDF-1.4 fake", doc_type="agreement", user="u"):
    return client.post(
        f"/sim/projects/{pid}/requirements/upload",
        files={"file": (name, io.BytesIO(data), "application/pdf")},
        data={"doc_type": doc_type},
        headers=hdr(user),
    )


# --- 需求文档 -----------------------------------------------------------

def test_upload_and_list_requirement(client, project):
    r = upload_req(client, project)
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["name"] == "技术协议.pdf"
    assert doc["doc_type"] == "agreement"
    assert doc["analysis_status"] == "pending"     # AI 未接入，如实标 pending
    assert doc["analysis"] is None
    assert os.path.isfile(doc["source_file"]["path"])
    # 与几何同处项目工作目录下，便于一并归档/清理
    assert "sdm_requirements" in doc["source_file"]["path"]

    rows = client.get(f"/sim/projects/{project}/requirements", headers=hdr()).json()
    assert [x["id"] for x in rows] == [doc["id"]]


def test_download_requirement(client, project):
    doc = upload_req(client, project, data=b"REQ-CONTENT").json()
    r = client.get(f"/sim/requirements/{doc['id']}/download", headers=hdr())
    assert r.status_code == 200 and r.content == b"REQ-CONTENT"


def test_requirement_isolated_by_project_owner(client, project):
    doc = upload_req(client, project).json()
    assert upload_req(client, project, user="intruder").status_code == 404
    assert client.get(f"/sim/requirements/{doc['id']}/download",
                      headers=hdr("intruder")).status_code == 404
    assert client.delete(f"/sim/requirements/{doc['id']}",
                         headers=hdr("intruder")).status_code == 404


def test_delete_requirement(client, project):
    doc = upload_req(client, project).json()
    assert client.delete(f"/sim/requirements/{doc['id']}", headers=hdr()).status_code == 204
    assert client.get(f"/sim/projects/{project}/requirements", headers=hdr()).json() == []


# --- 模板库 -------------------------------------------------------------

def test_builtin_template_is_the_default_base(client):
    rows = client.get("/sim/quality-templates", headers=hdr()).json()
    builtin = [t for t in rows if t["id"] == "generic-crash-5mm"]
    assert builtin and builtin[0]["builtin"] is True


def test_template_detail_exposes_criteria_and_algorithms(client):
    d = client.get("/sim/quality-templates/generic-crash-5mm", headers=hdr()).json()
    assert d["ansaVersion"] == "19.1.1"
    # criteria 只含启用的 shells 判据——"这张卡在管什么"的答案
    assert len(d["criteria"]) == 11
    by_name = {c["name"]: c for c in d["criteria"]}
    assert by_name["aspect ratio"]["calculation"] == "NASTRAN"
    assert by_name["warping"]["calculation"] == "IDEAS"
    assert by_name["max length"]["higherIsBetter"] is False   # 方向不能判反
    assert d["meshParams"]["targetElementLength"] == 5.0
    # 判废线 3mm 对应的时间步 ≈ 0.58μs，碰撞里这是机时的总闸
    assert d["timeStepAtFailedMinLength"] == pytest.approx(5.8e-7, rel=0.02)


def test_template_detail_exposes_whole_card_not_just_enabled_rows(client):
    """卡远不止启用的 11 行:停用判据、solids 域、生成侧全部参数都要能看到,
    否则用户会以为整张卡就一屏——而其余内容其实都会原样交给 ANSA。"""
    d = client.get("/sim/quality-templates/generic-crash-5mm", headers=hdr()).json()

    # 全量判据:两个域都在,停用的带着 enabled=False 一起回
    assert len(d["allCriteria"]) > len(d["criteria"])
    assert {c["domain"] for c in d["allCriteria"]} == {"shells", "solids"}
    assert sum(c["enabled"] for c in d["allCriteria"]) == 11
    taper = next(c for c in d["allCriteria"]
                 if c["name"] == "taper" and c["domain"] == "shells")
    assert taper["enabled"] is False and taper["calculation"] == "PATRAN"

    # 生成侧参数按 .ansa_mpar 的分节分组,一项不落
    groups = {g["title"]: g["params"] for g in d["meshGroups"]}
    assert "CFD" in groups and "Fillets" in groups
    total = sum(len(p) for p in groups.values())
    assert total > 150
    general = {p["key"]: p["value"] for p in groups["General Mesh"]}
    assert general["target_element_length"] == "5."   # 原始字符串,不是 5.0


def test_unknown_template_404(client):
    assert client.get("/sim/quality-templates/nope", headers=hdr()).status_code == 404


# --- 项目质量卡实例 -----------------------------------------------------

def test_create_card_from_template_with_overrides(client, project):
    doc = upload_req(client, project).json()
    r = client.post(
        f"/sim/projects/{project}/quality-cards",
        json={
            "template_id": "generic-crash-5mm",
            "name": "X90 座椅 4mm 卡",
            "requirement_doc_id": doc["id"],
            "overrides": [
                {"target": "mesh:target_element_length", "new_value": "4.",
                 "source": "技术协议 3.2 节：网格尺寸 4mm"},
                {"target": "criteria:warping [shells]:failed", "new_value": "12.0",
                 "source": "技术协议 表4"},
            ],
        },
        headers=hdr(),
    )
    assert r.status_code == 201, r.text
    card = r.json()
    assert card["derived_from_doc_id"] == doc["id"]
    # 旧值由引擎回填，不信调用方给的
    assert [o["old_value"] for o in card["overrides"]] == ["5.", "15.0"]

    detail = client.get(f"/sim/quality-cards/{card['id']}", headers=hdr()).json()
    assert detail["card"]["meshParams"]["targetElementLength"] == 4.0
    warp = [c for c in detail["card"]["criteria"] if c["name"] == "warping"][0]
    assert warp["thresholds"]["failed"] == 12.0
    # 没被覆盖的项原样继承
    jac = [c for c in detail["card"]["criteria"] if c["name"] == "jacobian"][0]
    assert jac["thresholds"]["failed"] == 0.6


def test_override_without_source_is_rejected(client, project):
    """无出处的阈值就是编的，而这个数字会一路流进网格验收。"""
    r = client.post(
        f"/sim/projects/{project}/quality-cards",
        json={"name": "无依据卡",
              "overrides": [{"target": "mesh:target_element_length", "new_value": "4."}]},
        headers=hdr(),
    )
    assert r.status_code == 400
    assert "依据" in r.json()["detail"]


def test_invalid_override_target_rejected_and_nothing_left_behind(client, project):
    r = client.post(
        f"/sim/projects/{project}/quality-cards",
        json={"name": "坏卡",
              "overrides": [{"target": "mesh:no_such_key", "new_value": "1", "source": "x"}]},
        headers=hdr(),
    )
    assert r.status_code == 400
    assert client.get(f"/sim/projects/{project}/quality-cards", headers=hdr()).json() == []


def test_export_gives_ansa_ready_files(client, project):
    """导出的必须是能直接喂给 ANSA 的原文件，不做二次生成。"""
    card = client.post(
        f"/sim/projects/{project}/quality-cards",
        json={"name": "导出卡",
              "overrides": [{"target": "mesh:target_element_length", "new_value": "4.",
                             "source": "协议"}]},
        headers=hdr(),
    ).json()

    qual = client.get(f"/sim/quality-cards/{card['id']}/export",
                      params={"kind": "qual"}, headers=hdr())
    assert qual.status_code == 200
    assert "ANSA_Version" in qual.text and "[shells]" in qual.text

    mpar = client.get(f"/sim/quality-cards/{card['id']}/export",
                      params={"kind": "mpar"}, headers=hdr())
    assert mpar.status_code == 200
    assert "target_element_length   = 4." in mpar.text     # 覆盖已落进文件


def test_card_isolated_by_project_owner(client, project):
    card = client.post(f"/sim/projects/{project}/quality-cards",
                       json={"name": "卡"}, headers=hdr()).json()
    assert client.get(f"/sim/quality-cards/{card['id']}",
                      headers=hdr("intruder")).status_code == 404
    assert client.delete(f"/sim/quality-cards/{card['id']}",
                         headers=hdr("intruder")).status_code == 404


def test_delete_card_removes_files(client, project):
    card = client.post(f"/sim/projects/{project}/quality-cards",
                       json={"name": "待删卡"}, headers=hdr()).json()
    card_dir = client.get(f"/sim/quality-cards/{card['id']}", headers=hdr()).json()["card_dir"]
    assert os.path.isdir(card_dir)
    assert client.delete(f"/sim/quality-cards/{card['id']}", headers=hdr()).status_code == 204
    assert not os.path.exists(card_dir)


def test_cards_land_in_project_workdir(client, project):
    """质量卡与几何、需求文档同处项目工作目录——备份/配额/清理只有一套口径。"""
    card = client.post(f"/sim/projects/{project}/quality-cards",
                       json={"name": "卡"}, headers=hdr()).json()
    assert "sdm_quality_cards" in card["card_dir"]
    proj = client.get(f"/sim/projects/{project}", headers=hdr()).json()
    assert card["card_dir"].startswith(proj["workdir"])


# --- 需求条目抽取（确定性规则，不依赖 AI）-------------------------------

from app.sim.requirements import extract_from_slides, extract_metrics
from app.sim.requirements.pptx_reader import Slide


def test_metrics_handle_chinese_comparators_and_units():
    """真实文档里比较关系有中文说法，只认 ≥≤ 会漏掉一半。"""
    ms = {(m.op, m.value, m.unit) for m in extract_metrics("减速度连续3ms内不能超过80g")}
    assert ("≤", 80.0, "g") in ms
    ms2 = {(m.op, m.value, m.unit) for m in extract_metrics("塑料件减重3%以上")}
    assert ("≥", 3.0, "%") in ms2


def test_bracket_notes_become_separate_metrics():
    """括号里放的是内控值/项目例外，与主值判定口径不同，必须分开成条。"""
    ms = extract_metrics("F≤3000N，侵入量35mm（T29为50mm）")
    override = [m for m in ms if m.kind == "override"]
    assert override and override[0].value == 50.0 and override[0].scope == "T29"
    assert any(m.op == "≤" and m.value == 3000.0 and m.kind == "target" for m in ms)

    internal = [m for m in extract_metrics("不能超过80g（内部标准72g）") if m.kind == "internal"]
    assert internal and internal[0].value == 72.0

    external = [m for m in extract_metrics("模态≥40Hz（对外目标≥38Hz）") if m.kind == "external"]
    assert external and external[0].value == 38.0


def test_extract_items_from_summary_table():
    slides = [Slide(number=1, lines=["2025年开发项目CAE分析要求"], tables=[[
        ["", "分析项", "目标值", "分析基准", "2025年项目"],
        ["1", "IP系统模态", "IP总成一级模态≥40Hz（对外目标≥38Hz）", "合格", "合格"],
        ["2", "IP系统膝碰", "F≤3000N，侵入量35mm（T29为50mm）", "参考", "重新讨论分析方法"],
    ]])]
    items = extract_from_slides(slides)
    assert len(items) == 2
    modal, knee = items
    assert modal.baseline == "required" and knee.baseline == "reference"
    assert modal.source_ref == "第1页 表1 第1行"
    assert knee.needs_clarification and knee.clarification_hint == "重新讨论"


def test_section_divider_page_produces_no_item():
    """分节页（页码 + 一个标题）不是要求，不该产条目。"""
    assert extract_from_slides([Slide(number=3, lines=["3", "网格要求"])]) == []


def test_load_points_counted_but_coordinates_are_not_claimed():
    """点位位置在图上，文字层只有编号——只能给出数量。"""
    slide = Slide(number=4, lines=["IP系统静态头碰", "P1", "P2", "P3", "加载点位信息"])
    items = extract_from_slides([slide])
    assert len(items) == 1 and items[0].load_points == 3
    assert items[0].category == "loading"


def test_unsupported_format_is_rejected_not_silently_empty():
    """返回空列表会被当成'这份文档没有要求'，比报错危险得多。"""
    from app.sim.requirements import extract_from_file

    with pytest.raises(ValueError, match="暂不支持"):
        extract_from_file("spec.docx")


# --- 条目接口 -----------------------------------------------------------

def _pptx_fixture(tmp_path):
    """造一个最小 pptx：一页总表 + 一页工况。"""
    import zipfile

    A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'

    def para(t):
        return f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>"

    def cell(t):
        return f"<a:tc><a:txBody>{para(t)}</a:txBody></a:tc>"

    rows = [["", "分析项", "目标值", "分析基准", "2025年项目"],
            ["1", "IP系统表面刚度", "直径50mm;90N，变形量≤1.0mm", "合格", "合格"]]
    tbl = "".join(f"<a:tr>{''.join(cell(c) for c in r)}</a:tr>" for r in rows)
    slide1 = (f'<p:sld {P} {A}><p:cSld><p:spTree>'
              f'<p:sp><p:txBody>{para("2025年开发项目CAE分析要求")}</p:txBody></p:sp>'
              f'<p:graphicFrame><a:graphic><a:graphicData><a:tbl>{tbl}</a:tbl>'
              f'</a:graphicData></a:graphic></p:graphicFrame>'
              f'</p:spTree></p:cSld></p:sld>')
    slide2 = (f'<p:sld {P} {A}><p:cSld><p:spTree>'
              f'<p:sp><p:txBody>{para("IP系统表面刚度")}</p:txBody></p:sp>'
              f'<p:sp><p:txBody>{para("P1")}{para("P2")}{para("直径50mm压头 90N")}</p:txBody></p:sp>'
              f'</p:spTree></p:cSld></p:sld>')

    path = tmp_path / "req.pptx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ppt/slides/slide1.xml", slide1)
        zf.writestr("ppt/slides/slide2.xml", slide2)
    return path


def _upload_pptx(client, project, tmp_path):
    pptx = _pptx_fixture(tmp_path)
    with open(pptx, "rb") as f:
        return client.post(
            f"/sim/projects/{project}/requirements/upload",
            files={"file": ("req.pptx", f, "application/octet-stream")},
            data={"doc_type": "spec"},
            headers=hdr(),
        ).json()


def test_extract_endpoint_persists_items_and_summary(client, project, tmp_path):
    doc = _upload_pptx(client, project, tmp_path)
    r = client.post(f"/sim/requirements/{doc['id']}/extract", headers=hdr())
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["summary"]["subjectCount"] == 1
    assert out["summary"]["loadingCount"] == 1
    assert out["summary"]["loadPointTotal"] == 2
    # 坐标抽不到这件事要写进产出，别让下游以为拿到了位置
    assert out["summary"]["loadPointCoordsAvailable"] is False
    assert out["summary"]["extractor"] == "rule"

    items = client.get(f"/sim/requirements/{doc['id']}/items", headers=hdr()).json()
    assert len(items) == 2
    subject = [i for i in items if i["category"] == "subject"][0]
    assert subject["baseline"] == "required"
    assert subject["source_ref"] == "第1页 表1 第1行"
    assert any(m["unit"] == "mm" and m["op"] == "≤" for m in subject["metrics"])

    docs = client.get(f"/sim/projects/{project}/requirements", headers=hdr()).json()
    assert docs[0]["analysis_status"] == "done"
    assert docs[0]["analysis"]["itemCount"] == 2


def test_reextract_replaces_instead_of_appending(client, project, tmp_path):
    """重解析必须幂等：追加会让库里同时躺着新旧两版条目。"""
    doc = _upload_pptx(client, project, tmp_path)
    first = client.post(f"/sim/requirements/{doc['id']}/extract", headers=hdr()).json()
    again = client.post(f"/sim/requirements/{doc['id']}/extract", headers=hdr()).json()
    assert len(first["items"]) == len(again["items"]) == 2


def test_manual_edit_marks_the_item_as_manual(client, project, tmp_path):
    """规则抽的和人改过的必须分得开，否则重解析会无声冲掉人的修正。"""
    doc = _upload_pptx(client, project, tmp_path)
    items = client.post(f"/sim/requirements/{doc['id']}/extract", headers=hdr()).json()["items"]
    iid = items[0]["id"]
    r = client.patch(f"/sim/requirement-items/{iid}",
                     json={"baseline": "reference", "status": "confirmed"}, headers=hdr())
    assert r.status_code == 200
    assert r.json()["extracted_by"] == "manual"
    assert r.json()["baseline"] == "reference"


def test_extract_rejects_unsupported_format(client, project):
    doc = upload_req(client, project, name="spec.pdf", data=b"%PDF-1.4")
    r = client.post(f"/sim/requirements/{doc.json()['id']}/extract", headers=hdr())
    assert r.status_code == 400 and "暂不支持" in r.json()["detail"]


# --- 规则/AI 交叉核对合并 -----------------------------------------------

from app.sim.requirements.merge import merge_rule_and_ai


def _rule_item(**kw):
    base = {"category": "subject", "title": "", "raw_text": "", "metrics": [],
            "baseline": "", "source_ref": "", "needs_clarification": False,
            "clarification_hint": "", "extracted_by": "rule", "load_points": 0,
            "indenter_diameter_mm": None}
    return {**base, **kw}


def _ai_item(**kw):
    return {**_rule_item(extracted_by="ai"), **kw}


def test_merge_corroborates_split_items_via_row_anchor():
    """AI 按方向拆条,但都指向同一表行——锚点对齐要吃下一对多,点位数按组求和。"""
    rule = [_rule_item(title="IP系统大屏刚度", source_ref="第1页 表1 第4行",
                       baseline="required", load_points=6)]
    ai = [_ai_item(title="IP系统大屏刚度测试 X向", source_ref="第1页 表1 第4行",
                   baseline="required", load_points=3),
          _ai_item(title="IP系统大屏刚度测试 Z向", source_ref="第1页 表1 第4行",
                   baseline="required", load_points=3)]
    merged, stats = merge_rule_and_ai(rule, ai)
    assert stats == {"ruleTotal": 1, "aiTotal": 2, "agreed": 1,
                     "conflicts": 0, "ruleOnly": 0, "aiOnly": 0}
    assert len(merged) == 2 and not any(i["needs_clarification"] for i in merged)


def test_merge_conflict_keeps_both_readings_and_forces_clarification():
    """数值冲突不裁决谁对:标待澄清,两个读数都进 hint 交给人。"""
    rule = [_rule_item(title="安装卡接孔刚度", source_ref="第1页 表1 第7行",
                       metrics=[{"quantity": "施加力值", "op": "=", "value": 240.0,
                                 "unit": "N", "raw": "240N"}])]
    ai = [_ai_item(title="安装卡接孔刚度", source_ref="第1页 表1 第7行",
                   metrics=[{"quantity": "施加力值", "op": "=", "value": 36.0,
                             "unit": "N", "raw": "36N"}])]
    merged, stats = merge_rule_and_ai(rule, ai)
    assert stats["conflicts"] == 1 and stats["agreed"] == 0
    assert merged[0]["needs_clarification"] is True
    hint = merged[0]["clarification_hint"]
    assert "240" in hint and "36" in hint and "规则/AI 读数不一致" in hint


def test_merge_kind_mismatch_is_not_a_conflict():
    """规则把 50mm 归为 T29 例外、AI 归为主值——归类差异不是读数矛盾。"""
    rule = [_rule_item(title="IP系统膝碰", source_ref="第1页 表1 第2行",
                       metrics=[{"quantity": "侵入量", "op": "", "value": 35.0,
                                 "unit": "mm", "raw": "", "kind": "target"},
                                {"quantity": "侵入量", "op": "", "value": 50.0,
                                 "unit": "mm", "raw": "", "kind": "override"}])]
    ai = [_ai_item(title="IP系统膝碰", source_ref="第1页 表1 第2行",
                   metrics=[{"quantity": "侵入量", "op": "≤", "value": 50.0,
                             "unit": "mm", "raw": "", "kind": "target"}])]
    _, stats = merge_rule_and_ai(rule, ai)
    assert stats["conflicts"] == 0 and stats["agreed"] == 1


def test_merge_falls_back_to_page_and_title_then_keeps_rule_only():
    """工况条目只有页码锚点,按同页+归一化标题对齐;两边都没有的规则条目兜底保留。"""
    rule = [
        _rule_item(title="IP系统静态头碰", category="loading",
                   source_ref="第4页", load_points=6),
        _rule_item(title="手套箱(打开)", category="loading",
                   source_ref="第9页", load_points=2),      # AI 漏掉的页
    ]
    ai = [_ai_item(title="IP系统静态头碰测试", category="loading",
                   source_ref="第 4 页", load_points=6),
          _ai_item(title="仪表板下体膝碰", category="loading",
                   source_ref="第12页", load_points=4)]     # 规则没抽到的
    merged, stats = merge_rule_and_ai(rule, ai)
    assert stats == {"ruleTotal": 2, "aiTotal": 2, "agreed": 1,
                     "conflicts": 0, "ruleOnly": 1, "aiOnly": 1}
    fallback = [i for i in merged if i["extracted_by"] == "rule"]
    assert len(fallback) == 1 and fallback[0]["title"] == "手套箱(打开)"


def test_ai_writeback_cross_checks_with_rule_extraction(client, project, tmp_path):
    """AI 回写时服务端独立跑规则抽取:冲突转待澄清、AI 漏页由规则兜底。"""
    doc = _upload_pptx(client, project, tmp_path)
    r = client.put(
        f"/sim/requirements/{doc['id']}/items",
        json={"extractor": "ai", "items": [
            # 与规则同表行,但变形量读数不同(规则读 ≤1.0mm)→ 应转待澄清
            {"category": "subject", "title": "IP系统表面刚度",
             "raw_text": "直径50mm;90N,变形量≤2.0mm", "baseline": "required",
             "source_ref": "第1页 表1 第1行",
             "metrics": [{"quantity": "变形量", "op": "≤", "value": 2.0,
                          "unit": "mm", "raw": "变形量≤2.0mm"}]},
            # 规则没有的条目,原样保留
            {"category": "loading", "title": "仪表板下体膝碰",
             "source_ref": "第5页", "load_points": 4},
            # 注意:没有回写第2页工况 → 模拟视觉失败页,应由规则兜底
        ], "summary": {"notes": ["第2页解析失败"]}},
        headers=hdr(),
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["summary"]["merge"] == {
        "ruleTotal": 2, "aiTotal": 2, "agreed": 0,
        "conflicts": 1, "ruleOnly": 1, "aiOnly": 1,
    }
    # notes 与合并统计并存
    assert out["summary"]["notes"] == ["第2页解析失败"]

    items = out["items"]
    assert len(items) == 3
    stiff = [i for i in items if i["source_ref"] == "第1页 表1 第1行"][0]
    assert stiff["extracted_by"] == "ai" and stiff["needs_clarification"]
    assert "1.0" in stiff["clarification_hint"] and "2.0" in stiff["clarification_hint"]
    # 待澄清计入统计口径
    assert out["summary"]["needsClarification"] >= 1

    fallback = [i for i in items if i["extracted_by"] == "rule"]
    assert len(fallback) == 1 and fallback[0]["source_ref"] == "第2页"
    assert fallback[0]["load_points"] == 2


def test_rule_writeback_and_unparseable_docs_skip_cross_check(client, project, tmp_path):
    """规则回写不自我核对;pdf 这类规则抽不了的格式,AI 回写照常落库、只是没有 merge。"""
    doc = _upload_pptx(client, project, tmp_path)
    r = client.put(f"/sim/requirements/{doc['id']}/items",
                   json={"extractor": "manual",
                         "items": [{"category": "subject", "title": "人工登记项"}]},
                   headers=hdr())
    assert r.status_code == 200 and "merge" not in r.json()["summary"]

    pdf = upload_req(client, project, name="spec.pdf", data=b"%PDF-1.4").json()
    r2 = client.put(f"/sim/requirements/{pdf['id']}/items",
                    json={"extractor": "ai",
                          "items": [{"category": "subject", "title": "AI 抽的"}]},
                    headers=hdr())
    assert r2.status_code == 200 and "merge" not in r2.json()["summary"]
    assert len(r2.json()["items"]) == 1


# --- 质量卡导入与在线编辑 -----------------------------------------------

def _builtin_bytes():
    from app.sim.quality.library import BUILTIN_DIR, CRITERIA_FILE, MESH_FILE

    base = os.path.join(BUILTIN_DIR, "generic-crash-5mm")
    with open(os.path.join(base, CRITERIA_FILE), "rb") as f:
        qual = f.read()
    with open(os.path.join(base, MESH_FILE), "rb") as f:
        mpar = f.read()
    return qual, mpar


def test_import_quality_template(client):
    qual, mpar = _builtin_bytes()
    r = client.post(
        "/sim/quality-templates/import",
        files={"qual_file": ("cust.ansa_qual", io.BytesIO(qual), "text/plain"),
               "mpar_file": ("cust.ansa_mpar", io.BytesIO(mpar), "text/plain")},
        data={"template_id": "cust-a", "name": "客户A 网格卡", "source": "客户A"},
        headers=hdr(),
    )
    assert r.status_code == 201, r.text
    assert r.json()["ansaVersion"] == "19.1.1"
    assert len(r.json()["criteria"]) == 11
    assert "cust-a" in {t["id"] for t in client.get("/sim/quality-templates", headers=hdr()).json()}


def test_import_without_mpar_inherits_and_says_so(client):
    """有些客户只给判定准则。沿用内置生成侧参数并写明——塞空文件会让人
    以为客户规定了这些参数。"""
    qual, _ = _builtin_bytes()
    r = client.post(
        "/sim/quality-templates/import",
        files={"qual_file": ("only.ansa_qual", io.BytesIO(qual), "text/plain")},
        data={"template_id": "only-qual", "name": "只给判据"},
        headers=hdr(),
    )
    assert r.status_code == 201, r.text
    assert "沿用内置模板" in r.json()["description"]
    assert r.json()["meshParams"]["targetElementLength"] == 5.0


def test_import_rejects_garbage_and_duplicate_id(client):
    r = client.post(
        "/sim/quality-templates/import",
        files={"qual_file": ("x.ansa_qual", io.BytesIO(b"not a quality card"), "text/plain")},
        data={"template_id": "junk", "name": "垃圾"},
        headers=hdr(),
    )
    assert r.status_code == 400 and "没有解析出任何判据" in r.json()["detail"]

    qual, _ = _builtin_bytes()
    client.post("/sim/quality-templates/import",
                files={"qual_file": ("a.ansa_qual", io.BytesIO(qual), "text/plain")},
                data={"template_id": "dup", "name": "A"}, headers=hdr())
    r2 = client.post("/sim/quality-templates/import",
                     files={"qual_file": ("a.ansa_qual", io.BytesIO(qual), "text/plain")},
                     data={"template_id": "dup", "name": "A2"}, headers=hdr())
    assert r2.status_code == 409


def _import_tpl(client, tid="cust-m", user="u"):
    qual, _ = _builtin_bytes()
    return client.post(
        "/sim/quality-templates/import",
        files={"qual_file": ("a.ansa_qual", io.BytesIO(qual), "text/plain")},
        data={"template_id": tid, "name": "客户卡"},
        headers=hdr(user),
    )


def test_template_management_only_by_importer_or_admin(client):
    """模板是全局共享资产:内置不可动;用户模板只有导入者或管理员能删改。"""
    assert _import_tpl(client, user="u").status_code == 201

    # 内置模板:改/删一律 403
    assert client.patch("/sim/quality-templates/generic-crash-5mm",
                        json={"name": "偷改"}, headers=hdr("root")).status_code == 403
    assert client.delete("/sim/quality-templates/generic-crash-5mm",
                         headers=hdr("root")).status_code == 403

    # 他人不可删改,导入者可以
    assert client.patch("/sim/quality-templates/cust-m", json={"name": "x"},
                        headers=hdr("other")).status_code == 403
    assert client.delete("/sim/quality-templates/cust-m",
                         headers=hdr("other")).status_code == 403
    r = client.patch("/sim/quality-templates/cust-m",
                     json={"name": "客户卡 v2", "scope": "碰撞"}, headers=hdr("u"))
    assert r.status_code == 200 and r.json()["name"] == "客户卡 v2"
    assert client.delete("/sim/quality-templates/cust-m", headers=hdr("u")).status_code == 204
    assert client.get("/sim/quality-templates/cust-m", headers=hdr("u")).status_code == 404

    # 管理员可删他人的模板
    _import_tpl(client, tid="cust-n", user="u")
    assert client.delete("/sim/quality-templates/cust-n", headers=hdr("root")).status_code == 204
    assert client.delete("/sim/quality-templates/nope", headers=hdr("u")).status_code == 404


def test_edit_template_content_online(client):
    """用户模板内容可在线改:每项必须带依据,留痕追加,权限同删改元数据。"""
    _import_tpl(client, tid="cust-edit", user="u")
    ov = {"target": "criteria:warping [shells]:failed", "new_value": "12"}

    # 无依据 → 400;他人 → 403;内置 → 403
    r = client.patch("/sim/quality-templates/cust-edit/content",
                     json={"overrides": [ov]}, headers=hdr("u"))
    assert r.status_code == 400 and "依据" in r.json()["detail"]
    assert client.patch("/sim/quality-templates/cust-edit/content",
                        json={"overrides": [{**ov, "source": "评审"}]},
                        headers=hdr("other")).status_code == 403
    assert client.patch("/sim/quality-templates/generic-crash-5mm/content",
                        json={"overrides": [{**ov, "source": "评审"}]},
                        headers=hdr("root")).status_code == 403

    # 导入者可改;返回刷新后的卡与全部留痕
    r = client.patch("/sim/quality-templates/cust-edit/content",
                     json={"overrides": [{**ov, "source": "评审结论"},
                                         {"target": "mesh:target_element_length",
                                          "new_value": "4", "source": "评审结论"}]},
                     headers=hdr("u"))
    assert r.status_code == 200, r.text
    warping = next(c for c in r.json()["criteria"] if c["name"] == "warping")
    assert warping["thresholds"]["failed"] == 12.0
    assert r.json()["meshParams"]["targetElementLength"] == 4.0
    assert [o["old_value"] for o in r.json()["overrides"]] == ["15.0", "5."]

    # 无效目标 → 400,且不会写半张卡
    assert client.patch("/sim/quality-templates/cust-edit/content",
                        json={"overrides": [{"target": "mesh:no_such", "new_value": "1",
                                             "source": "x"}]},
                        headers=hdr("u")).status_code == 400
    d = client.get("/sim/quality-templates/cust-edit", headers=hdr("u")).json()
    assert d["meshParams"]["targetElementLength"] == 4.0


def test_derive_template_makes_builtin_editable(client):
    """内置模板只读,但可以以它为底座派生一张用户模板来"改"它。"""
    r = client.post("/sim/quality-templates/generic-crash-5mm/derive",
                    json={"new_id": "my-crash-4mm", "name": "我们的 4mm 碰撞卡",
                          "overrides": [{"target": "mesh:target_element_length",
                                         "new_value": "4", "source": "内部评审"}]},
                    headers=hdr("u"))
    assert r.status_code == 201, r.text
    assert r.json()["based_on"] == "generic-crash-5mm"
    assert r.json()["created_by"] == "u"
    assert r.json()["overrides"][0]["old_value"] == "5."

    # 派生出的是普通用户模板:导入者可继续在线改内容
    assert client.patch("/sim/quality-templates/my-crash-4mm/content",
                        json={"overrides": [{"target": "mesh:general_min_target_len",
                                             "new_value": "2.5", "source": "评审"}]},
                        headers=hdr("u")).status_code == 200

    # 重复 id → 409;坏 id → 400;底座不存在 → 404
    assert client.post("/sim/quality-templates/generic-crash-5mm/derive",
                       json={"new_id": "my-crash-4mm", "name": "x"},
                       headers=hdr("u")).status_code == 409
    assert client.post("/sim/quality-templates/generic-crash-5mm/derive",
                       json={"new_id": "非法id", "name": "x"},
                       headers=hdr("u")).status_code == 400
    assert client.post("/sim/quality-templates/nope/derive",
                       json={"new_id": "whatever", "name": "x"},
                       headers=hdr("u")).status_code == 404


def test_template_list_reports_usage_and_deletion_keeps_instances(client, project):
    """used_by 只提示影响面:实例文件派生时已拷走,删模板不碰既有实例。"""
    _import_tpl(client, tid="cust-used")
    card = client.post(f"/sim/projects/{project}/quality-cards",
                       json={"name": "实例", "template_id": "cust-used"},
                       headers=hdr()).json()

    rows = {t["id"]: t for t in client.get("/sim/quality-templates", headers=hdr()).json()}
    assert rows["cust-used"]["used_by"] == 1
    assert rows["cust-used"]["created_by"] == "u"
    assert rows["generic-crash-5mm"]["used_by"] == 0

    assert client.delete("/sim/quality-templates/cust-used", headers=hdr()).status_code == 204
    detail = client.get(f"/sim/quality-cards/{card['id']}", headers=hdr()).json()
    assert detail["card"]["criteria"], "模板删了,实例的卡文件必须还在"


def test_edit_card_online_writes_into_ansa_files(client, project):
    card = client.post(f"/sim/projects/{project}/quality-cards",
                       json={"name": "在线编辑卡"}, headers=hdr()).json()
    r = client.patch(
        f"/sim/quality-cards/{card['id']}",
        json={"overrides": [
            {"target": "criteria:warping [shells]:failed", "new_value": "10.0", "source": "评审结论"},
            {"target": "mesh:target_element_length", "new_value": "6.", "source": "评审结论"},
        ]},
        headers=hdr(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["card"]["meshParams"]["targetElementLength"] == 6.0
    warp = [c for c in r.json()["card"]["criteria"] if c["name"] == "warping"][0]
    assert warp["thresholds"]["failed"] == 10.0
    assert len(r.json()["overrides"]) == 2
    assert all(o["source"] for o in r.json()["overrides"])

    # 编辑完仍是一份可直接交回 ANSA 的合法卡
    mpar = client.get(f"/sim/quality-cards/{card['id']}/export",
                      params={"kind": "mpar"}, headers=hdr())
    assert "target_element_length   = 6." in mpar.text


def test_edit_card_requires_source(client, project):
    card = client.post(f"/sim/projects/{project}/quality-cards",
                       json={"name": "卡"}, headers=hdr()).json()
    r = client.patch(f"/sim/quality-cards/{card['id']}",
                     json={"overrides": [{"target": "mesh:target_element_length",
                                          "new_value": "6."}]},
                     headers=hdr())
    assert r.status_code == 400 and "依据" in r.json()["detail"]


def test_override_survives_when_same_number_appears_elsewhere():
    """真实文档第 7 页的原文：同一个 50mm 在正文里先出现过一次。

    去重若按字符串跨段比较，括号里的项目例外「（T29为50mm）」会被当成重复
    丢掉——整份文档最关键的一条项目级覆盖就此静默消失。去重只能按本段内的
    位置算。这条曾经真的漏过。
    """
    text = "上50mm区域 下上50mm区域 F≤3000N，侵入量35mm（T29为50mm） *坐标标注两个刻度，35mm一个，50mm一个"
    override = [m for m in extract_metrics(text) if m.kind == "override"]
    assert override and override[0].value == 50.0 and override[0].scope == "T29"
