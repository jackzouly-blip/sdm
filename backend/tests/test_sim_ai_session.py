"""AI 会话：会话/消息 CRUD、项目级只读票据、ai-context 快照。

契约见 docs/vektor3d-ai-session-contract.md。这里守住三条底线：
  - 消息流是主数据：落库、按 seq 排序、随项目属主隔离；
  - ai.read 票据只在枚举的只读接口上有效，其余一律 401、跨项目 403、不能续签；
  - 提案只能"翻状态"，不存在任何借会话写主数据的口子。
"""
import io
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


def thdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def project(client):
    return client.post("/sim/projects", json={"name": "X90座椅碰撞"}, headers=hdr()).json()["id"]


def upload_req(client, pid, name="技术协议.pdf", data=b"%PDF-1.4 fake"):
    return client.post(
        f"/sim/projects/{pid}/requirements/upload",
        files={"file": (name, io.BytesIO(data), "application/pdf")},
        data={"doc_type": "agreement"},
        headers=hdr(),
    )


# --- 会话与消息 ----------------------------------------------------------

def test_session_and_message_flow(client, project):
    """建会话 → 追加 user/assistant 消息 → 详情按 seq 回放。消息流是主数据。"""
    s = client.post(f"/sim/projects/{project}/ai-sessions",
                    json={"title": "澄清压头直径"}, headers=hdr())
    assert s.status_code == 201, s.text
    sid = s.json()["id"]
    assert s.json()["created_by"] == "u"

    m1 = client.post(f"/sim/ai-sessions/{sid}/messages",
                     json={"role": "user", "content": "第5条的压头直径到底是多少?"},
                     headers=hdr())
    assert m1.status_code == 201
    m2 = client.post(f"/sim/ai-sessions/{sid}/messages",
                     json={"role": "assistant", "content": "文档写的是 165mm",
                           "citations": [{"text": "Φ165", "ref": "第5页 表2"}]},
                     headers=hdr())
    assert m2.status_code == 201

    detail = client.get(f"/sim/ai-sessions/{sid}", headers=hdr()).json()
    assert [m["seq"] for m in detail["messages"]] == [1, 2]
    assert detail["messages"][1]["citations"][0]["ref"] == "第5页 表2"

    rows = client.get(f"/sim/projects/{project}/ai-sessions", headers=hdr()).json()
    assert [r["id"] for r in rows] == [sid]

    # 空消息拒绝
    assert client.post(f"/sim/ai-sessions/{sid}/messages",
                       json={"role": "user", "content": "  "},
                       headers=hdr()).status_code == 400


def test_session_isolated_by_project_owner(client, project):
    sid = client.post(f"/sim/projects/{project}/ai-sessions",
                      json={}, headers=hdr()).json()["id"]
    assert client.get(f"/sim/ai-sessions/{sid}", headers=hdr("intruder")).status_code == 404
    assert client.post(f"/sim/ai-sessions/{sid}/messages",
                       json={"role": "user", "content": "x"},
                       headers=hdr("intruder")).status_code == 404
    assert client.delete(f"/sim/ai-sessions/{sid}", headers=hdr("intruder")).status_code == 404
    # 属主可删,消息级联清掉
    assert client.delete(f"/sim/ai-sessions/{sid}", headers=hdr()).status_code == 204
    assert client.get(f"/sim/ai-sessions/{sid}", headers=hdr()).status_code == 404


# --- 提案:枚举、依据强制、裁决一次性 ------------------------------------

def test_proposal_validation_and_decision(client, project):
    sid = client.post(f"/sim/projects/{project}/ai-sessions",
                      json={}, headers=hdr()).json()["id"]
    ok_proposal = {
        "action": "requirement_item.update", "targetId": "item-1",
        "patch": {"needs_clarification": False},
        "reason": "客户邮件确认", "evidence": [{"ref": "第5页 表2"}],
    }

    # 不在枚举里的动作 → 400;缺 reason → 400
    assert client.post(f"/sim/ai-sessions/{sid}/messages",
                       json={"role": "assistant", "content": "x",
                             "proposals": [{**ok_proposal, "action": "project.delete"}]},
                       headers=hdr()).status_code == 400
    assert client.post(f"/sim/ai-sessions/{sid}/messages",
                       json={"role": "assistant", "content": "x",
                             "proposals": [{**ok_proposal, "reason": " "}]},
                       headers=hdr()).status_code == 400

    # 合法提案:服务端置 pending,不信调用方塞的状态
    m = client.post(f"/sim/ai-sessions/{sid}/messages",
                    json={"role": "assistant", "content": "建议解除待澄清",
                          "proposals": [{**ok_proposal, "status": "confirmed"}]},
                    headers=hdr()).json()
    assert m["proposals"][0]["status"] == "pending"

    # 裁决一次有效,重复裁决 409;裁决人与时间落库
    r = client.post(f"/sim/ai-sessions/{sid}/messages/{m['id']}/proposal-decision",
                    json={"index": 0, "decision": "confirmed", "note": "已核对"},
                    headers=hdr())
    assert r.status_code == 200
    assert r.json()["proposals"][0]["status"] == "confirmed"
    assert r.json()["proposals"][0]["decided_by"] == "u"
    assert client.post(f"/sim/ai-sessions/{sid}/messages/{m['id']}/proposal-decision",
                       json={"index": 0, "decision": "rejected"},
                       headers=hdr()).status_code == 409
    # 越界 index → 404
    assert client.post(f"/sim/ai-sessions/{sid}/messages/{m['id']}/proposal-decision",
                       json={"index": 5, "decision": "confirmed"},
                       headers=hdr()).status_code == 404


# --- ai.read 票据:白名单、绑定、不可续签 --------------------------------

def _ticket(client, pid, user="u"):
    r = client.post(f"/sim/projects/{pid}/ai-read-ticket", headers=hdr(user))
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_ai_read_ticket_whitelist_and_binding(client, project):
    doc = upload_req(client, project, data=b"REQ-BYTES").json()
    tok = _ticket(client, project)

    # 白名单内:项目快照、文档下载
    ctx = client.get(f"/sim/projects/{project}/ai-context", headers=thdr(tok))
    assert ctx.status_code == 200, ctx.text
    dl = client.get(f"/sim/requirements/{doc['id']}/download", headers=thdr(tok))
    assert dl.status_code == 200 and dl.content == b"REQ-BYTES"

    # 白名单外:一律 401(默认拒绝,不是逐个排除)
    assert client.get("/sim/projects", headers=thdr(tok)).status_code == 401
    assert client.get(f"/sim/projects/{project}/ai-sessions",
                      headers=thdr(tok)).status_code == 401
    assert client.delete(f"/sim/requirements/{doc['id']}",
                         headers=thdr(tok)).status_code == 401

    # 跨项目 403
    pid2 = client.post("/sim/projects", json={"name": "另一项目"},
                       headers=hdr()).json()["id"]
    assert client.get(f"/sim/projects/{pid2}/ai-context",
                      headers=thdr(tok)).status_code == 403

    # 票据不能再签票据(自我续签)
    assert client.post(f"/sim/projects/{project}/ai-read-ticket",
                       headers=thdr(tok)).status_code == 401


def test_ai_read_ticket_requires_project_owner(client, project):
    assert client.post(f"/sim/projects/{project}/ai-read-ticket",
                       headers=hdr("intruder")).status_code == 404


# --- ai-context:内容 hash 随变更而变 -------------------------------------

def test_ai_context_hash_tracks_changes(client, project):
    upload_req(client, project)
    ctx1 = client.get(f"/sim/projects/{project}/ai-context", headers=hdr()).json()
    assert ctx1["project"]["id"] == project
    assert ctx1["requirementDocs"][0]["hash"], "文档要带内容 hash 供 manifest 判缓存"
    assert ctx1["requirementDocs"][0]["downloadPathSuffix"].endswith("/download")

    # 加一条工况 → subjects 节 hash 变化,其余节不变
    r = client.post(f"/sim/projects/{project}/subjects",
                    json={"name": "正碰", "subject_type": "crash",
                          "solver_type": "lsdyna"}, headers=hdr())
    assert r.status_code == 201, r.text
    ctx2 = client.get(f"/sim/projects/{project}/ai-context", headers=hdr()).json()
    assert ctx2["subjects"]["hash"] != ctx1["subjects"]["hash"]
    assert ctx2["requirementItems"]["hash"] == ctx1["requirementItems"]["hash"]
