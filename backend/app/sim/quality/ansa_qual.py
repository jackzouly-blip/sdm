"""`.ansa_qual` 的解析与回写。

**往返保真是硬要求**:这个文件要交回 ANSA 驱动批处理,少一个字段、变一种数字
写法都可能让 ANSA 读出不同的卡。因此策略是"保留原始行,只重写被改过的那几行",
而不是解析成对象再全量重新生成——后者一定会在空格、有效位、未知字段上丢东西。

自测里有一条断言守着它:未修改时解析再写出必须与原文件逐字节相同。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .model import BANDS, Criterion, QualityCriteria, RangeBand

# aspect ratio [shells] =  ON,  NASTRAN :  0XFFFF00|  1. |  1., -1./  2., -1./ ...
_CRITERION_RE = re.compile(
    r"^(?P<lead>\s*)(?P<name>.+?)\s*\[(?P<domain>shells|solids)\]\s*=\s*"
    r"(?P<flag>ON|OFF)\s*,\s*(?P<calc>[^:]*?)\s*:\s*"
    r"(?P<color>0X[0-9A-Fa-f]+)\s*\|\s*(?P<weight>[^|]+?)\s*\|\s*(?P<bands>.*?)\s*$"
)
# 每档形如 "  1.,    -1./"
_BAND_RE = re.compile(r"(?P<value>BLANK|[-+0-9.Ee]+)\s*,\s*(?P<pct>[-+0-9.Ee]+)\s*/")
_KV_RE = re.compile(r"^(?P<lead>\s*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*?)\s*$")


def _to_float(raw: str) -> Optional[float]:
    """BLANK 表示"该档没有阈值",必须映射成 None。

    用 0 顶替是错的:0 在 min length / warping 这些判据上都是合法阈值,
    混淆之后"没设阈值"和"阈值为 0"就再也分不开了。
    """
    txt = raw.strip()
    if not txt or txt.upper() == "BLANK":
        return None
    try:
        return float(txt)
    except ValueError:
        return None


class QualFile:
    """一份 .ansa_qual 的可回写视图。"""

    def __init__(self, lines: List[str]) -> None:
        self._lines = lines
        self._criterion_line: Dict[str, int] = {}   # "name [domain]" -> 行号
        self.data = QualityCriteria()
        self._parse()

    # ── 解析 ────────────────────────────────────────────────────────────────
    def _parse(self) -> None:
        for i, line in enumerate(self._lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            m = _CRITERION_RE.match(line)
            if m:
                crit = self._parse_criterion(m)
                self.data.criteria.append(crit)
                self._criterion_line[crit.key] = i
                continue

            kv = _KV_RE.match(line)
            if not kv:
                continue
            key, value = kv.group("key"), kv.group("value")
            if key == "ANSA_Version":
                self.data.ansa_version = value.strip()
            elif key == "name":
                self.data.name = value.strip()
            elif key == "Criterion_ranges_enabled":
                self.data.ranges_enabled = value.strip().upper() == "ON"
            elif key.startswith("Ranges_"):
                self.data.ranges[key[len("Ranges_"):]] = _parse_ranges(value)
            elif key.startswith("failed_index_"):
                idx = _to_float(value.rstrip(","))
                self.data.failed_index[key[len("failed_index_"):]] = int(idx or 0)
            elif key.startswith("jacobian_"):
                num = _to_float(value)
                if num is not None:
                    self.data.jacobian_settings[key] = int(num)

    @staticmethod
    def _parse_criterion(m: re.Match) -> Criterion:
        bands = _BAND_RE.findall(m.group("bands"))
        thresholds: Dict[str, Optional[float]] = {}
        percentages: Dict[str, float] = {}
        for band, (value, pct) in zip(BANDS, bands):
            thresholds[band] = _to_float(value)
            percentages[band] = _to_float(pct) if _to_float(pct) is not None else -1.0
        return Criterion(
            name=m.group("name").strip(),
            domain=m.group("domain"),
            enabled=m.group("flag") == "ON",
            calculation=m.group("calc").strip(),
            color=m.group("color"),
            weight=_to_float(m.group("weight")) or 1.0,
            thresholds=thresholds,
            percentages=percentages,
        )

    # ── 回写 ────────────────────────────────────────────────────────────────
    def set_criterion(
        self,
        name: str,
        domain: str = "shells",
        *,
        enabled: Optional[bool] = None,
        thresholds: Optional[Dict[str, Optional[float]]] = None,
        weight: Optional[float] = None,
    ) -> Criterion:
        """改一条判据。只有被改到的行会被重新格式化,其余行原样保留。"""
        crit = self.data.get(name, domain)
        if crit is None:
            raise KeyError(f"质量卡里没有判据 {name} [{domain}]")
        if enabled is not None:
            crit.enabled = enabled
        if weight is not None:
            crit.weight = weight
        if thresholds:
            for band, value in thresholds.items():
                if band not in BANDS:
                    raise KeyError(f"未知档位 {band}(应为 {BANDS})")
                crit.thresholds[band] = value
        self._lines[self._criterion_line[crit.key]] = _format_criterion(crit)
        return crit

    def dumps(self) -> str:
        return "".join(self._lines)


def _parse_ranges(value: str) -> List[RangeBand]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    out: List[RangeBand] = []
    for i in range(0, len(parts) - 2, 3):
        pct = _to_float(parts[i])
        penalty = _to_float(parts[i + 1])
        active = (_to_float(parts[i + 2]) or 0) != 0
        out.append(RangeBand(percentage=pct or 0.0, penalty=penalty or 0.0, active=active))
    return out


def _fmt_num(value: Optional[float]) -> str:
    """按 ANSA 的写法输出:整数带尾点(5.),小数保留原样(0.667),空值 BLANK。"""
    if value is None:
        return "BLANK"
    if value == int(value) and abs(value) < 1e15:
        return f"{int(value)}."
    return repr(value)


def _format_criterion(c: Criterion) -> str:
    bands = "".join(
        f"{_fmt_num(c.thresholds.get(b)):>13}, {_fmt_num(c.percentages.get(b, -1.0)):>6}/ "
        for b in BANDS
    )
    flag = " ON" if c.enabled else "OFF"
    return (
        f" {c.key:<32}= {flag}, {c.calculation:>14} : {c.color:>12}| "
        f"{_fmt_num(c.weight):>7} | {bands}\n"
    )


def load(path: str) -> QualFile:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return QualFile(f.readlines())


def loads(text: str) -> QualFile:
    return QualFile(text.splitlines(keepends=True))
