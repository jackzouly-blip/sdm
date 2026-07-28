"""读 .pptx 的文字层（正文、表格、备注）。

pptx 就是个 zip + XML，用标准库就够，不引第三方依赖：需求文档是要在集群上解析的，
每多一个依赖就多一处离线安装的麻烦。

**它只能读到文字层。** 真实需求文档里加载点位 P1~P28 的位置全画在图上，
文字层只有编号——这个边界写在这里，免得下游误以为拿到了坐标。
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List
from xml.etree import ElementTree as ET

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"

_SLIDE_RE = re.compile(r"ppt/slides/slide(\d+)\.xml$")
_NOTES_RE = re.compile(r"ppt/notesSlides/notesSlide(\d+)\.xml$")


@dataclass
class Slide:
    number: int
    lines: List[str] = field(default_factory=list)          # 形状里的文字，按段落
    tables: List[List[List[str]]] = field(default_factory=list)   # 每张表：行 → 单元格
    notes: List[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        """标题 = 第一行**非纯数字**的文字。

        PPT 的版式信息不可靠，首行是更稳的近似；但页码常做成独立文本框且排在
        正文之前（真实文档第 3 页首行就是 "3"，真正的标题是第二行"网格要求"），
        所以要跳过纯数字行。
        """
        for line in self.lines:
            if not line.strip().isdigit():
                return line
        return ""


def _para_text(node) -> str:
    return "".join(t.text or "" for t in node.iter(A + "t")).strip()


def _shape_lines(root) -> List[str]:
    out: List[str] = []
    for sp in root.iter(P + "sp"):
        for para in sp.iter(A + "p"):
            txt = _para_text(para)
            if txt:
                out.append(txt)
    return out


def _tables(root) -> List[List[List[str]]]:
    tables: List[List[List[str]]] = []
    for gf in root.iter(P + "graphicFrame"):
        rows: List[List[str]] = []
        for tr in gf.iter(A + "tr"):
            cells = [
                " ".join(_para_text(p) for p in tc.iter(A + "p")).strip()
                for tc in tr.iter(A + "tc")
            ]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def read_slides(path: str) -> List[Slide]:
    """按页号顺序返回全部幻灯片。"""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        slide_names = sorted(
            (n for n in names if _SLIDE_RE.match(n)),
            key=lambda n: int(_SLIDE_RE.match(n).group(1)),
        )
        notes_by_no: Dict[int, str] = {
            int(_NOTES_RE.match(n).group(1)): n for n in names if _NOTES_RE.match(n)
        }

        out: List[Slide] = []
        for name in slide_names:
            no = int(_SLIDE_RE.match(name).group(1))
            root = ET.fromstring(zf.read(name))
            slide = Slide(number=no, lines=_shape_lines(root), tables=_tables(root))
            if no in notes_by_no:
                notes_root = ET.fromstring(zf.read(notes_by_no[no]))
                slide.notes = [
                    t for t in (_para_text(p) for p in notes_root.iter(A + "p"))
                    if t and not t.isdigit() and t != "页码："
                ]
            out.append(slide)
        return out
