"""最小 GLB（glTF 2.0 二进制）写出器。

**不引入新依赖**：GLB 是「12 字节头 + JSON 块 + BIN 块」的简单容器，
手写约百来行，比为它引入 pygltflib/trimesh 更划算——生产环境少一个依赖，
少一处版本冲突来源。

产物刻意与 vektor3d 的 geometry.convert 输出对齐（见
docs/vektor3d-geometry-capability-contract.md）：**每个零件一个 node**，
node 上带 `extras.partId`。这样 deck 网格与将来的 CAD 轻量化产物走同一条展示链路，
`GeometryViewer` 的装配验收读数对两者同样有效。
"""
from __future__ import annotations

import json
import struct
from typing import Dict, List, Optional, Sequence, Tuple

# glTF 常量
_ARRAY_BUFFER = 34962
_ELEMENT_ARRAY_BUFFER = 34963
_FLOAT = 5126
_UNSIGNED_INT = 5125
_TRIANGLES = 4


class GlbPart:
    """一个零件的网格：顶点与三角面索引均为该零件内部编号。"""

    __slots__ = ("name", "part_id", "positions", "indices", "color")

    def __init__(
        self,
        name: str,
        part_id: int,
        positions: Sequence[float],
        indices: Sequence[int],
        color: Optional[Tuple[float, float, float]] = None,
    ):
        self.name = name
        self.part_id = part_id
        self.positions = positions
        self.indices = indices
        self.color = color


def _pad4(b: bytes, fill: bytes = b"\x00") -> bytes:
    r = len(b) % 4
    return b if r == 0 else b + fill * (4 - r)


def _color_for(pid: int) -> Tuple[float, float, float]:
    """按 PID 生成稳定的区分色。

    金色角（0.618）取模能让相邻 PID 的色相拉开，避免几十个零件挤在相近颜色里
    分不清——deck 里没有颜色信息，只能自己生成。
    """
    import colorsys

    h = (pid * 0.6180339887) % 1.0
    return colorsys.hsv_to_rgb(h, 0.45, 0.85)


def write_glb(parts: List[GlbPart], out_path: str, unit: str = "mm") -> Dict:
    """把各零件写成单个 GLB。返回统计信息。

    每个零件一个 mesh + 一个 node，node 的 extras 带 partId ——
    与几何能力契约对装配的要求一致，前端可据此做零件级选择。
    """
    bin_chunks: List[bytes] = []
    offset = 0
    buffer_views: List[Dict] = []
    accessors: List[Dict] = []
    meshes: List[Dict] = []
    nodes: List[Dict] = []
    materials: List[Dict] = []

    total_tris = 0
    total_verts = 0

    for p in parts:
        if not p.indices or not p.positions:
            continue

        pos_bytes = struct.pack(f"<{len(p.positions)}f", *p.positions)
        idx_bytes = struct.pack(f"<{len(p.indices)}I", *p.indices)

        # 包围盒：glTF 规范要求 POSITION 访问器必须带 min/max
        xs = p.positions[0::3]
        ys = p.positions[1::3]
        zs = p.positions[2::3]
        pos_min = [min(xs), min(ys), min(zs)]
        pos_max = [max(xs), max(ys), max(zs)]

        bin_chunks.append(pos_bytes)
        buffer_views.append({
            "buffer": 0, "byteOffset": offset,
            "byteLength": len(pos_bytes), "target": _ARRAY_BUFFER,
        })
        pos_view = len(buffer_views) - 1
        offset += len(pos_bytes)

        bin_chunks.append(idx_bytes)
        buffer_views.append({
            "buffer": 0, "byteOffset": offset,
            "byteLength": len(idx_bytes), "target": _ELEMENT_ARRAY_BUFFER,
        })
        idx_view = len(buffer_views) - 1
        offset += len(idx_bytes)

        accessors.append({
            "bufferView": pos_view, "componentType": _FLOAT,
            "count": len(p.positions) // 3, "type": "VEC3",
            "min": pos_min, "max": pos_max,
        })
        pos_acc = len(accessors) - 1
        accessors.append({
            "bufferView": idx_view, "componentType": _UNSIGNED_INT,
            "count": len(p.indices), "type": "SCALAR",
        })
        idx_acc = len(accessors) - 1

        r, g, b = p.color or _color_for(p.part_id)
        materials.append({
            "name": f"PID {p.part_id}",
            "pbrMetallicRoughness": {
                "baseColorFactor": [r, g, b, 1.0],
                "metallicFactor": 0.1, "roughnessFactor": 0.8,
            },
            "doubleSided": True,  # 壳单元没有朝向约定，单面渲染会大片消失
        })
        mat = len(materials) - 1

        meshes.append({
            "name": p.name,
            "primitives": [{
                "attributes": {"POSITION": pos_acc},
                "indices": idx_acc, "material": mat, "mode": _TRIANGLES,
            }],
        })
        nodes.append({
            "name": p.name,
            "mesh": len(meshes) - 1,
            # 与几何能力契约一致：零件标识放 extras.partId。
            # deck 的 PID 天生稳定（改模型不会变），正合"重新转换后不变"的要求。
            "extras": {"partId": str(p.part_id), "source": "lsdyna-deck"},
        })

        total_tris += len(p.indices) // 3
        total_verts += len(p.positions) // 3

    bin_blob = _pad4(b"".join(bin_chunks))

    gltf = {
        "asset": {"version": "2.0", "generator": f"SDM deck->glTF (unit={unit})"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": materials,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_blob)}],
    }

    json_blob = _pad4(
        json.dumps(gltf, separators=(",", ":")).encode("utf-8"), b" "
    )

    total_len = 12 + 8 + len(json_blob) + 8 + len(bin_blob)
    with open(out_path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total_len))  # 'glTF', ver 2
        f.write(struct.pack("<II", len(json_blob), 0x4E4F534A))  # 'JSON'
        f.write(json_blob)
        f.write(struct.pack("<II", len(bin_blob), 0x004E4942))   # 'BIN\0'
        f.write(bin_blob)

    return {
        "format": "glb",
        "bytes": total_len,
        "partCount": len(nodes),
        "triangleCount": total_tris,
        "vertexCount": total_verts,
        "unit": unit,
    }
