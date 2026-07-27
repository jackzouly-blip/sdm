"""deck → GLB 转换：把解析结果组装成按零件分组的网格并写出。

作为独立可执行脚本，供 d3plot 同款的降权执行链路调用
（`run_as_user`），因此读文件用的是目标用户的权限，而不是后端的 root。

    python -m app.sim.deck.convert <deck 路径> <输出 GLB 路径> [最大三角面数]

最后一行输出 JSON 摘要，供调用方读取。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Tuple

from .glb import GlbPart, write_glb
from .parser import Deck, parse_deck, solid_surface_faces, triangulate


def build_parts(deck: Deck, max_triangles: int = 0) -> Tuple[List[GlbPart], Dict]:
    """把解析结果按 PID 组装成零件网格。

    顶点按零件重建局部编号：deck 的节点 id 是全局的且往往稀疏
    （几百万编号里只用了一部分），直接当索引会产生巨量空洞。

    刻意**不跨零件合并顶点**：零件交界处保持各自的顶点，与几何能力契约中
    「不得跨 faceShape 焊接顶点」是同一个道理——合并会把零件边界抹平，
    也会让按零件着色失效。
    """
    parts: List[GlbPart] = []
    dropped_missing = 0
    dropped_parts = 0
    tri_budget = max_triangles if max_triangles and max_triangles > 0 else None
    total_tris = 0

    # 小零件优先。这个顺序是有意的：预算不够时应尽量多保住零件数，
    # 而不是让少数几个致密的实体件（座椅泡沫、假人软组织）吃光预算——
    # 实测整车模型若按大件优先，250 万面预算只装得下 11 个零件，
    # 其余 703 个（骨架、安全带等结构件）全部丢失，那样根本没法"看一眼模型对不对"。
    pids = sorted(
        set(deck.shells) | set(deck.solids),
        key=lambda p: len(deck.shells.get(p, ())) + len(deck.solids.get(p, ())),
    )

    for pid in pids:
        faces: List[Tuple[int, ...]] = list(deck.shells.get(pid, ()))
        solids = deck.solids.get(pid)
        if solids:
            faces.extend(solid_surface_faces(solids))
        if not faces:
            continue

        local: Dict[int, int] = {}
        positions: List[float] = []
        indices: List[int] = []

        for face in faces:
            for tri in triangulate(face):
                ok = True
                idx3 = []
                for nid in tri:
                    li = local.get(nid)
                    if li is None:
                        xyz = deck.nodes.get(nid)
                        if xyz is None:
                            ok = False
                            break
                        li = len(positions) // 3
                        local[nid] = li
                        positions.extend(xyz)
                    idx3.append(li)
                if ok:
                    indices.extend(idx3)
                else:
                    dropped_missing += 1

        if not indices:
            continue
        if tri_budget is not None and total_tris + len(indices) // 3 > tri_budget:
            # 跳过这一个而非就此中断：后面还有更小的零件装得下，
            # break 会把它们一并丢掉。
            dropped_parts += 1
            continue
        total_tris += len(indices) // 3
        parts.append(GlbPart(
            name=deck.part_names.get(pid, f"PART {pid}"),
            part_id=pid,
            positions=positions,
            indices=indices,
        ))

    stats = {
        "sourceFiles": len(deck.files),
        "missingIncludes": deck.missing_includes[:20],
        "missingIncludeCount": len(deck.missing_includes),
        "nodeCount": len(deck.nodes),
        "shellCount": deck.shell_count,
        "solidCount": deck.solid_count,
        # 引用了不存在节点的三角面：模型不完整（include 缺失）时会大量出现
        "droppedTriangles": dropped_missing,
        # 因三角面预算被跳过的零件数；>0 说明看到的模型不完整
        "droppedParts": dropped_parts,
        "truncated": dropped_parts > 0,
    }
    return parts, stats


def convert(deck_path: str, out_path: str, max_triangles: int = 0) -> Dict:
    deck = parse_deck(deck_path)
    parts, stats = build_parts(deck, max_triangles)
    if not parts:
        raise RuntimeError(
            "deck 中没有可渲染的网格（未找到 *ELEMENT_SHELL / *ELEMENT_SOLID，"
            f"或引用的 include 缺失：{stats['missingIncludeCount']} 个）"
        )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    summary = write_glb(parts, out_path)
    summary.update(stats)
    return summary


def main(argv: List[str]) -> int:
    if len(argv) < 3:
        print("用法: python -m app.sim.deck.convert <deck> <out.glb> [最大三角面数]",
              file=sys.stderr)
        return 2
    try:
        summary = convert(argv[1], argv[2],
                          int(argv[3]) if len(argv) > 3 else 0)
    except Exception as e:  # noqa: BLE001
        print(f"转换失败: {e}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
