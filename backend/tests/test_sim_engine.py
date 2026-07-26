"""DAG 编排引擎测试。

覆盖三块：文档校验（环、悬空边、未知类型）、执行推进（依赖顺序、扇出、失败传播、
入参合并）、外部回流（waiting → complete）。
"""
import json

import pytest

from app.sim.db import SimDB
from app.sim.engine import DagError, PipelineEngine, validate_doc
from app.sim.nodes import (
    Done,
    Failed,
    NodeType,
    Waiting,
    _REGISTRY,
    register_builtin_node_types,
    register_node_type,
)
from app.sim.pipeline_store import (
    NODE_DONE,
    NODE_FAILED,
    NODE_SKIPPED,
    NODE_WAITING,
    RUN_DONE,
    RUN_FAILED,
    RUN_WAITING,
)


@pytest.fixture(autouse=True)
def builtin_types():
    register_builtin_node_types()
    # 测试专用节点类型：直接完成 / 直接失败 / 挂起等外部
    if "test.ok" not in _REGISTRY:
        register_node_type(NodeType(
            type_id="test.ok", label="OK", category="internal", description="",
            params_schema={}, inputs=[], outputs=["v"],
            executor=lambda ctx: Done({"v": ctx.params.get("v", 1),
                                       f"from_{ctx.node_id}": True}),
        ))
        register_node_type(NodeType(
            type_id="test.boom", label="BOOM", category="internal", description="",
            params_schema={}, inputs=[], outputs=[],
            executor=lambda ctx: Failed("故意失败"),
        ))
        register_node_type(NodeType(
            type_id="test.wait", label="WAIT", category="internal", description="",
            params_schema={}, inputs=[], outputs=["w"],
            executor=lambda ctx: Waiting(ref=None, hint="等外部"),
        ))
        register_node_type(NodeType(
            type_id="test.raise", label="RAISE", category="internal", description="",
            params_schema={}, inputs=[], outputs=[],
            executor=lambda ctx: (_ for _ in ()).throw(RuntimeError("执行器炸了")),
        ))
        register_node_type(NodeType(
            type_id="test.echo_inputs", label="ECHO", category="internal",
            description="", params_schema={}, inputs=[], outputs=["seen"],
            executor=lambda ctx: Done({"seen": sorted(ctx.inputs.keys())}),
        ))


@pytest.fixture
def db(tmp_path):
    d = SimDB(str(tmp_path / "sim.db"))
    yield d
    d.close()


@pytest.fixture
def engine(db):
    return PipelineEngine(db)


def doc(nodes, edges=()):
    return {
        "nodes": [{"id": n[0], "type": n[1], "params": (n[2] if len(n) > 2 else {})}
                  for n in nodes],
        "edges": [{"from": a, "to": b} for a, b in edges],
    }


def node_status(db, rid):
    return {n["node_id"]: n["status"] for n in db.list_node_runs(rid)}


# --- 文档校验 -----------------------------------------------------------

def test_validate_rejects_cycle():
    d = doc([("a", "test.ok"), ("b", "test.ok"), ("c", "test.ok")],
            [("a", "b"), ("b", "c"), ("c", "a")])
    with pytest.raises(DagError) as ei:
        validate_doc(d)
    assert "环" in str(ei.value)
    # 报出成环节点，便于在画布上定位
    for n in ("a", "b", "c"):
        assert n in str(ei.value)


def test_validate_rejects_self_loop():
    with pytest.raises(DagError, match="不能指向自己"):
        validate_doc(doc([("a", "test.ok")], [("a", "a")]))


def test_validate_rejects_dangling_edge():
    with pytest.raises(DagError, match="终点不存在"):
        validate_doc(doc([("a", "test.ok")], [("a", "ghost")]))


def test_validate_rejects_unknown_node_type():
    with pytest.raises(DagError, match="未注册的节点类型"):
        validate_doc(doc([("a", "nope.nope")]))


def test_validate_rejects_duplicate_ids():
    d = {"nodes": [{"id": "a", "type": "test.ok"}, {"id": "a", "type": "test.ok"}],
         "edges": []}
    with pytest.raises(DagError, match="重复"):
        validate_doc(d)


def test_validate_accepts_diamond():
    """菱形（扇出后扇入）是合法 DAG，必须放行。"""
    validate_doc(doc(
        [("a", "test.ok"), ("b", "test.ok"), ("c", "test.ok"), ("d", "test.ok")],
        [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")],
    ))


# --- 执行推进 -----------------------------------------------------------

def test_linear_pipeline_runs_to_completion(db, engine):
    pid = db.create_pipeline_def("线性", "u",
                                 doc([("a", "test.ok"), ("b", "test.ok")], [("a", "b")]))
    rid = engine.start_run(pid, "u")

    assert db.get_run(rid)["status"] == RUN_DONE
    assert node_status(db, rid) == {"a": NODE_DONE, "b": NODE_DONE}


def test_node_waits_until_dependency_done(db, engine):
    """上游 waiting 时，下游不得被派发。"""
    pid = db.create_pipeline_def("等待", "u",
                                 doc([("a", "test.wait"), ("b", "test.ok")], [("a", "b")]))
    rid = engine.start_run(pid, "u")

    st = node_status(db, rid)
    assert st["a"] == NODE_WAITING
    assert st["b"] == "pending", "上游未完成，下游不能启动"
    assert db.get_run(rid)["status"] == RUN_WAITING


def test_external_completion_advances_run(db, engine):
    pid = db.create_pipeline_def("回流", "u",
                                 doc([("a", "test.wait"), ("b", "test.ok")], [("a", "b")]))
    rid = engine.start_run(pid, "u")

    engine.complete_node(rid, "a", outputs={"w": 42})

    assert node_status(db, rid) == {"a": NODE_DONE, "b": NODE_DONE}
    assert db.get_run(rid)["status"] == RUN_DONE


def test_completing_non_waiting_node_is_rejected(db, engine):
    """防重复回流把已完成的节点改回去。"""
    pid = db.create_pipeline_def("x", "u", doc([("a", "test.ok")]))
    rid = engine.start_run(pid, "u")
    with pytest.raises(DagError, match="不是等待中"):
        engine.complete_node(rid, "a", outputs={})


def test_failure_skips_downstream_and_fails_run(db, engine):
    pid = db.create_pipeline_def("失败", "u",
                                 doc([("a", "test.boom"), ("b", "test.ok")], [("a", "b")]))
    rid = engine.start_run(pid, "u")

    st = node_status(db, rid)
    assert st["a"] == NODE_FAILED
    assert st["b"] == NODE_SKIPPED, "上游失败，下游不应停在 pending"
    run = db.get_run(rid)
    assert run["status"] == RUN_FAILED
    assert "故意失败" in run["error_message"]


def test_executor_exception_becomes_node_failure(db, engine):
    """执行器抛异常不能掀翻引擎，只让该节点失败。"""
    pid = db.create_pipeline_def("炸", "u", doc([("a", "test.raise")]))
    rid = engine.start_run(pid, "u")

    assert node_status(db, rid)["a"] == NODE_FAILED
    assert "执行器炸了" in db.get_node_run(rid, "a")["error_message"]


def test_fan_out_and_fan_in_merges_inputs(db, engine):
    """扇出后扇入：下游节点应收到全部上游的 outputs 合并。"""
    pid = db.create_pipeline_def("菱形", "u", doc(
        [("a", "test.ok"), ("b", "test.ok"), ("c", "test.ok"),
         ("d", "test.echo_inputs")],
        [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")],
    ))
    rid = engine.start_run(pid, "u")

    assert db.get_run(rid)["status"] == RUN_DONE
    seen = json.loads(db.get_node_run(rid, "d")["outputs_json"])["seen"]
    assert "from_b" in seen and "from_c" in seen


def test_run_snapshots_doc_so_later_edits_dont_affect_it(db, engine):
    """定义改了不影响已跑的 run —— 可复现的前提。"""
    pid = db.create_pipeline_def("快照", "u", doc([("a", "test.wait")]))
    rid = engine.start_run(pid, "u")

    db.update_pipeline_def(pid, doc=doc([("a", "test.ok"), ("z", "test.ok")]))

    snap = json.loads(db.get_run(rid)["doc_snapshot_json"])
    assert [n["id"] for n in snap["nodes"]] == ["a"]
    assert db.get_pipeline_def(pid)["version"] == 2, "文档变更应使版本自增"


def test_cancel_run_skips_pending(db, engine):
    pid = db.create_pipeline_def("取消", "u",
                                 doc([("a", "test.wait"), ("b", "test.ok")], [("a", "b")]))
    rid = engine.start_run(pid, "u")
    engine.cancel_run(rid)

    assert db.get_run(rid)["status"] == "canceled"
    assert node_status(db, rid)["b"] == NODE_SKIPPED


def test_tick_is_idempotent_on_finished_runs(db, engine):
    pid = db.create_pipeline_def("兜底", "u", doc([("a", "test.ok")]))
    rid = engine.start_run(pid, "u")
    assert db.get_run(rid)["status"] == RUN_DONE

    # 兜底轮询不应把已完成的 run 再动一次
    assert engine.tick() == 0
    assert db.get_run(rid)["status"] == RUN_DONE


# --- 内置节点：配置校验 -------------------------------------------------

def test_validate_config_node_enforces_template_rules(db, engine):
    tpl = db.create_template(
        "正碰", "crash", "ls-dyna",
        validation_rules={"velocity": {"required": True, "max": 120}},
    )
    pid_proj = db.create_project("P", owner="u")
    sid = db.create_subject(pid_proj, "工况", "crash", "ls-dyna",
                            template_id=tpl, config={"velocity": 200})

    pdef = db.create_pipeline_def("校验", "u", doc([("v", "internal.validate_config")]))
    rid = engine.start_run(pdef, "u", sim_project_id=pid_proj, sim_subject_id=sid)

    assert node_status(db, rid)["v"] == NODE_FAILED
    assert "超过上限" in db.get_node_run(rid, "v")["error_message"]


def test_validate_config_node_passes_valid_config(db, engine):
    tpl = db.create_template(
        "正碰", "crash", "ls-dyna",
        validation_rules={"velocity": {"required": True, "max": 120}},
    )
    proj = db.create_project("P", owner="u")
    sid = db.create_subject(proj, "工况", "crash", "ls-dyna",
                            template_id=tpl, config={"velocity": 50})

    pdef = db.create_pipeline_def("校验", "u", doc([("v", "internal.validate_config")]))
    rid = engine.start_run(pdef, "u", sim_project_id=proj, sim_subject_id=sid)

    assert db.get_run(rid)["status"] == RUN_DONE


def test_export_deck_renders_from_template_mapping(db, engine):
    tpl = db.create_template(
        "正碰", "crash", "ls-dyna",
        validation_rules={"velocity": {"required": True}},
        export_mapping={"template": "*VELOCITY\n{velocity}\n"},
    )
    proj = db.create_project("P", owner="u")
    sid = db.create_subject(proj, "工况", "crash", "ls-dyna",
                            template_id=tpl, config={"velocity": 50})

    pdef = db.create_pipeline_def("生成", "u", doc(
        [("v", "internal.validate_config"), ("e", "internal.export_deck")],
        [("v", "e")],
    ))
    rid = engine.start_run(pdef, "u", sim_project_id=proj, sim_subject_id=sid)

    assert db.get_run(rid)["status"] == RUN_DONE
    out = json.loads(db.get_node_run(rid, "e")["outputs_json"])
    assert out["deck_text"] == "*VELOCITY\n50\n"


def test_export_deck_reports_missing_field(db, engine):
    tpl = db.create_template("正碰", "crash", "ls-dyna",
                             export_mapping={"template": "{no_such_field}"})
    proj = db.create_project("P", owner="u")
    sid = db.create_subject(proj, "工况", "crash", "ls-dyna",
                            template_id=tpl, config={})

    pdef = db.create_pipeline_def("生成", "u", doc([("e", "internal.export_deck")]))
    rid = engine.start_run(pdef, "u", sim_project_id=proj, sim_subject_id=sid)

    assert node_status(db, rid)["e"] == NODE_FAILED
    assert "不存在的字段" in db.get_node_run(rid, "e")["error_message"]


def test_capability_node_parks_for_browser_broker(db, engine):
    """能力节点必须挂起等浏览器代理——SDM 后端调不到桌面端 vektor3d。"""
    pdef = db.create_pipeline_def("能力", "u", doc(
        [("m", "capability.invoke", {"capability_id": "mesh.generate"})]
    ))
    rid = engine.start_run(pdef, "u")

    assert node_status(db, rid)["m"] == NODE_WAITING
    assert "mesh.generate" in db.get_node_run(rid, "m")["wait_hint"]

    pending = db.list_waiting_nodes(owner="u", category_prefix="capability.")
    assert len(pending) == 1 and pending[0]["node_id"] == "m"
