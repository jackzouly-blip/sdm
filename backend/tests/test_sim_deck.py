"""LS-DYNA deck 解析与 GLB 导出测试。

重点覆盖格式上容易写错的几处：定长/自由格式混用、*INCLUDE 的续行与相对路径基准、
三角壳的 n4 退化、实体单元只取外表面、以及 GLB 的二进制结构是否合规。
"""
import json
import struct

import pytest

from app.sim.deck.convert import build_parts, convert
from app.sim.deck.glb import GlbPart, write_glb
from app.sim.deck.parser import (
    parse_deck,
    solid_surface_faces,
    triangulate,
)


def w(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


# 定长格式：id 占 8 列，坐标各占 16 列
FIXED_DECK = """\
*KEYWORD
$ 这是注释，应被忽略
*NODE
       1             0.0             0.0             0.0
       2            10.0             0.0             0.0
       3            10.0            10.0             0.0
       4             0.0            10.0             0.0
*PART
车体外板
       7       1       1
*ELEMENT_SHELL
       1       7       1       2       3       4
*END
"""


def test_fixed_format(tmp_path):
    d = parse_deck(w(tmp_path / "a.k", FIXED_DECK))
    assert len(d.nodes) == 4
    assert d.nodes[2] == (10.0, 0.0, 0.0)
    assert d.shells[7] == [(1, 2, 3, 4)]
    assert d.part_names[7] == "车体外板"


def test_free_format(tmp_path):
    deck = """\
*NODE
1, 0.0, 0.0, 0.0
2, 1.0, 0.0, 0.0
3, 1.0, 1.0, 0.0
*ELEMENT_SHELL
1, 5, 1, 2, 3, 3
*END
"""
    d = parse_deck(w(tmp_path / "a.k", deck))
    assert len(d.nodes) == 3
    assert d.shells[5] == [(1, 2, 3, 3)]


def test_mixed_format_in_one_file(tmp_path):
    """同一文件里定长与自由格式混用 —— 不同工具导出的 include 各写各的。"""
    deck = """\
*NODE
       1             0.0             0.0             0.0
2, 1.0, 0.0, 0.0
*END
"""
    d = parse_deck(w(tmp_path / "a.k", deck))
    assert d.nodes[1] == (0.0, 0.0, 0.0)
    assert d.nodes[2] == (1.0, 0.0, 0.0)


def test_triangular_shell_degenerate_n4(tmp_path):
    """三角壳的 n4 缺省或等于 n3，不能当成第四个顶点。"""
    deck = """\
*NODE
1, 0,0,0
2, 1,0,0
3, 0,1,0
*ELEMENT_SHELL
1, 9, 1, 2, 3, 0
*END
"""
    d = parse_deck(w(tmp_path / "a.k", deck))
    assert d.shells[9] == [(1, 2, 3, 3)], "n4=0 应回落为 n3"
    assert triangulate((1, 2, 3, 3)) == [(1, 2, 3)]
    assert len(triangulate((1, 2, 3, 4))) == 2


# --- *INCLUDE ------------------------------------------------------------

def test_include_resolved_relative_to_including_file(tmp_path):
    """相对路径基于**包含它的文件**所在目录，不是主控文件。"""
    w(tmp_path / "sub" / "deep" / "mesh.k", """\
*NODE
1, 0,0,0
2, 1,0,0
3, 0,1,0
*ELEMENT_SHELL
1, 1, 1, 2, 3, 3
*END
""")
    # sub/mid.k 引用同目录下的 deep/mesh.k
    w(tmp_path / "sub" / "mid.k", "*INCLUDE\ndeep/mesh.k\n*END\n")
    master = w(tmp_path / "master.key", "*INCLUDE\nsub/mid.k\n*END\n")

    d = parse_deck(master)
    assert len(d.files) == 3
    assert len(d.nodes) == 3
    assert d.shell_count == 1
    assert d.missing_includes == []


def test_include_line_continuation(tmp_path):
    """长路径用行尾 + 续行（实测模型里确实这么写）。"""
    w(tmp_path / "a" / "b" / "c" / "mesh.k", "*NODE\n1, 0,0,0\n*END\n")
    master = w(tmp_path / "m.key", "*INCLUDE\na/b/+\nc/mesh.k\n*END\n")

    d = parse_deck(master)
    assert len(d.nodes) == 1
    assert d.missing_includes == []


def test_missing_include_is_reported_not_silent(tmp_path):
    """include 缺失必须报出来 —— 否则模型少一半却看不出问题。"""
    master = w(tmp_path / "m.key", "*INCLUDE\nnope/gone.k\n*END\n")
    d = parse_deck(master)
    assert len(d.missing_includes) == 1
    assert "gone.k" in d.missing_includes[0]


def test_include_cycle_terminates(tmp_path):
    a = w(tmp_path / "a.k", "*INCLUDE\nb.k\n*END\n")
    w(tmp_path / "b.k", "*INCLUDE\na.k\n*END\n")
    d = parse_deck(a)  # 不应无限递归
    assert len(d.files) == 2


def test_unrelated_keywords_are_skipped(tmp_path):
    """材料/接触/约束等卡不该污染节点或单元。"""
    deck = """\
*NODE
1, 0,0,0
*MAT_PIECEWISE_LINEAR_PLASTICITY
1, 7.8e-9, 210.0, 0.3
*SET_NODE_LIST
1, 2, 3, 4
*CONTACT_AUTOMATIC_SINGLE_SURFACE
1, 1, 0, 0
*END
"""
    d = parse_deck(w(tmp_path / "a.k", deck))
    assert len(d.nodes) == 1
    assert d.shell_count == 0


# --- 实体单元外表面 -------------------------------------------------------

def test_solid_interior_faces_are_dropped():
    """两个相邻六面体：共享面出现两次，应被剔除。"""
    hex_a = (1, 2, 3, 4, 5, 6, 7, 8)
    hex_b = (5, 6, 7, 8, 9, 10, 11, 12)  # 与 A 共享 5-6-7-8 面
    faces = solid_surface_faces([hex_a, hex_b])
    # 单个六面体 6 个面，两个共 12 个，去掉共享的 2 份 → 10 个
    assert len(faces) == 10
    shared = tuple(sorted((5, 6, 7, 8)))
    assert all(tuple(sorted(set(f))) != shared for f in faces)


def test_single_solid_keeps_all_six_faces():
    assert len(solid_surface_faces([(1, 2, 3, 4, 5, 6, 7, 8)])) == 6


# --- GLB 导出 -------------------------------------------------------------

def read_glb(path):
    with open(path, "rb") as f:
        data = f.read()
    magic, ver, total = struct.unpack("<III", data[:12])
    jlen, jtype = struct.unpack("<II", data[12:20])
    jchunk = data[20 : 20 + jlen]
    blen, btype = struct.unpack("<II", data[20 + jlen : 28 + jlen])
    return {
        "magic": magic, "version": ver, "total": total, "actual": len(data),
        "json": json.loads(jchunk.decode("utf-8")),
        "json_type": jtype, "bin_type": btype, "bin_len": blen,
    }


def test_glb_container_is_wellformed(tmp_path):
    out = str(tmp_path / "m.glb")
    stats = write_glb([GlbPart("零件A", 7, [0, 0, 0, 1, 0, 0, 0, 1, 0], [0, 1, 2])], out)

    g = read_glb(out)
    assert g["magic"] == 0x46546C67 and g["version"] == 2
    assert g["total"] == g["actual"], "头里的总长必须与实际字节数一致"
    assert g["json_type"] == 0x4E4F534A and g["bin_type"] == 0x004E4942
    assert g["bin_len"] % 4 == 0, "BIN 块必须 4 字节对齐"
    assert stats["triangleCount"] == 1 and stats["partCount"] == 1


def test_glb_carries_part_id_in_extras(tmp_path):
    """零件标识放 extras.partId —— 与几何能力契约对装配的要求一致。"""
    out = str(tmp_path / "m.glb")
    write_glb([GlbPart("车体外板", 42, [0, 0, 0, 1, 0, 0, 0, 1, 0], [0, 1, 2])], out)

    node = read_glb(out)["json"]["nodes"][0]
    assert node["name"] == "车体外板"
    assert node["extras"]["partId"] == "42"


def test_glb_position_accessor_has_min_max(tmp_path):
    """glTF 规范要求 POSITION 访问器带 min/max，缺了很多查看器会拒载。"""
    out = str(tmp_path / "m.glb")
    write_glb([GlbPart("p", 1, [0, 0, 0, 2, 0, 0, 0, 3, 0], [0, 1, 2])], out)

    acc = read_glb(out)["json"]["accessors"][0]
    assert acc["min"] == [0, 0, 0] and acc["max"] == [2, 3, 0]


def test_shells_are_double_sided(tmp_path):
    """壳单元没有朝向约定，单面渲染会大片消失。"""
    out = str(tmp_path / "m.glb")
    write_glb([GlbPart("p", 1, [0, 0, 0, 1, 0, 0, 0, 1, 0], [0, 1, 2])], out)
    assert read_glb(out)["json"]["materials"][0]["doubleSided"] is True


# --- 端到端 ---------------------------------------------------------------

def test_convert_end_to_end(tmp_path):
    deck = w(tmp_path / "m.k", FIXED_DECK)
    out = str(tmp_path / "m.glb")
    summary = convert(deck, out)

    assert summary["partCount"] == 1
    assert summary["triangleCount"] == 2, "一个四边形壳应拆成两个三角形"
    assert summary["nodeCount"] == 4
    g = read_glb(out)
    assert g["json"]["nodes"][0]["extras"]["partId"] == "7"


def test_convert_reports_dropped_triangles_on_missing_nodes(tmp_path):
    """单元引用了不存在的节点（include 缺失的典型症状）要计数报出。"""
    deck = """\
*NODE
1, 0,0,0
2, 1,0,0
3, 0,1,0
*ELEMENT_SHELL
1, 1, 1, 2, 3, 3
2, 1, 1, 2, 999, 999
*END
"""
    _, stats = build_parts(parse_deck(w(tmp_path / "a.k", deck)))
    assert stats["droppedTriangles"] == 1


def test_convert_raises_when_no_mesh(tmp_path):
    deck = w(tmp_path / "a.k", "*KEYWORD\n*MAT_ELASTIC\n1, 1.0\n*END\n")
    with pytest.raises(RuntimeError, match="没有可渲染的网格"):
        convert(deck, str(tmp_path / "o.glb"))


def test_budget_keeps_as_many_parts_as_possible(tmp_path):
    """预算不够时应跳过大件、保住多数小件，而不是就此中断把后面全丢掉。

    实测教训：整车模型按"大件优先 + 超预算即 break"，250 万面预算只装下 11 个
    零件，其余 703 个结构件全丢——那样根本没法判断模型对不对。
    """
    # 一个 4 面的大零件 + 三个 2 面的小零件
    lines = ["*NODE"]
    for i in range(1, 13):
        lines.append(f"{i}, {i}.0, 0.0, 0.0")
    lines.append("*ELEMENT_SHELL")
    lines.append("1, 100, 1, 2, 3, 4")   # 大件：四边形→2 面
    lines.append("2, 100, 5, 6, 7, 8")   # 大件再加 2 面
    for k, pid in enumerate((201, 202, 203)):
        lines.append(f"{10+k}, {pid}, 1, 2, 3, 3")  # 各 1 面
    lines.append("*END")
    deck = parse_deck(w(tmp_path / "a.k", "\n".join(lines) + "\n"))

    # 预算 3 面：装不下 4 面的大件，但三个小件都能进
    parts, stats = build_parts(deck, max_triangles=3)
    pids = {p.part_id for p in parts}

    assert 100 not in pids, "超预算的大件应被跳过"
    assert pids == {201, 202, 203}, "小件不应因大件超预算被连带丢掉"
    assert stats["droppedParts"] == 1
    assert stats["truncated"] is True


def test_no_truncation_when_budget_sufficient(tmp_path):
    deck = parse_deck(w(tmp_path / "a.k", FIXED_DECK))
    _, stats = build_parts(deck, max_triangles=1000)
    assert stats["droppedParts"] == 0 and stats["truncated"] is False


def test_parts_do_not_share_vertices(tmp_path):
    """零件之间不合并顶点：合并会抹平零件边界，也让按零件着色失效。"""
    deck = """\
*NODE
1, 0,0,0
2, 1,0,0
3, 0,1,0
*ELEMENT_SHELL
1, 10, 1, 2, 3, 3
2, 20, 1, 2, 3, 3
*END
"""
    parts, _ = build_parts(parse_deck(w(tmp_path / "a.k", deck)))
    assert len(parts) == 2
    # 两个零件各自复制了这三个共享节点
    assert all(len(p.positions) == 9 for p in parts)
