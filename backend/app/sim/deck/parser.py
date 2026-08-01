"""LS-DYNA 关键字文件解析（只取渲染网格所需的部分）。

**为什么自己写**：`lasso-python`（d3plot 查看器在用）只提供 `D3plot` 与 `Binout`，
没有关键字文件读取器；实测真实模型是主控 .key + 嵌套 *INCLUDE 树，
需要自行解析。

**只解析渲染需要的四类卡**：`*NODE`、`*ELEMENT_SHELL`、`*ELEMENT_SOLID`、`*PART`，
外加 `*INCLUDE` 用于展开引用树。其余卡片（材料、接触、约束…）一律跳过——
这里的目的是"提交前看一眼模型对不对"，不是复现求解器的语义。

格式要点（这几条是最容易写错的地方）：

  - `$` 开头是注释，`*` 开头是关键字，其余是数据行。
  - 同一份文件里定长与自由格式可以混用，故**逐行判定**而非整文件判定：
    含逗号走自由格式，否则按固定列宽切分。
  - `*INCLUDE` 的路径在下一非注释行；路径过长时用行尾 `+` 续行
    （实测模型里确实这么写）。
  - **相对路径基于主控 deck 所在目录**（即求解器的运行目录），
    不是基于包含它的那个文件。实测验证过：嵌套 include 里写的
    `00_Database/5_Dummy/P1/xxx.k` 要从主控目录解析才找得到。
  - `*INCLUDE` 可以连写多个文件名（每行一个）；而 `*INCLUDE_TRANSFORM` 等
    带后缀的变体**只有第一行是文件名**，其后是 idnoff/transform 等数据卡，
    误当成文件名会得到一堆 `50000000...` 之类的假路径。
  - `*INCLUDE_PATH` 声明的是搜索目录而非文件，需并入搜索路径。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ...logger import get_logger

log = get_logger(__name__)

# 只有这些卡需要进入解析状态机，其余一律跳过
_NODE = "*NODE"
_ELEMENT_SHELL = "*ELEMENT_SHELL"
_ELEMENT_SOLID = "*ELEMENT_SOLID"
_PART = "*PART"
_INCLUDE = "*INCLUDE"

# 定长格式的列宽
_W_ID = 8      # 各类 id
_W_COORD = 16  # 节点坐标


@dataclass
class Deck:
    """解析结果。节点与单元用扁平列表存，便于直接写进 GLB 缓冲区。"""

    # nid -> (x, y, z)
    nodes: Dict[int, Tuple[float, float, float]] = field(default_factory=dict)
    # pid -> [(n1,n2,n3,n4), ...]，三角单元的 n4 == n3
    shells: Dict[int, List[Tuple[int, int, int, int]]] = field(default_factory=dict)
    # pid -> [(n1..n8), ...]
    solids: Dict[int, List[Tuple[int, ...]]] = field(default_factory=dict)
    # pid -> 名称
    part_names: Dict[int, str] = field(default_factory=dict)
    # 展开过的文件（绝对路径），用于报告与防环
    files: List[str] = field(default_factory=list)
    # 未能解析的 include 路径，交给上层提示而不是静默丢失
    missing_includes: List[str] = field(default_factory=list)

    @property
    def shell_count(self) -> int:
        return sum(len(v) for v in self.shells.values())

    @property
    def solid_count(self) -> int:
        return sum(len(v) for v in self.solids.values())


def _fields_free(line: str) -> List[str]:
    return [f.strip() for f in line.split(",")]


def _fields_fixed(line: str, width: int, count: int) -> List[str]:
    out = []
    for i in range(count):
        out.append(line[i * width : (i + 1) * width].strip())
    return out


def _split(line: str, widths: Iterable[int]) -> List[str]:
    """按逐行判定切分：含逗号走自由格式，否则按给定列宽。

    同一份 deck 里两种格式混用是常见的（不同工具导出的 include 各写各的），
    所以判定必须逐行做。
    """
    if "," in line:
        return _fields_free(line)
    out = []
    pos = 0
    for w in widths:
        out.append(line[pos : pos + w].strip())
        pos += w
    return out


def _to_int(s: str) -> Optional[int]:
    try:
        return int(float(s))  # 有的导出把 id 写成 1.0 这种形式
    except (TypeError, ValueError):
        return None


def _to_float(s: str) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _resolve_include(raw: str, search_dirs: Sequence[str]) -> Optional[str]:
    """解析 include 路径。

    相对路径基于**主控 deck 所在目录**（求解器的运行目录）——这是 LS-DYNA 的实际
    行为，已用真实模型验证。`search_dirs` 依次是：主控目录、`*INCLUDE_PATH` 声明的
    目录、以及包含它的文件所在目录（兜底：个别工具确实按相对本文件写）。

    返回第一个真实存在的候选；都不存在则返回首选，以便上层报为缺失。
    """
    p = raw.strip().strip('"')
    if not p:
        return None
    p = p.replace("\\", "/")
    if os.path.isabs(p):
        return os.path.normpath(p)
    first = None
    for d in search_dirs:
        cand = os.path.normpath(os.path.join(d, p))
        if first is None:
            first = cand
        if os.path.isfile(cand):
            return cand
    return first


def parse_deck(path: str, max_files: int = 2000, max_bytes: int = 4 << 30) -> Deck:
    """解析一份 deck 及其 include 树。

    max_files / max_bytes 是防护性上限：真实模型的 include 树可达数百个文件、
    近 GB 文本，出现环或异常引用时不能让解析无限膨胀。
    """
    deck = Deck()
    seen: Set[str] = set()
    total = 0
    root = os.path.abspath(path)
    # 主控 deck 所在目录是相对路径的基准（求解器的运行目录）
    root_dir = os.path.dirname(root)
    extra_dirs: List[str] = []   # *INCLUDE_PATH 声明的搜索目录
    queue: List[str] = [root]

    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        if len(seen) >= max_files:
            log.warning("include 文件数达到上限 %d，停止展开", max_files)
            break
        if not os.path.isfile(cur):
            deck.missing_includes.append(cur)
            continue
        try:
            total += os.path.getsize(cur)
        except OSError:
            pass
        if total > max_bytes:
            log.warning("include 总体积达到上限，停止展开")
            break
        seen.add(cur)
        deck.files.append(cur)
        queue.extend(_parse_one(cur, deck, root_dir, extra_dirs))

    return deck


def _parse_one(
    path: str, deck: Deck, root_dir: str, extra_dirs: List[str]
) -> List[str]:
    """解析单个文件，返回它引用的 include 绝对路径列表。

    root_dir 是主控目录（相对路径基准）；extra_dirs 收集 `*INCLUDE_PATH`，
    对后续文件同样生效，故就地修改传入的列表。
    """
    self_dir = os.path.dirname(path)
    includes: List[str] = []
    mode = ""          # 当前所处的卡
    pending_part = 0   # *PART 的标题行/数据行计数
    part_title = ""
    include_buf = ""   # *INCLUDE 的续行缓冲
    # 带后缀的 *INCLUDE_XXX 只有第一行是文件名，取到即停，避免把数据卡当路径
    include_single = False
    include_taken = False

    def search_dirs() -> List[str]:
        return [root_dir, *extra_dirs, self_dir]

    def flush_include() -> None:
        nonlocal include_buf
        if not include_buf:
            return
        if mode == "*INCLUDE_PATH":
            d = include_buf.strip().strip('"').replace("\\", "/")
            if d:
                extra_dirs.append(
                    d if os.path.isabs(d) else os.path.normpath(os.path.join(root_dir, d))
                )
        else:
            r = _resolve_include(include_buf, search_dirs())
            if r:
                includes.append(r)
        include_buf = ""

    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("打开 deck 文件失败 %s: %s", path, e)
        deck.missing_includes.append(path)
        return includes

    with fh:
        for raw in fh:
            line = raw.rstrip("\n").rstrip("\r")
            if not line or line.startswith("$"):
                continue

            if line.startswith("*"):
                flush_include()  # 关键字切换前收尾未完的续行
                kw = line.strip().upper()
                include_single = False
                include_taken = False
                if kw.startswith(_NODE) and not kw.startswith("*NODE_"):
                    mode = _NODE
                elif kw.startswith(_ELEMENT_SHELL):
                    mode = _ELEMENT_SHELL
                elif kw.startswith(_ELEMENT_SOLID):
                    mode = _ELEMENT_SOLID
                elif kw.startswith(_PART):
                    mode = _PART
                    pending_part = 0
                    part_title = ""
                elif kw.startswith(_INCLUDE):
                    if kw.startswith("*INCLUDE_PATH"):
                        mode = "*INCLUDE_PATH"
                    else:
                        mode = _INCLUDE
                        # 纯 *INCLUDE 可连写多个文件名；带后缀的变体
                        # （*INCLUDE_TRANSFORM 等）只有第一行是文件名，
                        # 其后是 idnoff / 变换参数等数据卡。
                        include_single = kw != _INCLUDE
                else:
                    mode = ""  # 其余卡一律跳过
                continue

            if mode == _NODE:
                f = _split(line, (_W_ID, _W_COORD, _W_COORD, _W_COORD))
                nid = _to_int(f[0] if f else "")
                if nid is not None and len(f) >= 4:
                    deck.nodes[nid] = (_to_float(f[1]), _to_float(f[2]), _to_float(f[3]))

            elif mode == _ELEMENT_SHELL:
                f = _split(line, (_W_ID,) * 6)
                if len(f) >= 6:
                    pid = _to_int(f[1])
                    ns = [_to_int(x) for x in f[2:6]]
                    if pid is not None and all(n is not None for n in ns[:3]):
                        n4 = ns[3] if ns[3] else ns[2]  # 三角壳：n4 缺省或等于 n3
                        deck.shells.setdefault(pid, []).append(
                            (ns[0], ns[1], ns[2], n4)  # type: ignore[arg-type]
                        )

            elif mode == _ELEMENT_SOLID:
                f = _split(line, (_W_ID,) * 10)
                if len(f) >= 10:
                    pid = _to_int(f[1])
                    ns = [_to_int(x) for x in f[2:10]]
                    if pid is not None and all(n is not None for n in ns):
                        deck.solids.setdefault(pid, []).append(tuple(ns))  # type: ignore[arg-type]

            elif mode == _PART:
                # *PART 的排布是：标题行，然后 pid/secid/mid... 数据行
                if pending_part == 0:
                    part_title = line.strip()
                    pending_part = 1
                else:
                    f = _split(line, (_W_ID,) * 3)
                    pid = _to_int(f[0] if f else "")
                    if pid is not None:
                        deck.part_names[pid] = part_title or f"PART {pid}"
                    pending_part = 0
                    part_title = ""

            elif mode in (_INCLUDE, "*INCLUDE_PATH"):
                if include_single and include_taken:
                    continue  # 变体的数据卡，不是文件名
                s = line.rstrip()
                if s.endswith("+"):
                    include_buf += s[:-1].strip()
                else:
                    include_buf += s
                    flush_include()
                    include_taken = True

        flush_include()  # 文件末尾仍有未收尾的续行

    return includes


# --- 只枚举 include 树（不解析网格）----------------------------------------


def _scan_includes(path: str, root_dir: str, extra_dirs: List[str]) -> Tuple[List[str], bool]:
    """只扫 *INCLUDE*，返回 (引用的绝对路径列表, 文件是否读到了)。

    与 `_parse_one` 共用同一套 include 规则（续行、带后缀的变体只取首行、
    *INCLUDE_PATH 并入搜索目录），但跳过 *NODE/*ELEMENT_*/*PART 的逐行解析——
    枚举文件清单用不上网格数据，而网格恰是耗时大头：实测整车 deck 全解析约
    40 秒，只扫 include 是毫秒级。
    """
    self_dir = os.path.dirname(path)
    includes: List[str] = []
    mode = ""
    include_buf = ""
    include_single = False
    include_taken = False

    def flush() -> None:
        nonlocal include_buf
        if not include_buf:
            return
        if mode == "*INCLUDE_PATH":
            d = include_buf.strip().strip('"').replace("\\", "/")
            if d:
                extra_dirs.append(
                    d if os.path.isabs(d) else os.path.normpath(os.path.join(root_dir, d))
                )
        else:
            r = _resolve_include(include_buf, [root_dir, *extra_dirs, self_dir])
            if r:
                includes.append(r)
        include_buf = ""

    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("打开 deck 文件失败 %s: %s", path, e)
        return includes, False

    with fh:
        for raw in fh:
            line = raw.rstrip("\n").rstrip("\r")
            if not line or line.startswith("$"):
                continue
            if line.startswith("*"):
                flush()
                kw = line.strip().upper()
                include_single = include_taken = False
                if kw.startswith("*INCLUDE_PATH"):
                    mode = "*INCLUDE_PATH"
                elif kw.startswith(_INCLUDE):
                    mode = _INCLUDE
                    include_single = kw != _INCLUDE
                else:
                    mode = ""
                continue
            if mode not in (_INCLUDE, "*INCLUDE_PATH"):
                continue
            if include_single and include_taken:
                continue
            s = line.rstrip()
            if s.endswith("+"):
                include_buf += s[:-1].strip()
            else:
                include_buf += s
                flush()
                include_taken = True
        flush()

    return includes, True


def list_deck_files(path: str, max_files: int = 2000) -> Tuple[List[str], List[str]]:
    """枚举一份 deck 的 include 树，返回 (存在的绝对路径, 缺失的路径)。

    用途是把整棵树交给外部能力（vektor3d 的 cae.deck.check）自取。清单**由服务端
    解析得出**，下发文件时只认清单里的成员——否则 `?path=` 就成了任意文件读取的
    口子：调用方可以拿它去读 /etc/shadow。
    """
    root = os.path.abspath(path)
    root_dir = os.path.dirname(root)
    extra_dirs: List[str] = []
    seen: Set[str] = set()
    found: List[str] = []
    missing: List[str] = []
    queue: List[str] = [root]

    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        if len(found) >= max_files:
            log.warning("deck include 文件数达上限 %d，停止枚举", max_files)
            break
        if not os.path.isfile(cur):
            missing.append(cur)
            continue
        seen.add(cur)
        found.append(cur)
        subs, ok = _scan_includes(cur, root_dir, extra_dirs)
        if ok:
            queue.extend(subs)

    return found, missing


# --- 三角化 -------------------------------------------------------------

# 六面体的 6 个面，按 *ELEMENT_SOLID 的节点顺序（n1..n8）
_HEX_FACES = (
    (0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1),
    (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0),
)


def solid_surface_faces(solids: List[Tuple[int, ...]]) -> List[Tuple[int, ...]]:
    """取实体单元的外表面：只出现一次的面即边界面。

    内部面被两个相邻单元共享（出现两次），渲染时不可见却占大量三角面——
    座椅泡沫这类实体件若不做这步，三角面数会翻好几倍且全被外壳挡住。
    """
    count: Dict[Tuple[int, ...], int] = {}
    first: Dict[Tuple[int, ...], Tuple[int, ...]] = {}
    for el in solids:
        # 退化的六面体（四面体用重复节点表示）会产生重复面，用集合去重
        for f in _HEX_FACES:
            face = tuple(el[i] for i in f)
            if len(set(face)) < 3:
                continue
            key = tuple(sorted(set(face)))
            count[key] = count.get(key, 0) + 1
            first.setdefault(key, face)
    return [first[k] for k, c in count.items() if c == 1]


def triangulate(quad: Tuple[int, ...]) -> List[Tuple[int, int, int]]:
    """四边形拆两个三角形；三角形（n4==n3 或只有 3 个不同点）直接返回。"""
    a, b, c = quad[0], quad[1], quad[2]
    d = quad[3] if len(quad) > 3 else c
    if d == c or d == a:
        return [(a, b, c)]
    return [(a, b, c), (a, c, d)]
