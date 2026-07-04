"""提交接口接入本地排队队列后的集成测试（HTTP 层，不依赖真实 qstat/qsub）。

以当前系统用户身份提交(参照 test_packaging.py 的做法)，这样 stat_path/write_file
的降权逻辑判定"已经是目标用户"而无需 root；qsub 调用统一在 scheduler 模块内被
monkeypatch 成假实现。
"""
import getpass
import os
import tempfile
import time

from fastapi.testclient import TestClient

from app.auth.session import issue_token
from app.config import get_settings
from app.db.jobs_db import JobsDB
from app.pbs.parser import Job
from app.submit import scheduler as scheduler_mod


def _fake_job(jobid: str, owner: str, state: str = "R", nodes: str = "1:ppn=4") -> Job:
    now = time.time()
    return Job(
        jobid=jobid, name="j", owner=owner, state=state, queue="batch",
        workdir="/tmp", exec_host="node1", submit_ts=now, start_ts=now,
        end_ts=None, walltime_used=None, walltime_limit=None, nodes=nodes,
        exit_status=None, raw={},
    )


class _FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_qsub(jobid="1000.hpcmaster"):
    def fake_run_as_user(username, argv, cwd=None, timeout=None):
        return _FakeProc(returncode=0, stdout=f"{jobid}\n".encode())
    scheduler_mod.run_as_user = fake_run_as_user


def _setup_workdir() -> str:
    root = tempfile.mkdtemp(prefix="subq_")
    job_dir = os.path.join(root, "job1")
    os.makedirs(job_dir)
    with open(os.path.join(job_dir, "case.k"), "w") as f:
        f.write("*KEYWORD\n")
    get_settings().fs_roots = root
    return job_dir


def _swap_db(app) -> JobsDB:
    """给测试注入一个独立的临时库，jobs_db 和调度器都要指向同一个实例。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = JobsDB(tmp.name)
    app.state.jobs_db = db
    app.state.scheduler.db = db
    return db


def _submit_body(job_dir: str, name: str = "job1") -> dict:
    return {
        "name": name,
        "cores": 4,
        "init_dir": job_dir,
        "input_file": "case.k",
        "script": "#!/bin/bash\n#PBS -l nodes=1:ppn=1\necho hi\n",
    }


def test_default_user_submitted_immediately():
    """无策略、无全局核数上限：提交应在本次请求内直接进入 PBS(与旧行为一致)。"""
    job_dir = _setup_workdir()
    get_settings().cluster_total_cores = 0
    # 普通用户路径(非管理员)：exec_user=自己，无需真实降权即可跑通 stat/write。
    get_settings().admin_users = "zzz_admin_placeholder"
    _patch_qsub(jobid="1001.hpcmaster")

    from app.main import app

    with TestClient(app) as client:
        db = _swap_db(app)
        me = getpass.getuser()
        h = {"Authorization": f"Bearer {issue_token(me)}"}

        r = client.post("/jobs/submit", json=_submit_body(job_dir), headers=h)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "submitted"
        assert data["jobid"] == "1001.hpcmaster"
        db.close()
    get_settings().admin_users = ""


def test_quota_holds_then_frees_and_cancel():
    """配额占满时本地排队展示在任务列表；可撤回；名额空出后立即补位。"""
    job_dir = _setup_workdir()
    get_settings().cluster_total_cores = 0
    get_settings().admin_users = "zzz_admin_placeholder"
    _patch_qsub()

    from app.main import app

    with TestClient(app) as client:
        db = _swap_db(app)
        me = getpass.getuser()
        db.policy_upsert(me, max_concurrent=1, priority=5)
        db.upsert_active([_fake_job("1.hpcmaster", me)])  # 已占满配额

        h = {"Authorization": f"Bearer {issue_token(me)}"}
        r = client.post("/jobs/submit", json=_submit_body(job_dir, "job2"), headers=h)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "queued"
        qid = data["queue_id"]

        # 展示在任务列表里，标记为本地排队
        r = client.get("/jobs", headers=h)
        matched = [x for x in r.json() if x.get("queue_id") == qid]
        assert len(matched) == 1
        assert matched[0]["derived_state"] == "queued_local"

        # 撤回排队
        r = client.post(f"/jobs/queue/{qid}/cancel", headers=h)
        assert r.status_code == 200, r.text
        assert db.sq_get(qid)["status"] == "cancelled"

        # 原任务结束，名额空出 -> 新排队项应立即被准入
        db.upsert_active([])
        qid2 = db.sq_enqueue(me, "job3", "batch", "/tmp/x.pbs", "/tmp", cores=1)
        admitted = app.state.scheduler.tick()
        assert admitted == 1
        assert db.sq_get(qid2)["status"] == "submitted"
        db.close()
    get_settings().admin_users = ""


def test_admin_policy_crud():
    job_dir = _setup_workdir()
    from app.main import app

    with TestClient(app) as client:
        db = _swap_db(app)
        me = getpass.getuser()
        h = {"Authorization": f"Bearer {issue_token(me)}"}

        r = client.put(
            "/admin/user-policies/user07",
            json={"max_concurrent": 2, "priority": 10},
            headers=h,
        )
        assert r.status_code == 200, r.text
        assert r.json()["max_concurrent"] == 2

        r = client.get("/admin/user-policies", headers=h)
        assert any(p["user"] == "user07" for p in r.json())

        r = client.delete("/admin/user-policies/user07", headers=h)
        assert r.status_code == 200, r.text

        r = client.get("/admin/user-policies", headers=h)
        assert all(p["user"] != "user07" for p in r.json())
        db.close()
    # job_dir 未在这个用例里实际用到提交，仅复用 fixture 建好 fs_roots


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
