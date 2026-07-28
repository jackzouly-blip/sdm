"""网格质量卡：解析、往返保真、判据引擎。

基准数据是客户给的真实卡（ref/5mm.ansa_qual、ref/5mm.ansa_mpar），已作为内置
默认模板 generic-crash-5mm 随代码走。用真卡而非构造样例做基准，是因为这套东西
的价值全在"和 ANSA 对得上"——自己编的样例证明不了这一点。
"""
import json
import os

import pytest

from app.sim.quality import (
    Override,
    QualityCardLibrary,
    added_mass_ratio,
    estimate_time_step,
    evaluate,
    evaluate_all,
)
from app.sim.quality import ansa_mpar, ansa_qual
from app.sim.quality.library import BUILTIN_DIR, CRITERIA_FILE, DEFAULT_TEMPLATE_ID, MESH_FILE

BUILTIN = os.path.join(BUILTIN_DIR, DEFAULT_TEMPLATE_ID)


@pytest.fixture
def card():
    return QualityCardLibrary().load()


# --- 解析 ---------------------------------------------------------------

def test_parses_real_quality_card(card):
    assert card.criteria.ansa_version == "19.1.1"
    shells = [c for c in card.criteria.criteria if c.domain == "shells"]
    assert len(shells) == 28
    # 碰撞是壳的天下：这张卡 solids 判据一条没开
    assert card.criteria.enabled_criteria("solids") == []
    assert len(card.criteria.enabled_criteria("shells")) == 11


def test_criterion_carries_its_algorithm_family(card):
    """算法族不是装饰：同一个长宽比按 NASTRAN 和按 IDEAS 算数值不同。"""
    assert card.criteria.get("aspect ratio").calculation == "NASTRAN"
    assert card.criteria.get("skewness").calculation == "PATRAN"
    assert card.criteria.get("warping").calculation == "IDEAS"
    assert card.criteria.get("jacobian").calculation == "ANSA"


def test_thresholds_and_ranges(card):
    warp = card.criteria.get("warping")
    assert (warp.thresholds["best"], warp.thresholds["good"],
            warp.thresholds["failed"], warp.thresholds["worst"]) == (0.0, 5.0, 15.0, 180.0)
    bands = card.criteria.ranges["shells"]
    assert [b.percentage for b in bands] == [0.0, 60.0, 95.0, 100.0]
    assert [b.penalty for b in bands] == [0.0, 0.0, 1.0, 10.0]
    assert card.criteria.failed_index["shells"] == 2


def test_blank_threshold_is_none_not_zero(card):
    """BLANK 表示"该档没有阈值"，用 0 顶替会让它和真实的 0 阈值混为一谈。"""
    inc = card.criteria.get("incomplete element")
    assert inc.thresholds["best"] is None
    assert card.criteria.get("min length").thresholds["good"] == 0.0  # 这个 0 是真值


def test_mesh_params_keep_raw_strings(card):
    mp = card.mesh_params
    assert mp.values["target_element_length"] == "5."
    assert mp.values["general_min_target_len"] == "3."
    assert mp.values["general_max_target_len"] == "8."
    # 表达式与枚举必须原样保留，提前转 float 会毁掉它们
    assert mp.values["attach_zones_on_perimeters"] == "0.667*Lmin"
    assert mp.values["bm_features_handling"] == "Recognize features"
    # 段名是网格生成侧的旋钮分类，后续调参靠它定位
    assert mp.sections["target_element_length"] == "General Mesh"
    assert "Fillets" in set(mp.sections.values())
    assert "Holes 2D" in set(mp.sections.values())


# --- 往返保真（这个文件要交回 ANSA，少一个字段都可能读出不同的卡）--------

def test_qual_round_trip_is_byte_identical():
    raw = open(os.path.join(BUILTIN, CRITERIA_FILE), encoding="utf-8", newline="").read()
    assert ansa_qual.loads(raw).dumps() == raw


def test_mpar_round_trip_is_byte_identical():
    raw = open(os.path.join(BUILTIN, MESH_FILE), encoding="utf-8", newline="").read()
    assert ansa_mpar.loads(raw).dumps() == raw


def test_editing_touches_only_the_edited_line():
    raw = open(os.path.join(BUILTIN, CRITERIA_FILE), encoding="utf-8", newline="").read()
    f = ansa_qual.loads(raw)
    f.set_criterion("warping", thresholds={"failed": 12.0})
    out = f.dumps()
    before, after = raw.splitlines(), out.splitlines()
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(changed) == 1, "只应改动一行"
    assert "warping" in after[changed[0]]
    assert ansa_qual.loads(out).data.get("warping").thresholds["failed"] == 12.0


def test_mpar_edit_keeps_other_lines():
    raw = open(os.path.join(BUILTIN, MESH_FILE), encoding="utf-8", newline="").read()
    f = ansa_mpar.loads(raw)
    f.set("target_element_length", "4.")
    out = f.dumps()
    changed = [i for i, (a, b) in enumerate(zip(raw.splitlines(), out.splitlines())) if a != b]
    assert len(changed) == 1
    assert ansa_mpar.loads(out).data.values["target_element_length"] == "4."


# --- 判据引擎 -----------------------------------------------------------

def test_direction_is_derived_not_hardcoded(card):
    assert card.criteria.get("jacobian").higher_is_better is True
    assert card.criteria.get("min length").higher_is_better is True
    assert card.criteria.get("warping").higher_is_better is False
    # 这条是关键：Good/Worst 都是占位 0，若按 Best>Worst 判方向会得出"越大越好"
    assert card.criteria.get("max length").higher_is_better is False


def test_max_length_does_not_judge_backwards(card):
    """12mm 的超长单元必须判废——曾因方向判反而被评成"优秀"。"""
    crit = card.criteria.get("max length")
    ranges = card.criteria.ranges["shells"]
    assert evaluate(crit, 12.0, ranges).passed is False
    assert evaluate(crit, 4.0, ranges).passed is True


def test_grades_across_bands(card):
    warp = card.criteria.get("warping")
    ranges = card.criteria.ranges["shells"]
    assert evaluate(warp, 0.0, ranges).grade == "best"
    assert evaluate(warp, 3.0, ranges).grade == "good"
    assert evaluate(warp, 14.9, ranges).passed is True    # 判废线是 15，14.9 仍合格
    assert evaluate(warp, 20.0, ranges).passed is False
    assert evaluate(warp, 200.0, ranges).grade == "worst"


def test_higher_is_better_criterion(card):
    jac = card.criteria.get("jacobian")
    ranges = card.criteria.ranges["shells"]
    assert evaluate(jac, 0.95, ranges).passed is True
    assert evaluate(jac, 0.5, ranges).passed is False     # failed 线是 0.6
    assert evaluate(jac, 0.7, ranges).passed is True


def test_only_enabled_criteria_are_evaluated(card):
    """卡里关掉的判据不参与——拿工程师没同意的标准判他的网格是越权。"""
    verdicts = evaluate_all(card.criteria, {
        "warping": 20.0,
        "taper": 0.1,          # OFF
        "squish": 0.9,         # OFF
        "jacobian": 0.9,
    })
    assert {v.criterion for v in verdicts} == {"warping", "jacobian"}
    assert [v.passed for v in verdicts if v.criterion == "warping"] == [False]


def test_time_step_and_added_mass():
    """碰撞里最该被看见的两个数：时间步和为抬时间步付出的加质量。"""
    assert estimate_time_step(5.0) == pytest.approx(9.67e-7, rel=0.01)
    assert estimate_time_step(3.0) == pytest.approx(5.80e-7, rel=0.01)
    # 3mm 单元想抬到 1μs 目标，要加约 197% 的质量——远超 5% 的可接受线
    assert added_mass_ratio(estimate_time_step(3.0), 1.0e-6) == pytest.approx(1.97, rel=0.05)
    assert added_mass_ratio(1.0e-6, 1.0e-6) == 0.0


# --- 模板库 -------------------------------------------------------------

def test_builtin_template_is_listed_and_readonly(tmp_path):
    lib = QualityCardLibrary(user_dir=str(tmp_path))
    ids = {t.id for t in lib.list_templates()}
    assert DEFAULT_TEMPLATE_ID in ids
    with pytest.raises(PermissionError):
        lib.delete(DEFAULT_TEMPLATE_ID)


def test_derive_records_every_override_with_its_source(tmp_path):
    """派生出的实例本身就是一份合法 ANSA 卡，且每项改动都留了出处。"""
    lib = QualityCardLibrary(user_dir=str(tmp_path))
    meta = lib.derive(
        DEFAULT_TEMPLATE_ID, "cust-a-4mm", "客户A 4mm 卡",
        overrides=[
            Override("mesh:target_element_length", "", "4.", source="技术协议 3.2 节", by="ai"),
            Override("criteria:warping [shells]:failed", "", "12.0", source="技术协议 表4", by="ai"),
        ],
        source="客户A", revision="A", scope="碰撞/钣金",
    )
    assert meta.based_on == DEFAULT_TEMPLATE_ID
    # 旧值由引擎回填，不信调用方给的
    assert [o.old_value for o in meta.overrides] == ["5.", "15.0"]
    assert all(o.source for o in meta.overrides), "每项覆盖都必须有出处"

    derived = lib.load("cust-a-4mm")
    assert derived.target_element_length == 4.0
    assert derived.criteria.get("warping").thresholds["failed"] == 12.0
    # 没被覆盖的项原样继承
    assert derived.criteria.get("jacobian").thresholds["failed"] == 0.6

    saved = json.load(open(os.path.join(tmp_path, "cust-a-4mm", "template.json"), encoding="utf-8"))
    assert saved["overrides"][0]["source"] == "技术协议 3.2 节"


def test_derive_rejects_unknown_targets(tmp_path):
    lib = QualityCardLibrary(user_dir=str(tmp_path))
    with pytest.raises(KeyError):
        lib.derive(DEFAULT_TEMPLATE_ID, "bad1", "x",
                   overrides=[Override("criteria:no such [shells]:failed", "", "1")])
    with pytest.raises(KeyError):
        lib.derive(DEFAULT_TEMPLATE_ID, "bad2", "x",
                   overrides=[Override("mesh:no_such_key", "", "1")])


def test_failed_derive_leaves_nothing_behind(tmp_path):
    """半张卡比没有卡更危险:它看着完整,还会占住 id 让重试直接失败。"""
    lib = QualityCardLibrary(user_dir=str(tmp_path))
    with pytest.raises(KeyError):
        lib.derive(DEFAULT_TEMPLATE_ID, "half", "x",
                   overrides=[Override("mesh:target_element_length", "", "4."),
                              Override("mesh:no_such_key", "", "1")])
    assert not os.path.exists(os.path.join(tmp_path, "half"))
    # 同一个 id 可以立刻重试
    lib.derive(DEFAULT_TEMPLATE_ID, "half", "x",
               overrides=[Override("mesh:target_element_length", "", "4.", source="重试")])
    assert lib.load("half").target_element_length == 4.0
