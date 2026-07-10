"""试算（管理节点直跑，不进 PBS）接口的集成测试（HTTP 层）。

以当前系统用户身份跑：popen 的降权逻辑判定"已经是目标用户"而无需 root。
命令走 bash，故本机需有 /bin/bash（CI/开发机均满足）。
"""
import getpass
import os
import tempfile
import time

from fastapi.testclient import TestClient

from app.auth.session import issue_token
from app.config import get_settings
from app.db.jobs_db import JobsDB
from app.trial.manager import TrialManager, set_trial_manager


def _setup_workdir() -> str:
    root = tempfile.mkdtemp(prefix="trial_")
    job_dir = os.path.join(root, "job1")
    os.makedirs(job_dir)
    with open(os.path.join(job_dir, "case.k"), "w") as f:
        f.write("*KEYWORD\n")
    get_settings().fs_roots = root
    return job_dir


def test_finish_writes_status_file():
    """EXIT 陷阱回归：试算结束后必须落盘退出码 status 文件（供重启恢复），
    正常结束写 0、失败写非 0。此前偶发未写出会导致完成任务被误判中断。"""
    d = tempfile.mkdtemp()
    db = JobsDB(os.path.join(d, "t.db"))
    tm = TrialManager(db, max_concurrent=2, interval=1)
    tm.start()
    me = getpass.getuser()
    wd = tempfile.mkdtemp()

    tid = tm.start_trial(me, "ok", wd, "echo hi")
    time.sleep(1.5)
    row = db.trial_get(tid)
    assert row["status"] == "finished" and row["exit_code"] == 0
    with open(row["status_path"]) as f:
        assert f.read().strip() == "0", "正常结束未写出 status=0"

    tid2 = tm.start_trial(me, "bad", wd, "exit 5")
    time.sleep(1.5)
    row2 = db.trial_get(tid2)
    assert row2["status"] == "failed" and row2["exit_code"] == 5
    with open(row2["status_path"]) as f:
        assert f.read().strip() == "5", "失败未写出 status=5"
    tm.stop()
    db.close()


def test_reaper_skips_starting_row_without_pid():
    """启动竞态回归：一条 running 但尚未回填 pid 的"启动中"行，收割器绝不能把它
    误判成 interrupted（此前的现网 bug：进程其实在跑，库里却被标中断）。"""
    d = tempfile.mkdtemp()
    db = JobsDB(os.path.join(d, "t.db"))
    tm = TrialManager(db, max_concurrent=2, interval=1)
    # 模拟 start_trial 刚 trial_create 完、还没 Popen/回填 pid 的中间态
    tid = db.trial_create(
        "someone", "half", "/tmp", "sleep 1",
        os.path.join(d, "x.log"), os.path.join(d, "x.status"),
    )
    row = db.trial_get(tid)
    assert row["status"] == "running" and row["pid"] is None
    # 收割一轮：不得动它
    tm._reap()
    assert db.trial_get(tid)["status"] == "running", "启动中行被误判了！"
    # 回填 pid 后（且无句柄、进程确实不存在）才允许判终态
    db.trial_set_started(tid, 999999, 999999)  # 不存在的 pid
    tm._reap()
    assert db.trial_get(tid)["status"] in ("interrupted", "failed", "finished")
    db.close()


def _swap_db(app) -> JobsDB:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = JobsDB(tmp.name)
    app.state.jobs_db = db
    # 试算管理器指向同一个库；用短收割间隔加快测试
    tm = TrialManager(db, max_concurrent=2, interval=1)
    set_trial_manager(tm)
    app.state.trial_manager = tm
    tm.start()
    return db


def _make_template(app, content: str, kind: str = "trial") -> int:
    """直接经 TemplatesDB 建模板（避免走需要 admin 的 HTTP 接口，从而让提交端
    以非管理员=自己身份跑，无需真实 root 降权）。"""
    row = app.state.templates_db.create("tpl", content, kind)
    return row["id"]


def _wait_status(client, headers, tid: int, target: set, timeout=8.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        r = client.get(f"/trials/{tid}/output", headers=headers, params={"offset": 0})
        assert r.status_code == 200, r.text
        last = r.json()
        if last["status"] in target:
            return last
        time.sleep(0.3)
    raise AssertionError(f"试算未在预期时间内到达 {target}，最后状态={last.get('status')}")


def test_trial_runs_and_captures_output():
    """提交试算 → 直接在管理节点跑 → 拿到输出与退出码；列表混入 done 行。"""
    job_dir = _setup_workdir()
    me = getpass.getuser()
    # 非管理员=以自己身份跑，stat/popen 判定"已是目标用户"，无需真实 root。
    get_settings().admin_users = "zzz_admin_placeholder"

    from app.main import app

    with TestClient(app) as client:
        db = _swap_db(app)
        h = {"Authorization": f"Bearer {issue_token(me)}"}
        # 命令引用占位符，验证渲染替换生效
        tid_tpl = _make_template(app, "echo START ###input_file_path###; echo DONE")

        r = client.post(
            "/trials/submit",
            json={
                "name": "demo",
                "init_dir": job_dir,
                "input_file": "case.k",
                "template_id": tid_tpl,
            },
            headers=h,
        )
        assert r.status_code == 200, r.text
        tid = r.json()["id"]

        final = _wait_status(client, h, tid, {"finished"})
        assert final["exit_code"] == 0
        assert "START case.k" in final["data"]
        assert "DONE" in final["data"]

        # 混入 /jobs 列表：done 过滤下应出现该试算行
        r = client.get("/jobs", headers=h, params={"state": "done"})
        rows = [x for x in r.json() if x["jobid"] == f"trial:{tid}"]
        assert len(rows) == 1
        assert rows[0]["derived_state"] == "trial_done"
        db.close()
    get_settings().admin_users = ""


def test_trial_cancel_interrupts_process():
    """长命令试算可被中断（killpg），状态转为 killed，并从活跃列表移除运行态。"""
    job_dir = _setup_workdir()
    me = getpass.getuser()
    get_settings().admin_users = "zzz_admin_placeholder"

    from app.main import app

    with TestClient(app) as client:
        db = _swap_db(app)
        h = {"Authorization": f"Bearer {issue_token(me)}"}
        tid_tpl = _make_template(app, "echo GO; sleep 60; echo NEVER")

        r = client.post(
            "/trials/submit",
            json={
                "name": "long",
                "init_dir": job_dir,
                "input_file": "case.k",
                "template_id": tid_tpl,
            },
            headers=h,
        )
        assert r.status_code == 200, r.text
        tid = r.json()["id"]

        # 等它真正跑起来（能读到 GO）
        _wait_status(client, h, tid, {"running"})
        assert db.trial_get(tid)["status"] == "running"

        r = client.post(f"/trials/{tid}/cancel", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["cancelled"] is True
        assert db.trial_get(tid)["status"] == "killed"

        # 再次中断已结束的试算 → 409
        r = client.post(f"/trials/{tid}/cancel", headers=h)
        assert r.status_code == 409
        db.close()
    get_settings().admin_users = ""


def test_trial_rejects_non_trial_template():
    """选了 kind=pbs 的普通模板去提交试算应被拒绝。"""
    job_dir = _setup_workdir()
    me = getpass.getuser()
    get_settings().admin_users = "zzz_admin_placeholder"

    from app.main import app

    with TestClient(app) as client:
        db = _swap_db(app)
        h = {"Authorization": f"Bearer {issue_token(me)}"}
        pbs_id = _make_template(app, "#!/bin/bash\necho hi", kind="pbs")

        r = client.post(
            "/trials/submit",
            json={
                "name": "x",
                "init_dir": job_dir,
                "input_file": "case.k",
                "template_id": pbs_id,
            },
            headers=h,
        )
        assert r.status_code == 400
        assert "试算模板" in r.json()["detail"]
        db.close()
    get_settings().admin_users = ""
