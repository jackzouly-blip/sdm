"""d3plot -> 网页可渲染资产（独立脚本，由 run_as_user 以登录用户身份调用）。

用法: python extract.py <d3plot文件> <输出目录> [max_states]
依赖: lasso-python, numpy（须装在调用此脚本的 Python 环境里）。
不导入门户任何模块，保持自包含，可用任意带 lasso 的 Python 执行。

产物 <out>/model.bin 布局：
  tri_idx u32[T*3] | tri_part u16[T] | coords f32[S*U*3] |
  bcoords f32[S*B*3] | bidx u32[L*2] | bpart u16[L] | fields f32[nF*S*U]
<out>/model.json：counts + parts(title/region) + fields(name/min/max) + times。
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
from lasso.dyna import D3plot, ArrayType

HEX_FACES = np.array([[0,1,2,3],[4,7,6,5],[0,4,5,1],[1,5,6,2],[2,6,7,3],[3,7,4,0]], np.int64)


def quads_to_tris(q):
    if len(q) == 0:
        return np.zeros((0,3), np.int64)
    return np.vstack([q[:, [0,1,2]], q[:, [0,2,3]]])


def _boundary_mask(faces):
    """faces (M,k)：返回每个面是否为边界面（其节点集合在全体中只出现一次）。"""
    if len(faces) == 0:
        return np.zeros(0, bool)
    key = np.sort(faces, axis=1)
    order = np.lexsort(key.T[::-1]); ks = key[order]
    sp = np.zeros(len(ks), bool); sn = np.zeros(len(ks), bool)
    eq = np.all(ks[1:] == ks[:-1], axis=1); sp[1:] = eq; sn[:-1] = eq
    um = ~(sp | sn)
    res = np.zeros(len(faces), bool); res[order] = um
    return res


# LS-DYNA 8 节点退化实体的节点排布（已由数据验证）：
#   四面体 abcddddd -> 唯一节点 [0,1,2,3]；五面体 abcdeeff -> [0,1,2,3,4,6]
_TET_TRIS = ([0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3])
_WEDGE_TRIS = ([0, 4, 1], [2, 6, 3])
_WEDGE_QUADS = ([0, 1, 2, 3], [1, 4, 6, 2], [3, 6, 4, 0])
_TYPE_COLS = {4: [0, 1, 2, 3], 6: [0, 1, 2, 3, 4, 6], 8: [0, 1, 2, 3, 4, 5, 6, 7]}


def solid_surface(solid, part):
    """按单元类型(四/五/六面体)正确抽外表面，返回 (三角面(K,3), 每面部件号(K,))。"""
    if len(solid) == 0:
        return np.zeros((0, 3), np.int64), np.zeros(0, np.int64)
    s = np.sort(solid, axis=1)
    nuniq = 1 + (np.diff(s, axis=1) != 0).sum(axis=1)
    tris, trip, quads, quadp = [], [], [], []
    m = nuniq == 4
    if m.any():
        nd = solid[m]; p = part[m]
        for c in _TET_TRIS:
            tris.append(nd[:, c]); trip.append(p)
    m = nuniq == 6
    if m.any():
        nd = solid[m]; p = part[m]
        for c in _WEDGE_TRIS:
            tris.append(nd[:, c]); trip.append(p)
        for c in _WEDGE_QUADS:
            quads.append(nd[:, c]); quadp.append(p)
    m = nuniq == 8
    if m.any():
        nd = solid[m]; p = part[m]
        quads.append(nd[:, HEX_FACES.reshape(-1)].reshape(-1, 4))
        quadp.append(np.repeat(p, 6))
    tris = np.vstack(tris) if tris else np.zeros((0, 3), np.int64)
    trip = np.concatenate(trip) if trip else np.zeros(0, np.int64)
    quads = np.vstack(quads) if quads else np.zeros((0, 4), np.int64)
    quadp = np.concatenate(quadp) if quadp else np.zeros(0, np.int64)
    tb = _boundary_mask(tris); qb = _boundary_mask(quads)
    btri, btrip = tris[tb], trip[tb]
    bq, bqp = quads[qb], quadp[qb]
    out_t, out_p = [], []
    if len(btri):
        out_t.append(btri); out_p.append(btrip)
    if len(bq):
        out_t.append(bq[:, [0, 1, 2]]); out_p.append(bqp)
        out_t.append(bq[:, [0, 2, 3]]); out_p.append(bqp)
    if not out_t:
        return np.zeros((0, 3), np.int64), np.zeros(0, np.int64)
    return np.vstack(out_t), np.concatenate(out_p)


def solid_incidence(solid):
    """实体单元的(唯一节点, 单元号)关联，供结果场按单元平均到节点（避免退化重复节点失真）。"""
    if len(solid) == 0:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    s = np.sort(solid, axis=1)
    nuniq = 1 + (np.diff(s, axis=1) != 0).sum(axis=1)
    nodes, elems = [], []
    allidx = np.arange(len(solid))
    for k, cols in _TYPE_COLS.items():
        m = nuniq == k
        if m.any():
            nd = solid[m][:, cols]
            nodes.append(nd.reshape(-1))
            elems.append(np.repeat(allidx[m], len(cols)))
    return np.concatenate(nodes), np.concatenate(elems)


def region_of(title):
    t = title.split("_")
    if len(t) >= 2 and t and t[0][:2].isdigit():
        return t[1]
    return t[0] if t else "OTHER"


def _rng(arr):
    """返回数组有限值的 (min, max)；全为 NaN 时返回 (0, 0)。"""
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0, 0.0
    return float(finite.min()), float(finite.max())


def _prange(arr, plo=1.0, phi=99.0):
    """色阶范围:取 1%/99% 分位，避免个别异常值(如塑性应变 -27/41)拉爆图例色阶。
    全为 NaN 返回 (0,0)；上下相等时给一点宽度避免除零。"""
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0, 0.0
    lo, hi = (float(x) for x in np.percentile(finite, [plo, phi]))
    if hi <= lo:
        hi = lo + 1e-6
    return lo, hi


def decimate(tris, tri_part_raw, coords_full, node_fields_full, target_tris):
    """体素聚类减面：按(部件, 空间网格)聚类顶点生成新顶点集；多状态坐标与各
    结果场按聚类内"三角顶点出现"平均。三角面重映射后丢弃塌缩(退化)面。
    部件并入聚类键，避免跨部件焊接，部件着色/显隐/拾取仍按三角面(tri_part)成立。
    返回 (tris_u32(T2,3), tri_part(T2,), coords(S,C,3), [(name,arr(S,C))], C)。
    """
    S, N, _ = coords_full.shape
    T = len(tris)
    rest = coords_full[0]
    cn = tris.reshape(-1)                       # (3T,) 原节点号
    cp = np.repeat(tri_part_raw, 3)             # (3T,) 部件索引
    pts = rest[cn]                              # (3T,3) 初始坐标
    lo = pts.min(0)
    # 由表面积估算网格边长，使聚类(≈顶点)数 ≈ target_tris/2
    v0, v1, v2 = rest[tris[:, 0]], rest[tris[:, 1]], rest[tris[:, 2]]
    area = float(0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1).sum())
    target_verts = max(2000, target_tris // 2)
    span = float(np.linalg.norm(pts.max(0) - lo)) or 1.0
    cell = (area / target_verts) ** 0.5 if area > 0 else span / 256.0
    cell = max(cell, span * 1e-4)
    gi = np.floor((pts - lo) / cell).astype(np.int64)
    dims = gi.max(0) + 1
    cellkey = (gi[:, 0] * dims[1] + gi[:, 1]) * dims[2] + gi[:, 2]
    P = int(cp.max()) + 1 if len(cp) else 1
    key = cellkey * P + cp                      # (网格, 部件) 合并为一维键
    _uniq, inv = np.unique(key, return_inverse=True)
    inv = inv.astype(np.int64)
    C = len(_uniq)
    new_tris = inv.reshape(T, 3)
    good = ((new_tris[:, 0] != new_tris[:, 1]) &
            (new_tris[:, 1] != new_tris[:, 2]) &
            (new_tris[:, 0] != new_tris[:, 2]))
    out_tris = new_tris[good].astype(np.uint32)
    out_part = tri_part_raw[good]
    # 聚类平均用稀疏矩阵一次算(等价于原逐帧 bincount，数值完全一致，但快得多)：
    # M[c, node] = 该节点在聚类 c 中出现的角点数；聚类和 = M @ 节点值。nnz 因合并重复
    # (cluster,node) 远小于 3T，故 M@坐标/场 比逐帧 gather+bincount 快一个量级。
    from scipy import sparse
    M = sparse.coo_matrix(
        (np.ones(len(inv), np.float64), (inv, cn)), shape=(C, N)
    ).tocsr()
    cnt = np.asarray(M.sum(axis=1)).ravel()
    cnt_safe = np.where(cnt > 0, cnt, 1.0)[:, None]
    coords = np.empty((S, C, 3), np.float32)
    for s in range(S):
        coords[s] = (M @ coords_full[s] / cnt_safe).astype(np.float32)
    out_fields = []
    for name, arr in node_fields_full:
        fn = np.full((S, C), np.nan, np.float32)
        for s in range(S):
            v = arr[s]
            valid = np.isfinite(v)
            num = M @ np.where(valid, v, 0.0).astype(np.float64)
            den = M @ valid.astype(np.float64)
            nz = den > 0
            fn[s, nz] = (num[nz] / den[nz]).astype(np.float32)
        out_fields.append((name, fn))
    return out_tris, out_part, coords, out_fields, C


def main():
    src, out_dir = sys.argv[1], sys.argv[2]
    max_states = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    tri_budget = int(sys.argv[4]) if len(sys.argv) > 4 else 1_200_000
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.time()

    # 限帧时：先轻量预读拿总帧数，再用 state_filter 只读抽样到的帧
    # （避免把全部帧读进内存再丢弃——大幅减少 I/O 与内存，对长仿真尤其明显）
    state_filter = None
    if max_states and max_states > 0:
        d0 = D3plot(src, state_array_filter=[ArrayType.global_timesteps])
        t0arr = d0.arrays.get(ArrayType.global_timesteps)
        s_total = len(t0arr) if t0arr is not None else 0
        del d0
        if s_total and s_total > max_states:
            selidx = np.unique(np.linspace(0, s_total - 1, max_states).astype(int))
            state_filter = set(int(x) for x in selidx)

    d = D3plot(src, state_array_filter=[
        ArrayType.node_displacement,
        ArrayType.element_solid_stress,
        ArrayType.element_solid_effective_plastic_strain,
        ArrayType.element_shell_is_alive,
        ArrayType.element_solid_is_alive,
        ArrayType.element_beam_is_alive,
        ArrayType.global_timesteps,
    ], state_filter=state_filter)
    a = d.arrays
    nodes = a[ArrayType.node_coordinates].astype(np.float32)
    disp = a[ArrayType.node_displacement].astype(np.float32)  # state_filter 已只返回抽样帧
    S, N = disp.shape[0], disp.shape[1]
    times = a.get(ArrayType.global_timesteps)
    times = None if times is None else np.asarray(times, np.float64).tolist()
    t_read = round(time.time() - t0, 1)  # 读取(解析)耗时

    pid = a[ArrayType.part_ids]
    ptids = a.get(ArrayType.part_titles_ids); ptitles = a.get(ArrayType.part_titles)
    id2title = {}
    if ptids is not None and ptitles is not None:
        for i, tt in zip(np.asarray(ptids), ptitles):
            id2title[int(i)] = tt.decode("latin1","replace").strip() if isinstance(tt,(bytes,bytearray)) else str(tt).strip()
    title_of = lambda p: id2title.get(int(pid[p]), f"PART_{int(pid[p])}")

    shell = a.get(ArrayType.element_shell_node_indexes); shell_p = a.get(ArrayType.element_shell_part_indexes)
    solid = a.get(ArrayType.element_solid_node_indexes); solid_p = a.get(ArrayType.element_solid_part_indexes)
    beam = a.get(ArrayType.element_beam_node_indexes); beam_p = a.get(ArrayType.element_beam_part_indexes)

    # 剔除被删除(侵蚀失效)的单元：碰撞后期失效单元的节点会飞到极远处形成巨大尖刺
    # （已诊断：飞走节点 100% 属于被删壳）。在所渲染帧中任一帧被删则整体剔除。
    def _alive_mask(at, n):
        arr = a.get(at)
        if arr is None or not n:
            return None  # 无删除信息 → 全保留
        m = (np.asarray(arr) != 0).all(axis=0)  # state_filter 已只含抽样帧
        return None if m.all() else m  # 全存活则无需过滤

    if shell is not None and len(shell):
        m = _alive_mask(ArrayType.element_shell_is_alive, len(shell))
        if m is not None:
            shell = shell[m]; shell_p = np.asarray(shell_p)[m]
    if beam is not None and len(beam):
        m = _alive_mask(ArrayType.element_beam_is_alive, len(beam))
        if m is not None:
            beam = beam[m]; beam_p = np.asarray(beam_p)[m]
    solid_mask = None
    if solid is not None and len(solid):
        solid_mask = _alive_mask(ArrayType.element_solid_is_alive, len(solid))
        if solid_mask is not None:
            solid = solid[solid_mask]; solid_p = np.asarray(solid_p)[solid_mask]

    tri_list, trip_list = [], []
    n_shell_tris = 0
    if shell is not None and len(shell):
        sh = quads_to_tris(shell[:, :4].astype(np.int64))
        n_shell_tris = len(sh)
        tri_list.append(sh)
        trip_list.append(np.concatenate([shell_p, shell_p]))
    if solid is not None and len(solid):
        st, spf = solid_surface(solid[:, :8].astype(np.int64), np.asarray(solid_p).astype(np.int64))
        tri_list.append(st); trip_list.append(spf)
    tris = np.vstack(tri_list).astype(np.int64)
    tri_part_raw = np.concatenate(trip_list).astype(np.int64)

    # 实体结果场先算到“全节点”数组(S,N)（带 NaN），供精确/减面两条路径共用
    node_fields_full = []  # [(name, arr(S,N))]
    if solid is not None and len(solid):
        inc_n, inc_e = solid_incidence(solid[:, :8].astype(np.int64))
        cnt_n = np.bincount(inc_n, minlength=N).astype(np.float64)

        def e2n_full(val_s):
            out = np.full((S, N), np.nan, np.float32)
            for s in range(S):
                w = val_s[s][inc_e]
                with np.errstate(invalid="ignore", divide="ignore"):
                    out[s] = (np.bincount(inc_n, weights=w, minlength=N) / cnt_n).astype(np.float32)
            return out

        stress = a.get(ArrayType.element_solid_stress)
        if stress is not None:  # state_filter 已只含抽样帧
            if solid_mask is not None:
                stress = stress[:, solid_mask]  # 与已过滤的实体连通性对齐
            sx,sy,sz,sxy,syz,szx = (stress[:, :, 0, k] for k in range(6))
            vm = np.sqrt(0.5*((sx-sy)**2+(sy-sz)**2+(sz-sx)**2)+3*(sxy**2+syz**2+szx**2))
            node_fields_full.append(("von Mises 应力", e2n_full(vm)))
        eps = a.get(ArrayType.element_solid_effective_plastic_strain)
        if eps is not None:
            e = eps[:, :, 0]
            if solid_mask is not None:
                e = e[:, solid_mask]
            node_fields_full.append(("有效塑性应变", e2n_full(e)))

    n_tris_full = int(len(tris))
    decimating = bool(tri_budget) and n_tris_full > tri_budget
    if decimating:
        # 服务器端体素聚类减面（整车级模型避免浏览器卡死）
        tris_r, tri_part_raw, coords_s, vert_fields, U = decimate(
            tris, tri_part_raw, disp, node_fields_full, tri_budget)
        n_shell_tris = 0
    else:
        used = np.unique(tris)
        remap = np.full(N, -1, np.int64); remap[used] = np.arange(len(used))
        tris_r = remap[tris].astype(np.uint32); U = len(used)
        coords_s = disp[:, used, :]
        vert_fields = [(name, arr[:, used]) for name, arr in node_fields_full]

    fields = []
    mag = np.linalg.norm(coords_s - coords_s[0:1], axis=2).astype(np.float32)
    fields.append(("变形幅值", mag, float(mag.min()), float(mag.max())))
    for name, arr in vert_fields:
        mn, mx = _rng(arr)
        fields.append((name, arr, mn, mx))

    if beam is not None and len(beam):
        bp = beam[:, :2].astype(np.int64); bnodes = np.unique(bp)
        br = np.full(N, -1, np.int64); br[bnodes] = np.arange(len(bnodes))
        bidx = br[bp].astype(np.uint32); bcoords = disp[:, bnodes, :].astype(np.float32)
        beam_part_raw = np.asarray(beam_p).astype(np.int64); B, L = len(bnodes), len(bidx)
    else:
        bidx = np.zeros((0,2), np.uint32); bcoords = np.zeros((S,0,3), np.float32)
        beam_part_raw = np.zeros((0,), np.int64); B = L = 0

    all_parts = np.unique(np.concatenate([tri_part_raw, beam_part_raw]))
    dense = {int(p): i for i, p in enumerate(all_parts)}
    tri_part = np.array([dense[int(p)] for p in tri_part_raw], np.uint16)
    beam_part = np.array([dense[int(p)] for p in beam_part_raw], np.uint16)
    parts_table = [{"title": title_of(p), "region": region_of(title_of(p))} for p in all_parts]

    bp_path = os.path.join(out_dir, "model.bin")
    # 布局：先所有 u32/f32(保证 4 字节对齐，前端用 Float32Array 视图零拷贝)，
    # 再把 u16(tri_part/beam_part)放末尾(u16 仅需 2 字节对齐)。
    with open(bp_path, "wb") as f:
        tris_r.astype("<u4").tofile(f)
        coords_s.astype("<f4").tofile(f); bcoords.astype("<f4").tofile(f)
        bidx.astype("<u4").tofile(f)
        tri_part.astype("<u2").tofile(f); beam_part.astype("<u2").tofile(f)
    # 云图场分文件存放，前端按需下载，避免默认 payload 过大（慢链路友好）。
    # 变形幅值(index 0)前端用 coords 现算，不落盘；其余每个场一个 field_<i>.bin (f32[S*U])。
    field_meta = []
    for i, (name, arr, mn, mx) in enumerate(fields):
        lo, hi = _prange(arr)  # 1%/99% 分位色阶
        entry = {"name": name, "min": mn, "max": mx, "lo": lo, "hi": hi}
        if i == 0:
            entry["kind"] = "deformation"  # 前端按 coords-coords[0] 现算
        else:
            fn = f"field_{i}.bin"
            arr.astype("<f4").tofile(os.path.join(out_dir, fn))
            entry["file"] = fn
        field_meta.append(entry)
    manifest = {
        "n_verts": U, "n_tris": int(len(tris_r)), "n_states": S,
        "decimated": bool(decimating), "n_tris_full": n_tris_full,
        "n_shell_tris": int(n_shell_tris),
        "n_beam_verts": B, "n_beam_lines": int(L),
        "n_parts": len(parts_table), "parts": parts_table,
        "fields": field_meta,
        "times": times,
        "layout": ["tri_idx:u32[T*3]","coords:f32[S*U*3]","bcoords:f32[S*B*3]",
                   "bidx:u32[L*2]","tri_part:u16[T]","bpart:u16[L]",
                   "fields: 分文件 field_<i>.bin f32[S*U]，index0=变形幅值前端现算"],
        "bytes": os.path.getsize(bp_path), "t_read": t_read, "elapsed": round(time.time()-t0, 1),
    }
    with open(os.path.join(out_dir, "model.json"), "w") as f:
        json.dump(manifest, f, ensure_ascii=False)
    # 摘要打印到 stdout，供调用方捕获
    print(json.dumps({k: manifest[k] for k in ("n_verts","n_tris","n_tris_full","decimated","n_states","n_beam_lines","n_parts","fields","bytes","t_read","elapsed")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
