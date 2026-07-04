"""提交准入调度器测试：不依赖真实 qstat/qsub，直接注入假 Job 与假 run_as_user。"""
import tempfile
import time

from app.db.jobs_db import JobsDB
from app.pbs.parser import Job
from app.submit import scheduler as scheduler_mod
from app.submit.scheduler import SubmissionScheduler


def _make_db() -> JobsDB:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return JobsDB(tmp.name)


def _fake_job(jobid: str, owner: str, state: str = "R", nodes: str = "1:ppn=8") -> Job:
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


def _patch_qsub(monkeypatch_ok=True, jobid="999.hpcmaster"):
    """把 run_as_user 换成假实现：总是返回成功并生成 jobid。"""
    calls = []

    def fake_run_as_user(username, argv, cwd=None, timeout=None):
        calls.append((username, argv, cwd))
        return _FakeProc(returncode=0, stdout=f"{jobid}\n".encode())

    scheduler_mod.run_as_user = fake_run_as_user
    return calls


def test_default_user_admitted_immediately():
    """无策略、无全局核数上限：提交应在一次 tick 内直接准入(等价现状行为)。"""
    db = _make_db()
    _patch_qsub()
    sched = SubmissionScheduler(db, interval=999)

    qid = db.sq_enqueue("user01", "job1", "batch", "/tmp/a.pbs", "/tmp", cores=4)
    admitted = sched.tick()

    assert admitted == 1
    row = db.sq_get(qid)
    assert row["status"] == "submitted"
    assert row["jobid"] == "999.hpcmaster"
    db.close()


def test_user_cap_blocks_then_frees_on_completion():
    """user07 配额2：已有2个运行中任务时，第3个提交本地排队；其中一个结束后自动补位。"""
    db = _make_db()
    _patch_qsub()
    sched = SubmissionScheduler(db, interval=999)
    db.policy_upsert("user07", max_concurrent=2, priority=10)

    # 模拟 user07 已有 2 个活跃任务（轮询采集得到）
    db.upsert_active([
        _fake_job("1.hpcmaster", "user07"),
        _fake_job("2.hpcmaster", "user07"),
    ])

    qid = db.sq_enqueue("user07", "job3", "batch", "/tmp/c.pbs", "/tmp", cores=4)
    admitted = sched.tick()
    assert admitted == 0
    assert db.sq_get(qid)["status"] == "queued"

    # 其中一个任务结束（下一轮 qstat 中消失）-> 空出一个名额
    db.upsert_active([_fake_job("1.hpcmaster", "user07")])
    admitted = sched.tick()
    assert admitted == 1
    row = db.sq_get(qid)
    assert row["status"] == "submitted"
    db.close()


def test_priority_wins_when_cores_scarce():
    """全局核数吃紧时，高优先级用户的排队项应先于普通用户被准入。"""
    db = _make_db()
    _patch_qsub()
    sched = SubmissionScheduler(db, interval=999)
    db.policy_upsert("user07", max_concurrent=None, priority=10)

    from app.config import get_settings
    get_settings().cluster_total_cores = 8  # 集群总共 8 核，且全部空闲

    # user01(默认策略) 先排队，user07(高优先级) 后排队，但两者都要 8 核（只够放一个）
    qid_default = db.sq_enqueue("user01", "job_default", "batch", "/tmp/d.pbs", "/tmp", cores=8)
    qid_priority = db.sq_enqueue("user07", "job_priority", "batch", "/tmp/e.pbs", "/tmp", cores=8)

    admitted = sched.tick()
    assert admitted == 1
    assert db.sq_get(qid_priority)["status"] == "submitted"
    assert db.sq_get(qid_default)["status"] == "queued"

    get_settings().cluster_total_cores = 0  # 还原，避免影响其它测试
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
