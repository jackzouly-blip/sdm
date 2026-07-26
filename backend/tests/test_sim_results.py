"""结果收集与查看产物节点测试。"""
import json
import os

import pytest

from app.sim.db import SimDB
from app.sim.engine import PipelineEngine, set_engine
from app.sim.nodes import register_builtin_node_types
from app.sim.pipeline_store import NODE_DONE, NODE_FAILED, NODE_WAITING
from app.sim.results import TASK_PREFIX, classify, tick_task_refs


class FakeTaskManager:
    """只实现编排用到的 submit/snapshot。"""

    def __init__(self):
        self.submitted = []
        self.snaps = {}
        self._seq = 0

    def submit(self, task_type, owner, params):
        self._seq += 1
        tid = f"t{self._seq}"
        self.submitted.append({"id": tid, "type": task_type, "owner": owner,
                               "params": params})
        self.snaps[tid] = {"id": tid, "status": "running", "result": None,
                           "error": None}
        return tid

    def snapshot(self, tid):
        return self.snaps.get(tid)

    def finish(self, tid, result=None, status="success", error=None):
        self.snaps[tid].update(status=status, result=result, error=error)


@pytest.fixture(autouse=True)
def builtin(patch_or_stub):
    register_builtin_node_types()
    # viewer.prepare 只用到 d3plot.service 里的纯函数 cache_key，但该模块会
    # 传递依赖 fcntl，在非 Unix 上导入不了；Linux 上走的仍是真实模块。
    import hashlib

    patch_or_stub("app.d3plot.service", {
        "cache_key": lambda realpath, mtime, size, family_sig="": hashlib.sha1(
            f"{realpath}|{int(mtime)}|{int(size)}|{family_sig}".encode()
        ).hexdigest()[:16],
    })
    patch_or_stub("app.d3plot.router", {"_family_sig": lambda user, d3path: "1:10:0"})


@pytest.fixture
def db(tmp_path):
    d = SimDB(str(tmp_path / "sim.db"))
    yield d
    d.close()


@pytest.fixture
def tm():
    return FakeTaskManager()


@pytest.fixture
def engine(db, tm):
    e = PipelineEngine(db, task_manager=tm)
    set_engine(e)
    yield e
    set_engine(None)


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    for name in ("d3plot", "d3plot01", "d3plot02", "binout0000", "d3hsp",
                 "2434.h3d", "input.k", "messag"):
        (d / name).write_bytes(b"x" * 10)
    return str(d)


def collect_doc(params=None):
    return {"nodes": [{"id": "c", "type": "internal.collect_results",
                       "params": params or {}}], "edges": []}


# --- 文件分类 -----------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("d3plot", "d3plot"),
    ("d3plot01", "d3plot"),
    ("d3plotaa", "d3plot"),
    ("binout0000", "binout"),
    ("d3hsp", "d3hsp"),
    ("2434.h3d", "h3d"),
    ("case3.H3D", "h3d"),
    ("input.k", None),
    ("messag", None),
])
def test_classify(name, expected):
    assert classify(name) == expected


# --- 收集结果 -----------------------------------------------------------

def test_collect_registers_results(db, engine, workdir):
    proj = db.create_project("P", owner="u", workdir=workdir)
    sid = db.create_subject(proj, "工况", "crash", "ls-dyna")
    pdef = db.create_pipeline_def("p", "u", collect_doc())
    rid = engine.start_run(pdef, "u", sim_project_id=proj, sim_subject_id=sid)

    node = db.get_node_run(rid, "c")
    assert node["status"] == NODE_DONE
    out = json.loads(node["outputs_json"])
    assert out["registered"] == {"d3plot": 3, "binout": 1, "d3hsp": 1, "h3d": 1}
    assert out["registered_total"] == 6

    results = db.list_results(out["sim_job_id"])
    names = {os.path.basename(r["file_path"]) for r in results}
    assert "input.k" not in names and "messag" not in names


def test_collect_is_idempotent(db, engine, workdir):
    """重跑不该产生重复登记。"""
    proj = db.create_project("P", owner="u", workdir=workdir)
    sid = db.create_subject(proj, "工况", "crash", "ls-dyna")
    pdef = db.create_pipeline_def("p", "u", collect_doc())

    rid1 = engine.start_run(pdef, "u", sim_project_id=proj, sim_subject_id=sid)
    job1 = json.loads(db.get_node_run(rid1, "c")["outputs_json"])["sim_job_id"]

    rid2 = engine.start_run(pdef, "u", sim_project_id=proj, sim_subject_id=sid)
    out2 = json.loads(db.get_node_run(rid2, "c")["outputs_json"])

    assert out2["sim_job_id"] == job1, "应复用同一个 sim_job 而非每次新建"
    assert out2["registered_total"] == 0, "第二次不应重复登记"
    assert len(db.list_results(job1)) == 6


def test_collect_can_filter_types(db, engine, workdir):
    proj = db.create_project("P", owner="u", workdir=workdir)
    sid = db.create_subject(proj, "工况", "crash", "ls-dyna")
    pdef = db.create_pipeline_def("p", "u", collect_doc({"result_types": ["d3plot"]}))
    rid = engine.start_run(pdef, "u", sim_project_id=proj, sim_subject_id=sid)

    out = json.loads(db.get_node_run(rid, "c")["outputs_json"])
    assert out["registered"] == {"d3plot": 3}


def test_collect_requires_project_and_subject(db, engine, workdir):
    pdef = db.create_pipeline_def("p", "u", collect_doc())
    rid = engine.start_run(pdef, "u")
    assert "未绑定仿真项目" in db.get_node_run(rid, "c")["error_message"]

    proj = db.create_project("P", owner="u", workdir=workdir)
    rid = engine.start_run(pdef, "u", sim_project_id=proj)
    assert "未绑定工况" in db.get_node_run(rid, "c")["error_message"]


def test_collect_attaches_to_existing_job_by_hpc_id(db, engine, workdir):
    """上游 hpc 节点传下作业号时，结果应挂到那个 sim_job 上。"""
    proj = db.create_project("P", owner="u", workdir=workdir)
    sid = db.create_subject(proj, "工况", "crash", "ls-dyna")
    jid = db.create_job(sid)
    db.mark_job_submitted(jid, "2434.hpcmaster")

    doc = {
        "nodes": [
            {"id": "up", "type": "manual.confirm", "params": {}},
            {"id": "c", "type": "internal.collect_results", "params": {}},
        ],
        "edges": [{"from": "up", "to": "c"}],
    }
    pdef = db.create_pipeline_def("p", "u", doc)
    rid = engine.start_run(pdef, "u", sim_project_id=proj, sim_subject_id=sid)
    engine.complete_node(rid, "up", outputs={"hpc_jobid": "2434.hpcmaster"})

    out = json.loads(db.get_node_run(rid, "c")["outputs_json"])
    assert out["sim_job_id"] == jid


# --- 查看产物 -----------------------------------------------------------

def viewer_doc():
    return {
        "nodes": [
            {"id": "c", "type": "internal.collect_results", "params": {}},
            {"id": "v", "type": "viewer.prepare", "params": {}},
        ],
        "edges": [{"from": "c", "to": "v"}],
    }


def test_viewer_prepare_submits_task_and_parks(db, engine, tm, workdir):
    proj = db.create_project("P", owner="u", workdir=workdir)
    sid = db.create_subject(proj, "工况", "crash", "ls-dyna")
    pdef = db.create_pipeline_def("p", "u", viewer_doc())
    rid = engine.start_run(pdef, "u", sim_project_id=proj, sim_subject_id=sid)

    node = db.get_node_run(rid, "v")
    assert node["status"] == NODE_WAITING
    assert node["external_ref"].startswith(TASK_PREFIX)

    sub = tm.submitted[0]
    assert sub["type"] == "d3plot_view"
    # 应挑家族主文件 d3plot 而非 d3plot01
    assert os.path.basename(sub["params"]["d3plot"]) == "d3plot"
    assert sub["params"]["run_as"] == "u"


def test_viewer_task_completion_advances_node(db, engine, tm, workdir):
    proj = db.create_project("P", owner="u", workdir=workdir)
    sid = db.create_subject(proj, "工况", "crash", "ls-dyna")
    pdef = db.create_pipeline_def("p", "u", viewer_doc())
    rid = engine.start_run(pdef, "u", sim_project_id=proj, sim_subject_id=sid)

    tid = db.get_node_run(rid, "v")["external_ref"][len(TASK_PREFIX):]
    tm.finish(tid, result={"key": "abc123", "n_states": 40})

    assert tick_task_refs(db, tm) == 1
    node = db.get_node_run(rid, "v")
    assert node["status"] == NODE_DONE
    assert json.loads(node["outputs_json"])["viewer_key"] == "abc123"
    assert db.get_run(rid)["status"] == "done"


def test_viewer_task_failure_fails_node(db, engine, tm, workdir):
    proj = db.create_project("P", owner="u", workdir=workdir)
    sid = db.create_subject(proj, "工况", "crash", "ls-dyna")
    pdef = db.create_pipeline_def("p", "u", viewer_doc())
    rid = engine.start_run(pdef, "u", sim_project_id=proj, sim_subject_id=sid)

    tid = db.get_node_run(rid, "v")["external_ref"][len(TASK_PREFIX):]
    tm.finish(tid, status="failed", error="lasso 解析失败")

    tick_task_refs(db, tm)
    node = db.get_node_run(rid, "v")
    assert node["status"] == NODE_FAILED
    assert "lasso" in node["error_message"]


def test_viewer_without_d3plot_fails_clearly(db, engine, tm, tmp_path):
    """没有 d3plot 结果时要说清楚，而不是假装成功。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "binout0000").write_bytes(b"x")
    proj = db.create_project("P", owner="u", workdir=str(empty))
    sid = db.create_subject(proj, "工况", "crash", "ls-dyna")
    pdef = db.create_pipeline_def("p", "u", viewer_doc())
    rid = engine.start_run(pdef, "u", sim_project_id=proj, sim_subject_id=sid)

    assert "没有 d3plot 结果" in db.get_node_run(rid, "v")["error_message"]


def test_viewer_without_task_manager_fails_clearly(db, workdir):
    eng = PipelineEngine(db)  # 未装配 task_manager
    set_engine(eng)
    try:
        proj = db.create_project("P", owner="u", workdir=workdir)
        sid = db.create_subject(proj, "工况", "crash", "ls-dyna")
        pdef = db.create_pipeline_def("p", "u", viewer_doc())
        rid = eng.start_run(pdef, "u", sim_project_id=proj, sim_subject_id=sid)
        assert "任务管理器未装配" in db.get_node_run(rid, "v")["error_message"]
    finally:
        set_engine(None)


def test_tick_ignores_unfinished_tasks(db, engine, tm, workdir):
    proj = db.create_project("P", owner="u", workdir=workdir)
    sid = db.create_subject(proj, "工况", "crash", "ls-dyna")
    pdef = db.create_pipeline_def("p", "u", viewer_doc())
    rid = engine.start_run(pdef, "u", sim_project_id=proj, sim_subject_id=sid)

    assert tick_task_refs(db, tm) == 0
    assert db.get_node_run(rid, "v")["status"] == NODE_WAITING
