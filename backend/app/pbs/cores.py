"""从 PBS 的 nodes 规格里估算核数。

`Resource_List.nodes` 形如 `1:ppn=64` 或 `1:ppn=64:first`：冒号分隔，首段是节点数，
其余段里 `ppn=N` 是每节点核数，其它段是节点属性(可忽略)。已完成作业的 accounting
记录额外带 `Resource_List.nodect`，与 spec 里的节点数应一致，优先取用（更贴近实际
分配）。
"""
from __future__ import annotations

import re
from typing import Optional, Union

_PPN_RE = re.compile(r"ppn=(\d+)")
_NODECOUNT_RE = re.compile(r"^(\d+)")


def parse_ppn(nodes_spec: Optional[str]) -> int:
    """从 nodes 规格解析每节点核数(ppn)，缺失则视为 1。"""
    m = _PPN_RE.search(nodes_spec or "")
    return int(m.group(1)) if m else 1


def cores_from_nodes(
    nodes_spec: Optional[str], nodect: Optional[Union[str, int]] = None
) -> int:
    """估算总核数 = 节点数 × ppn。

    nodect 优先(如 accounting 记录自带)；否则从 spec 开头的节点数解析；
    两者都拿不到则按 1 个节点算，缺失 ppn 按 1 核算，至少返回 1。
    """
    ppn = parse_ppn(nodes_spec)
    n: Optional[int] = None
    if nodect:
        try:
            n = int(nodect)
        except (TypeError, ValueError):
            n = None
    if n is None:
        m = _NODECOUNT_RE.match((nodes_spec or "").strip())
        n = int(m.group(1)) if m else 1
    return max(1, n) * max(1, ppn)
