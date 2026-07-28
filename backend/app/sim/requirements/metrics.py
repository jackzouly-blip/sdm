"""从需求原文里抽出带量纲的指标。

真实文档（奇瑞 T29 IP 系统 CAE 分析要求）里的目标值长这样：

    IP总成一级模态≥40Hz（对外目标≥38Hz）
    减速度连续3ms内不能超过80g（内部标准72g）
    直径50mm;90N，水平线以上变形量≤1.0mm 水平线以下变形量≤2.0mm
    F≤3000N，侵入量35mm（T29为50mm）
    塑料件减重3%以上

几条从真实文档得来的约束：

- **一条目标值里往往有多个指标**，且带括号补充（"对外目标"/"内部标准"/"T29为"）。
  括号里的常是**项目级例外或更严的内控值**，最容易被漏掉,必须单独成条并标出来。
- **比较关系有中文表达**："不能超过"/"以上"/"不小于",不能只认 ≥ ≤。
- **原文有笔误**（"20m'm"、"10mmm"）。抽不出数不能丢掉整条——原文照样留档,
  交给人确认,总好过静默丢一条要求。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

# 量纲。℃ 与 % 单独列,别被 C/m 之类误匹配
UNITS = ["Hz", "kN", "N", "MPa", "mm", "kg", "g", "%", "℃", "ms", "s"]
_UNIT_RE = "|".join(re.escape(u) for u in UNITS)

# 比较符：符号与中文说法等价
_OP_MAP = {
    "≥": "≥", ">=": "≥", "≧": "≥", "不小于": "≥", "不低于": "≥", "以上": "≥",
    "≤": "≤", "<=": "≤", "≦": "≤", "不大于": "≤", "不超过": "≤", "不能超过": "≤",
    "以下": "≤", "小于": "<", "<": "<", "大于": ">", ">": ">", "=": "=",
}
_OP_BEFORE = "≥|>=|≧|≤|<=|≦|<|>|=|不小于|不低于|不大于|不超过|不能超过|小于|大于"
_OP_AFTER = "以上|以下"

# 值前的比较符：  ≥40Hz / 不能超过80g
_RE_OP_VALUE = re.compile(
    rf"(?P<op>{_OP_BEFORE})\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>{_UNIT_RE})"
)
# 值后的比较符：  减重3%以上
_RE_VALUE_OP = re.compile(
    rf"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>{_UNIT_RE})\s*(?P<op>{_OP_AFTER})"
)
# 无比较符的裸量：  90N / 直径50mm / 侵入量35mm
_RE_BARE = re.compile(rf"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>{_UNIT_RE})")
# 直径写法：φ50 / 直径50mm / 压头大小直径20mm
_RE_DIAMETER = re.compile(r"(?:直径|φ|Φ|Ø)\s*(?P<value>\d+(?:\.\d+)?)\s*(?:mm)?")

# 括号补充里的限定词 → 该指标的性质
_QUALIFIERS = [
    ("内部标准", "internal"), ("内控", "internal"),
    ("对外目标", "external"), ("对外", "external"),
]
_BRACKET_RE = re.compile(r"[（(]([^（）()]{1,40})[)）]")


@dataclass
class Metric:
    """一个可判定的指标。"""
    quantity: str        # 被限定的量,如"一级模态""变形量";抽不出时为空
    op: str              # ≥ ≤ < > = 或空(裸量)
    value: float
    unit: str
    raw: str             # 原文片段,永远保留——人要能核对
    kind: str = "target"     # target=判定值 / internal=内控 / external=对外 / override=项目例外
    scope: str = ""          # 该指标的适用范围,如"T29""水平线以上"


def _norm_op(raw: str) -> str:
    return _OP_MAP.get(raw.strip(), raw.strip())


def _quantity_before(text: str, start: int) -> str:
    """取指标前面那段中文当"量名"。

    只往前看一小段并在标点/数字处截断——"水平线以上变形量≤1.0mm"要得到"变形量",
    而不是把整句话都当量名。
    """
    head = text[max(0, start - 14):start]
    head = re.split(r"[，,；;。:：、\s\d]", head)[-1].strip()
    # 去掉粘在数值前的助词:"（T29为50mm）"的量名应是空而不是"为"
    return re.sub(r"[为是达至到内的约]+$", "", head).strip()


def extract_metrics(text: str) -> List[Metric]:
    """从一段目标值原文里抽出全部指标。

    括号内容单独处理:那里放的往往是内控值或项目例外(如"T29为50mm"),
    与主值并列成条,不能合并——两者判定口径不同。
    """
    if not text or not text.strip():
        return []

    out: List[Metric] = []
    seen: set = set()

    def push(m: Metric) -> None:
        key = (m.quantity, m.op, m.value, m.unit, m.kind, m.scope)
        if key in seen:
            return
        seen.add(key)
        out.append(m)

    def scan(segment: str, kind: str, scope: str) -> None:
        # 已被"带比较符"的匹配占用的字符区间。裸量去重只能按**本段内的位置**算,
        # 不能按字符串跨段比——真实文档第 7 页里"上50mm区域"先产出一个 50mm,
        # 若按字符串去重,括号中"（T29为50mm）"这个项目例外就会被当成重复丢掉,
        # 于是整份文档最关键的一条项目级覆盖静默消失。
        spans: List[tuple] = []
        for rx in (_RE_OP_VALUE, _RE_VALUE_OP):
            for mt in rx.finditer(segment):
                spans.append((mt.start(), mt.end()))
                push(Metric(
                    quantity=_quantity_before(segment, mt.start()),
                    op=_norm_op(mt.group("op")),
                    value=float(mt.group("value")),
                    unit=mt.group("unit"),
                    raw=mt.group(0),
                    kind=kind,
                    scope=scope,
                ))
        # 裸量兜底:90N、侵入量35mm 这类没有比较符但同样是判定输入的数
        for mt in _RE_BARE.finditer(segment):
            if any(mt.start() < e and s_ < mt.end() for s_, e in spans):
                continue      # 与带比较符的匹配重叠,已计过
            push(Metric(
                quantity=_quantity_before(segment, mt.start()),
                op="",
                value=float(mt.group("value")),
                unit=mt.group("unit"),
                raw=mt.group(0),
                kind=kind,
                scope=scope,
            ))

    # 先摘出括号补充,主文本去掉它们再扫,避免"（T29为50mm）"污染主值的量名
    brackets = _BRACKET_RE.findall(text)
    main = _BRACKET_RE.sub(" ", text)
    scan(main, "target", "")

    for note in brackets:
        kind = "override"
        scope = ""
        for word, k in _QUALIFIERS:
            if word in note:
                kind = k
                break
        else:
            # 形如"T29为50mm":冒号前那截是适用范围
            mt = re.match(r"\s*([A-Za-z0-9一-龥]{1,10})\s*[为:：]", note)
            if mt:
                scope = mt.group(1)
        scan(note, kind, scope)

    return out


def extract_diameter(text: str) -> Optional[float]:
    """压头直径。它是加载条件而非判定值,单独抽出来供工况参数使用。"""
    mt = _RE_DIAMETER.search(text or "")
    return float(mt.group("value")) if mt else None


def count_load_points(lines: List[str]) -> int:
    """数加载点位标签(P1、P2…)。

    ⚠ 只能数出**有几个点**。真实文档里 P1~P28 的位置全在图上,文字层只有编号,
    坐标抽不出来——必须由人在 CAE 里定。宁可只给个数并说清,也不要假装知道位置。
    """
    labels = set()
    for line in lines:
        for mt in re.finditer(r"^\s*P(\d{1,3})\s*$", line.strip()):
            labels.add(int(mt.group(1)))
    return len(labels)


def metrics_to_json(metrics: List[Metric]) -> List[Dict]:
    return [asdict(m) for m in metrics]
