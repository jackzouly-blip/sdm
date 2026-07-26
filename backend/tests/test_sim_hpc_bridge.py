"""编排与 HPC 链路的桥接测试。

不触真实 qsub/PAM：用假的 jobs_db 与打桩的 write_file 验证桥接逻辑本身——
提交前置校验、两段式句柄、句柄升级、完成与失败回流。
"""
import pytest

from app.sim import hpc_bridge
from app.sim.db import SimDB
from app.sim.engine import PipelineEngine, set_engine
from app.sim.nodes import register_builtin_node_types
from app.sim.pipeline_store import NODE_DONE, NODE_FAILED, NODE_WAITING


class FakeSettings:
    fs_roots = "/data"
    submit_default_queue = "batch"


class FakeJobsDB:
    """只实现桥接用到的那几个方法。"""

    def __init__(self):
        self.queue = {}
        self.jobs = {}
        self._seq = 0
        self.enqueued = []

    def sq_enqueue(self, owner, jobname, queue_name, script_path, workdir,
                   cores, extra_l=None):
        self._seq += 1
        self.queue[self._seq] = {
            "id": self._seq, "status": "queued", "jobid": None, "msg": None,
            "owner": owner, "cores": cores, "script_path": script_path,
        }
        self.enqueued.append({"jobname": jobname, "queue_name": queue_name,
                              "workdir": workdir, "cores": cores,
                              "script_path": script_path, "extra_l": extra_l})
        return self._seq

    def sq_get(self, qid):
        return self.queue.get(qid)

    def get(self, jobid):
        return self.jobs.get(jobid)

    # --- 测试辅助 ---
    def admit(self, qid, jobid):
        self.queue[qid].update(status="submitted", jobid=jobid)

    def fail(self, qid, msg):
        self.queue[qid].update(status="failed", msg=msg)

    def finish(self, jobid, exit_status=0, workdir="/data/p"):
        self.jobs[jobid] = {"jobid": jobid, "exit_status": exit_status,
                            "workdir": workdir}


@pytest.fixture(autouse=True)
def builtin():
    register_builtin_node_types()


@pytest.fixture
def db(tmp_path):
    d = SimDB(str(tmp_path / "sim.db"))
    yield d
    d.close()


@pytest.fixture
def jobs_db():
    return FakeJobsDB()


@pytest.fixture
def engine(db, jobs_db):
    e = PipelineEngine(db, jobs_db=jobs_db, templates_db=None,
                       settings=FakeSettings())
    set_engine(e)
    yield e
    set_engine(None)


@pytest.fixture
def no_fs(monkeypatch):
    """打桩落盘与准入调度：桥接逻辑与真实文件系统/PBS 无关。

    app.fs.browser 依赖 fcntl（Unix 专有），在非 Unix 开发机上根本导入不了，
    故那里注入一个桩模块；Linux 上仍打桩真实模块，走的是同一条代码路径。
    """
    import importlib
    import sys
    import types

    written = []

    def fake_write_file(user, parent, name, data, roots):
        written.append({"user": user, "parent": parent, "name": name, "data": data})
        return {"path": f"{parent}/{name}"}

    def patch_or_stub(mod_name: str, attrs: dict) -> None:
        """能导入就打桩真实模块，导不进（缺 fcntl）就注入桩模块。"""
        try:
            importlib.import_module(mod_name)
        except ImportError:
            stub = types.ModuleType(mod_name)
            for k, v in attrs.items():
                setattr(stub, k, v)
            stub.FsError = type("FsError", (Exception,), {"message": ""})
            monkeypatch.setitem(sys.modules, mod_name, stub)
            # 同时挂到父包上，否则测试里用点号路径 monkeypatch 会解析不到
            pkg_name, _, leaf = mod_name.rpartition(".")
            monkeypatch.setattr(importlib.import_module(pkg_name), leaf, stub,
                                raising=False)
            return
        for k, v in attrs.items():
            monkeypatch.setattr(f"{mod_name}.{k}", v)

    patch_or_stub("app.fs.browser", {"write_file": fake_write_file})
    patch_or_stub("app.submit.router", {
        "render_template": lambda content, init_dir, rel: content,
        "inject_ppn": lambda script, cores: script,
    })
    patch_or_stub("app.submit.scheduler", {"get_scheduler": lambda: None})
    return written


def hpc_doc(params=None):
    return {"nodes": [{"id": "j", "type": "hpc.submit", "params": params or {}}],
            "edges": []}


def project_with_workdir(db, workdir="/data/proj"):
    return db.create_project("P", owner="u", workdir=workdir)


# --- 提交前置校验 -------------------------------------------------------

def test_submit_requires_project(db, engine):
    pdef = db.create_pipeline_def("p", "u", hpc_doc({"script": "#!/bin/sh"}))
    rid = engine.start_run(pdef, "u")
    node = db.get_node_run(rid, "j")
    assert node["status"] == NODE_FAILED
    assert "未绑定仿真项目" in node["error_message"]


def test_submit_requires_workdir(db, engine):
    proj = db.create_project("P", owner="u")  # 无 workdir
    pdef = db.create_pipeline_def("p", "u", hpc_doc({"script": "#!/bin/sh"}))
    rid = engine.start_run(pdef, "u", sim_project_id=proj)
    assert "未设置工作目录" in db.get_node_run(rid, "j")["error_message"]


def test_submit_requires_script_or_template(db, engine, no_fs):
    proj = project_with_workdir(db)
    pdef = db.create_pipeline_def("p", "u", hpc_doc({}))
    rid = engine.start_run(pdef, "u", sim_project_id=proj)
    assert "无法生成提交脚本" in db.get_node_run(rid, "j")["error_message"]


def test_engine_without_hpc_deps_reports_clearly(db):
    """引擎未装配 HPC 依赖时应给明确错误，而不是崩溃。"""
    eng = PipelineEngine(db)  # 无 jobs_db/settings
    proj = project_with_workdir(db)
    pdef = db.create_pipeline_def("p", "u", hpc_doc({"script": "x"}))
    rid = eng.start_run(pdef, "u", sim_project_id=proj)
    assert "未装配" in db.get_node_run(rid, "j")["error_message"]


# --- 两段式句柄 ---------------------------------------------------------

def test_queued_submission_parks_with_sq_handle(db, engine, jobs_db, no_fs):
    """受配额/核数限制仍在本地排队时，用 sq: 句柄先追踪住。"""
    proj = project_with_workdir(db)
    pdef = db.create_pipeline_def("p", "u", hpc_doc({"script": "#!/bin/sh\n"}))
    rid = engine.start_run(pdef, "u", sim_project_id=proj)

    node = db.get_node_run(rid, "j")
    assert node["status"] == NODE_WAITING
    assert node["external_ref"] == "sq:1"
    assert "排队" in node["wait_hint"]


def test_immediate_admission_uses_real_jobid(db, engine, jobs_db, no_fs, monkeypatch):
    """配额充裕时准入调度在提交调用内就完成 qsub，直接拿到作业号。"""
    proj = project_with_workdir(db)

    class Sched:
        def tick(self):
            jobs_db.admit(1, "2434.hpcmaster")

    monkeypatch.setattr("app.submit.scheduler.get_scheduler", lambda: Sched())
    pdef = db.create_pipeline_def("p", "u", hpc_doc({"script": "#!/bin/sh\n"}))
    rid = engine.start_run(pdef, "u", sim_project_id=proj)

    node = db.get_node_run(rid, "j")
    assert node["external_ref"] == "2434.hpcmaster"
    assert "求解" in node["wait_hint"]


def test_queue_handle_is_promoted_to_jobid(db, engine, jobs_db, no_fs):
    proj = project_with_workdir(db)
    pdef = db.create_pipeline_def("p", "u", hpc_doc({"script": "#!/bin/sh\n"}))
    rid = engine.start_run(pdef, "u", sim_project_id=proj)
    assert db.get_node_run(rid, "j")["external_ref"] == "sq:1"

    jobs_db.admit(1, "2500.hpcmaster")
    hpc_bridge.tick_queue_refs(db, jobs_db)

    node = db.get_node_run(rid, "j")
    assert node["external_ref"] == "2500.hpcmaster"
    assert node["status"] == NODE_WAITING, "升级句柄不应改变等待状态"


def test_queue_failure_fails_the_node(db, engine, jobs_db, no_fs):
    """qsub 失败要让节点失败，而不是永远挂着。"""
    proj = project_with_workdir(db)
    pdef = db.create_pipeline_def("p", "u", hpc_doc({"script": "#!/bin/sh\n"}))
    rid = engine.start_run(pdef, "u", sim_project_id=proj)

    jobs_db.fail(1, "队列不存在")
    hpc_bridge.tick_queue_refs(db, jobs_db)

    node = db.get_node_run(rid, "j")
    assert node["status"] == NODE_FAILED
    assert "队列不存在" in node["error_message"]


# --- 完成回流 -----------------------------------------------------------

def test_job_completion_advances_node(db, engine, jobs_db, no_fs):
    proj = project_with_workdir(db)
    pdef = db.create_pipeline_def("p", "u", hpc_doc({"script": "#!/bin/sh\n"}))
    rid = engine.start_run(pdef, "u", sim_project_id=proj)
    jobs_db.admit(1, "2600.hpcmaster")
    hpc_bridge.tick_queue_refs(db, jobs_db)

    jobs_db.finish("2600.hpcmaster", exit_status=0)
    assert hpc_bridge.on_jobs_finished(db, jobs_db, ["2600.hpcmaster"]) == 1

    node = db.get_node_run(rid, "j")
    assert node["status"] == NODE_DONE
    import json
    assert json.loads(node["outputs_json"])["hpc_jobid"] == "2600.hpcmaster"
    assert db.get_run(rid)["status"] == "done"


def test_nonzero_exit_fails_the_pipeline(db, engine, jobs_db, no_fs):
    """求解失败不能让流水线拿着坏结果继续往下走。"""
    proj = project_with_workdir(db)
    pdef = db.create_pipeline_def("p", "u", hpc_doc({"script": "#!/bin/sh\n"}))
    rid = engine.start_run(pdef, "u", sim_project_id=proj)
    jobs_db.admit(1, "2700.hpcmaster")
    hpc_bridge.tick_queue_refs(db, jobs_db)

    jobs_db.finish("2700.hpcmaster", exit_status=137)
    hpc_bridge.on_jobs_finished(db, jobs_db, ["2700.hpcmaster"])

    node = db.get_node_run(rid, "j")
    assert node["status"] == NODE_FAILED
    assert "137" in node["error_message"]
    assert db.get_run(rid)["status"] == "failed"


def test_unrelated_finished_jobs_are_ignored(db, engine, jobs_db, no_fs):
    """门户之外直接 qsub 的作业不该影响编排。"""
    proj = project_with_workdir(db)
    pdef = db.create_pipeline_def("p", "u", hpc_doc({"script": "#!/bin/sh\n"}))
    engine.start_run(pdef, "u", sim_project_id=proj)

    assert hpc_bridge.on_jobs_finished(db, jobs_db, ["9999.other"]) == 0


# --- 输入卡落盘 ---------------------------------------------------------

def test_deck_is_written_with_run_suffix(db, engine, jobs_db, no_fs, monkeypatch):
    """重跑不能因 write_file 的 O_EXCL 防覆盖而失败，故文件名带运行号后缀。"""
    proj = project_with_workdir(db, "/data/proj")
    doc = {
        "nodes": [
            {"id": "d", "type": "manual.confirm", "params": {}},
            {"id": "j", "type": "hpc.submit",
             "params": {"script": "#!/bin/sh\n", "deck_filename": "input.k"}},
        ],
        "edges": [{"from": "d", "to": "j"}],
    }
    pdef = db.create_pipeline_def("p", "u", doc)
    rid = engine.start_run(pdef, "u", sim_project_id=proj)
    # 人工节点带出 deck_text，模拟上游 export_deck 的产出
    engine.complete_node(rid, "d", outputs={"deck_text": "*KEYWORD\n"})

    decks = [w for w in no_fs if w["name"].startswith("input_")]
    assert len(decks) == 1
    assert decks[0]["name"] == f"input_{rid[:8]}.k"
    assert decks[0]["data"] == b"*KEYWORD\n"
    assert decks[0]["parent"] == "/data/proj"


def test_cores_are_accounted_for_admission(db, engine, jobs_db, no_fs):
    """核数要传给排队队列，全局核数网关才能正确记账。"""
    proj = project_with_workdir(db)
    pdef = db.create_pipeline_def("p", "u", hpc_doc(
        {"script": "#!/bin/sh\n#PBS -l nodes=1:ppn=4\n", "cores": 16,
         "queue": "fast"}))
    engine.start_run(pdef, "u", sim_project_id=proj)

    assert jobs_db.enqueued[0]["cores"] == 16
    assert jobs_db.enqueued[0]["queue_name"] == "fast"
