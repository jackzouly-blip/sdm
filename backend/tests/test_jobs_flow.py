"""任务库 + 列表/详情 API 集成测试（不依赖真实 qstat）。

通过直接签发 JWT 绕过 PAM，单独验证任务库写入、消失即 done、按属主隔离。
"""
import tempfile
from pathlib import Path

import pytest

from app.db.jobs_db import ACTIVE, DONE, JobsDB
from app.pbs.parser import parse_jobs

FIXTURE = Path(__file__).parent / "fixtures" / "qstat_f_1785.txt"


@pytest.fixture(autouse=True)
def _admins(restrict_admins):
    """启用管理员白名单（定义见 conftest）。

    不配置时 is_admin() 对所有人返回 True，"他人访问应 403"会变成 200——
    这条断言等于没测。
    """


def _make_db() -> JobsDB:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return JobsDB(tmp.name)


def test_upsert_and_owner_filter():
    db = _make_db()
    jobs = parse_jobs(FIXTURE.read_text())
    db.upsert_active(jobs)

    rows = db.list_by_owner("user07")
    assert len(rows) == 1
    assert rows[0]["jobid"] == "1785.hpcmaster"
    assert rows[0]["derived_state"] == ACTIVE
    assert rows[0]["workdir"].endswith("DR_PA/case2")

    # 其他用户看不到
    assert db.list_by_owner("someoneelse") == []
    db.close()


def test_disappear_marks_done():
    db = _make_db()
    jobs = parse_jobs(FIXTURE.read_text())
    db.upsert_active(jobs)
    assert db.get("1785.hpcmaster")["derived_state"] == ACTIVE

    # 下一轮 qstat 中该任务消失（传入空列表）-> 应标记 done
    db.upsert_active([])
    row = db.get("1785.hpcmaster")
    assert row["derived_state"] == DONE
    assert row["end_ts"] is not None
    db.close()


def test_api_list_and_detail():
    from fastapi.testclient import TestClient

    from app.auth.session import issue_token
    from app.main import app

    db = _make_db()
    db.upsert_active(parse_jobs(FIXTURE.read_text()))

    with TestClient(app) as client:
        # 用注入的库替换轮询器初始化的库
        app.state.jobs_db = db
        token = issue_token("user07")
        h = {"Authorization": f"Bearer {token}"}

        r = client.get("/jobs", headers=h)
        assert r.status_code == 200, r.text
        data = r.json()
        assert len(data) == 1
        assert data[0]["short_id"] == "1785"

        r = client.get("/jobs/1785.hpcmaster", headers=h)
        assert r.status_code == 200
        assert r.json()["workdir"].endswith("DR_PA/case2")

        # 越权访问：以别的用户身份取该任务 -> 403
        other = issue_token("intruder")
        r = client.get(
            "/jobs/1785.hpcmaster", headers={"Authorization": f"Bearer {other}"}
        )
        assert r.status_code == 403, r.text

        # 未登录 -> 401
        assert client.get("/jobs").status_code == 401
    db.close()


if __name__ == "__main__":
    import sys

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"ERROR {name}: {e!r}")
    sys.exit(1 if failed else 0)
