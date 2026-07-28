"""需求文档 → 需求条目。

**先做确定性抽取，不上 AI。** 真实文档（奇瑞 T29 IP 系统）里 11 个分析项躺在一张
规整表格里，列是「分析项 / 目标值 / 分析基准 / 年度要求」——规则就能抽准，而且
抽错了能一眼看出是哪条规则的问题。AI 该负责的是后面几页那些自由文本的加载条件，
以及规则抽不动的文档；把规则能干的事交给模型，只会换来不可复现的错误。

抽出来的每条都带 `source_ref`（第几页/表格第几行），这样人能立刻回原文核对——
需求条目的价值全在可追溯，不可追溯的条目还不如不抽。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .metrics import Metric, count_load_points, extract_diameter, extract_metrics
from .pptx_reader import Slide, read_slides

# 条目分类。真实文档的主出口是工况,不是网格——网格质量要求通常是另一份文件
CATEGORY_SUBJECT = "subject"       # 分析项/工况
CATEGORY_LOADING = "loading"       # 加载与约束条件
CATEGORY_DELIVERY = "delivery"     # 交付物、流程、轮次
CATEGORY_MESH = "mesh"             # 网格相关(这类文档里往往极少)
CATEGORY_OTHER = "other"

# "分析基准"列的取值:决定这项是验收门槛还是仅供参考
BASELINE_REQUIRED = "required"     # 合格
BASELINE_REFERENCE = "reference"   # 参考
BASELINE_UNKNOWN = ""

# 出现这些说法 = 要求尚未定死,必须标出来让人去澄清,不能硬结构化成一个数
_CLARIFY_MARKERS = [
    "重新讨论", "共同确认", "先行制定", "内部讨论", "待定", "另行", "沟通确认",
    "供应商可根据", "不作为必须",
]
_MESH_MARKERS = ["网格", "材料卡片", "单元", "mesh"]
_DELIVERY_MARKERS = ["报告", "验收", "提供", "交付", "轮"]


@dataclass
class RequirementItem:
    """一条需求条目。

    `raw_text` 永远保留原文:抽取规则会有漏网,人核对时看的是原文而不是我们的解读。
    """
    seq: str                                  # 原文序号,如 "1"
    category: str
    title: str
    raw_text: str
    metrics: List[Dict] = field(default_factory=list)
    baseline: str = BASELINE_UNKNOWN
    project_note: str = ""                    # 年度/项目专属要求列
    source_ref: str = ""                      # "第1页 表1 第3行"
    needs_clarification: bool = False
    clarification_hint: str = ""
    extracted_by: str = "rule"                # rule / ai / manual
    load_points: int = 0                      # 加载点位数量(坐标不可得,见 metrics.count_load_points)
    indenter_diameter_mm: Optional[float] = None


def _baseline_of(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return BASELINE_UNKNOWN
    if "合格" in t:
        return BASELINE_REQUIRED
    if "参考" in t:
        return BASELINE_REFERENCE
    return BASELINE_UNKNOWN


def _clarification(*texts: str) -> str:
    joined = " ".join(t for t in texts if t)
    for marker in _CLARIFY_MARKERS:
        if marker in joined:
            return marker
    return ""


def _categorize(title: str, body: str) -> str:
    joined = f"{title} {body}"
    if any(m in joined for m in _MESH_MARKERS) and "分析" not in title:
        return CATEGORY_MESH
    if any(m in joined for m in _DELIVERY_MARKERS) and not title.strip():
        return CATEGORY_DELIVERY
    return CATEGORY_SUBJECT


def _looks_like_item_table(rows: List[List[str]]) -> bool:
    """分析项总表的判据:表头里同时有"分析项"和"目标值"。

    按表头认而不是按列数认——列数会因合并单元格变化,表头不会。
    """
    head = " ".join(rows[0]) if rows else ""
    return "分析项" in head and "目标值" in head


def _column_index(header: List[str], *keywords: str) -> int:
    for i, cell in enumerate(header):
        if any(k in cell for k in keywords):
            return i
    return -1


def extract_from_slides(slides: List[Slide]) -> List[RequirementItem]:
    items: List[RequirementItem] = []

    for slide in slides:
        # ① 分析项总表 → 每行一条
        for t_idx, rows in enumerate(slide.tables):
            if not _looks_like_item_table(rows):
                continue
            header = rows[0]
            i_title = _column_index(header, "分析项")
            i_target = _column_index(header, "目标值")
            i_base = _column_index(header, "分析基准", "基准")
            i_note = _column_index(header, "项目", "年")
            for r_idx, row in enumerate(rows[1:], start=1):
                def cell(i: int) -> str:
                    return row[i].strip() if 0 <= i < len(row) else ""

                title = cell(i_title)
                if not title:
                    continue
                target = cell(i_target)
                note = cell(i_note)
                hint = _clarification(target, note, cell(i_base))
                items.append(RequirementItem(
                    seq=(row[0].strip() if row and row[0].strip().isdigit() else str(len(items) + 1)),
                    category=_categorize(title, target),
                    title=title,
                    raw_text=target,
                    metrics=[asdict(m) for m in extract_metrics(target)],
                    baseline=_baseline_of(cell(i_base)),
                    project_note=note,
                    source_ref=f"第{slide.number}页 表{t_idx + 1} 第{r_idx}行",
                    needs_clarification=bool(hint),
                    clarification_hint=hint,
                    indenter_diameter_mm=extract_diameter(target),
                ))

        # ② 工况页 → 加载与约束条件。标题页/纯图页不产条目。
        # 正文要排除标题行本身与纯数字页码——标题未必是第一行(见 Slide.title),
        # 按下标切会把"网格要求"这种分节页的标题当成正文,凭空多出一条空要求。
        title = slide.title
        body = [l for l in slide.lines
                if l.strip() and l != title and not l.strip().isdigit()]
        if not title or not body:
            continue
        if any(_looks_like_item_table(rows) for rows in slide.tables):
            continue          # 总表页已经处理过,不重复产条目

        # 只留含数值或约束说明的行:P1、P2 这类纯标签不是要求本身
        meaningful = [
            l for l in body
            if not re.fullmatch(r"P\d{1,3}", l.strip())
            and (re.search(r"\d", l) or "约束" in l or "要求" in l)
        ]
        points = count_load_points(body)
        # 没有数值行但有点位标签的页照样成条:真实文档第 4 页(静态头碰)正文只有
        # P1~P6 和"加载点位信息",丢掉它就等于丢掉"这个工况有 6 个加载点"。
        if not meaningful and not points:
            continue
        text = " ".join(meaningful) if meaningful else " ".join(body)
        hint = _clarification(text)
        items.append(RequirementItem(
            seq=str(len(items) + 1),
            category=CATEGORY_LOADING,
            title=title,
            raw_text=text,
            metrics=[asdict(m) for m in extract_metrics(text)],
            source_ref=f"第{slide.number}页",
            needs_clarification=bool(hint),
            clarification_hint=hint,
            load_points=points,
            indenter_diameter_mm=extract_diameter(text),
        ))

    return items


def extract_from_file(path: str) -> List[RequirementItem]:
    """目前只支持 .pptx。

    其它格式(docx/pdf)先如实拒绝,而不是返回空列表——空列表会被当成"这份文档
    没有要求",比报错危险得多。
    """
    lower = path.lower()
    if lower.endswith(".pptx"):
        return extract_from_slides(read_slides(path))
    raise ValueError(
        f"暂不支持解析该格式: {path.rsplit('.', 1)[-1]}。当前支持 .pptx；"
        "其它格式请先转存，或等 AI 抽取接入后再试"
    )


def items_to_json(items: List[RequirementItem]) -> List[Dict]:
    return [asdict(i) for i in items]
