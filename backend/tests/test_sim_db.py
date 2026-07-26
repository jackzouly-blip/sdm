"""SDM 仿真数据模型测试。

不依赖 HTTP 与集群，只验证数据层的结构性保证：
  - 层级级联删除（项目删掉，下面全清）
  - 版本号自增与唯一
  - 跨库软引用 hpc_jobid 的回填与反查
  - 属主隔离所依赖的查询语义
"""
import pytest

from app.sim.db import (
    JOB_DONE,
    JOB_DRAFT,
    JOB_SUBMITTED,
    SUBJECT_DRAFT,
    SimDB,
)


@pytest.fixture
def db(tmp_path):
    d = SimDB(str(tmp_path / "sim.db"))
    yield d
    d.close()


@pytest.fixture
def project(db):
    return db.create_project("整椅碰撞", owner="user07", default_solver="ls-dyna")


def test_project_crud_and_owner_scoping(db):
    p1 = db.create_project("A", owner="user07")
    db.create_project("B", owner="other")

    assert len(db.list_projects(owner="user07")) == 1
    assert len(db.list_projects(owner=None)) == 2, "owner=None 是管理员视角，看全部"

    db.update_project(p1, name="A2", description="改了")
    row = db.get_project(p1)
    assert row["name"] == "A2" and row["description"] == "改了"
    assert row["updated_at"] >= row["created_at"]


def test_update_ignores_unknown_fields(db, project):
    """部分更新只接受白名单字段，避免路由层疏漏导致越权改 owner。"""
    db.update_project(project, owner="attacker", name="新名字")
    row = db.get_project(project)
    assert row["owner"] == "user07"
    assert row["name"] == "新名字"


def test_geometry_and_mesh_version_autoincrement(db, project):
    t = db.create_target(project, "座椅骨架", "assembly")
    g1 = db.add_geometry(t, source_type="upload")
    g2 = db.add_geometry(t, source_type="upload")

    assert [g["version_no"] for g in db.list_geometries(t)] == [1, 2]

    m1 = db.add_mesh(g2, mesh_type="shell", mesh_engine="manual")
    m2 = db.add_mesh(g2, mesh_type="shell", mesh_engine="manual")
    assert [m["version_no"] for m in db.list_meshes(g2)] == [1, 2]
    # 版本号在各自父级下独立计数
    assert db.get_mesh(m1)["version_no"] == 1
    assert len(db.list_meshes(g1)) == 0


def test_cascade_delete_project_clears_everything(db, project):
    t = db.create_target(project, "座椅骨架", "assembly")
    g = db.add_geometry(t, source_type="upload")
    m = db.add_mesh(g, mesh_type="shell", mesh_engine="manual")
    s = db.create_subject(project, "正碰 50km/h", "crash", "ls-dyna",
                          sim_mesh_version_id=m)
    j = db.create_job(s)
    db.add_result(j, "d3plot", "/data/run/d3plot")

    assert db.delete_project(project) is True

    assert db.get_project(project) is None
    assert db.get_target(t) is None
    assert db.get_geometry(g) is None
    assert db.get_mesh(m) is None
    assert db.get_subject(s) is None
    assert db.get_job(j) is None
    assert db.list_results(j) == []


def test_job_lifecycle_and_hpc_backref(db, project):
    """sim_job 不实现执行：投递后回填 hpc_jobid，并能按它反查。"""
    s = db.create_subject(project, "正碰", "crash", "ls-dyna")
    j = db.create_job(s, submit_mode="pbs")

    assert db.get_job(j)["status"] == JOB_DRAFT
    assert db.get_job(j)["hpc_jobid"] is None

    db.mark_job_submitted(j, "2434.hpcmaster")
    row = db.get_job(j)
    assert row["status"] == JOB_SUBMITTED
    assert row["hpc_jobid"] == "2434.hpcmaster"
    assert row["submitted_at"] is not None
    assert row["finished_at"] is None, "刚投递不该有完成时间"

    # 作业状态回流时靠 hpc_jobid 定位 sim_job
    assert db.find_job_by_hpc("2434.hpcmaster")["id"] == j
    assert db.find_job_by_hpc("不存在") is None

    db.set_job_status(j, JOB_DONE)
    row = db.get_job(j)
    assert row["status"] == JOB_DONE
    assert row["finished_at"] is not None


def test_subject_starts_as_draft_and_config_roundtrip(db, project):
    import json

    s = db.create_subject(project, "正碰", "crash", "ls-dyna",
                          config={"velocity": 50, "unit": "km/h"})
    row = db.get_subject(s)
    assert row["status"] == SUBJECT_DRAFT
    assert json.loads(row["config_json"])["velocity"] == 50


def test_builtin_template_cannot_be_deleted(db):
    builtin = db.create_template("内置正碰", "crash", "ls-dyna", is_builtin=True)
    custom = db.create_template("自定义正碰", "crash", "ls-dyna")

    assert db.delete_template(builtin) is False
    assert db.get_template(builtin) is not None
    assert db.delete_template(custom) is True


def test_template_filtering(db):
    db.create_template("正碰", "crash", "ls-dyna")
    db.create_template("侧碰", "crash", "ls-dyna")
    db.create_template("流场", "cfd", "fluent")

    assert len(db.list_templates()) == 3
    assert len(db.list_templates(subject_type="crash")) == 2
    assert len(db.list_templates(solver_type="fluent")) == 1


def test_project_stats_counts_across_hierarchy(db, project):
    t = db.create_target(project, "骨架", "assembly")
    db.create_target(project, "面套", "part")
    s1 = db.create_subject(project, "正碰", "crash", "ls-dyna")
    s2 = db.create_subject(project, "侧碰", "crash", "ls-dyna")
    db.create_job(s1)
    db.create_job(s1)
    db.create_job(s2)

    assert db.project_stats(project) == {"targets": 2, "subjects": 2, "jobs": 3}


def test_results_grouped_by_project_for_viewer_dispatch(db, project):
    """结果查看页按 result_type 分发到查看器插件，故需能按项目取全部结果。"""
    s = db.create_subject(project, "正碰", "crash", "ls-dyna")
    j = db.create_job(s)
    db.add_result(j, "d3plot", "/data/run/d3plot")
    db.add_result(j, "binout", "/data/run/binout0000")

    rows = db.list_results_by_project(project)
    assert {r["result_type"] for r in rows} == {"d3plot", "binout"}
    assert all(r["subject_name"] == "正碰" for r in rows)

    assert len(db.list_results(j, result_type="d3plot")) == 1
