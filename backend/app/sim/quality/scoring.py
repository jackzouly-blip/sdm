"""判据引擎:把一个实测指标值,按四档阈值判成等级与违规百分比。

ANSA 的质量评估**不是 pass/fail**,而是四档梯度 + 权重 + 惩罚:

    Ranges_shells = 0./0./1, 60./0./1, 95./1./1, 100./10./1
    warping [shells] = ON, IDEAS : ...| 权重 1. | Best 0 / Good 5 / Failed 15 / Worst 180

含义(**本模块的核心假设,见下方标定说明**):
  - 指标落在 Best..Good 之间 → 违规百分比 0..60
  - Good..Failed → 60..95
  - Failed..Worst → 95..100
  - `failed_index = 2` 指向 Failed 列,即越过该阈值即判废

⚠ **待与 ANSA 标定**:上面这套映射是从文件结构与 failed_index 推出来的最合理
读法,但没有对着 ANSA 的质量报告逐值验证过。标定方法:拿一份网格在 ANSA 里出
质量报告,与本引擎逐单元比对;不一致就改这里的映射,**不要在调用方打补丁**。
在标定完成前,等级判定(合格/不合格)是可信的,百分比得分只应作参考。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .model import BANDS, Criterion, QualityCriteria

GRADES = ("best", "good", "failed", "worst")


@dataclass
class Verdict:
    """一条判据对一个单元(或一批单元的极值)的判定结果。"""
    criterion: str
    calculation: str
    value: float
    grade: str              # best / good / failed / worst
    passed: bool            # 是否在判废线之内
    violation_pct: float    # 0..100,越大越差
    penalty: float          # 该档的惩罚权重
    weight: float

    @property
    def weighted_penalty(self) -> float:
        return self.penalty * self.weight


def _interp(value: float, lo: float, hi: float, out_lo: float, out_hi: float) -> float:
    if hi == lo:
        return out_lo
    t = (value - lo) / (hi - lo)
    t = max(0.0, min(1.0, t))
    return out_lo + t * (out_hi - out_lo)


def evaluate(
    criterion: Criterion,
    value: float,
    ranges: Optional[List] = None,
    failed_index: int = 2,
) -> Verdict:
    """按四档阈值判定一个指标值。

    方向(越大越好 / 越小越好)由阈值走向自动得出,不给每条判据硬编码——
    warping 是 0→180,jacobian 是 1→0,min length 是 5→0,方向本就编码在
    Best 与 Worst 的大小关系里。
    """
    scale = [b.percentage for b in ranges] if ranges else [0.0, 60.0, 95.0, 100.0]
    penalties = [b.penalty for b in ranges] if ranges else [0.0, 0.0, 1.0, 10.0]

    th = [criterion.thresholds.get(b) for b in BANDS]
    # Best 与 Failed 是必需的;Good/Worst 允许缺失或为占位值
    if th[0] is None or th[2] is None:
        return Verdict(criterion.name, criterion.calculation, value, "best", True,
                       0.0, 0.0, criterion.weight)

    better_high = criterion.higher_is_better
    # 统一成"数值越大越差"的坐标,后面只需一套逻辑
    sign = -1.0 if better_high else 1.0
    v = sign * value

    # 只保留构成递增序列的边界。这一步是必需的:5mm 卡里 min/max length、
    # min height 三条的 Good、Worst 都是占位 0.,拿它们当插值边界会把区间算烂。
    bounds = [(sign * th[0], scale[0], penalties[0])]
    if th[1] is not None and bounds[-1][0] < sign * th[1] < sign * th[2]:
        bounds.append((sign * th[1], scale[1], penalties[1]))
    bounds.append((sign * th[2], scale[2], penalties[2]))
    if th[3] is not None and sign * th[3] > bounds[-1][0]:
        bounds.append((sign * th[3], scale[3], penalties[3]))

    failed_bound = sign * th[2]
    if v <= bounds[0][0]:
        grade, pct, penalty = "best", scale[0], penalties[0]
    elif v > bounds[-1][0]:
        grade, penalty, pct = "worst", penalties[3], scale[3]
    else:
        # 落在哪一段就按该段线性插值;是否判废只看有没有越过 Failed 那条线
        grade, pct, penalty = "good", scale[1], penalties[1]
        for (lo, lo_pct, _), (hi, hi_pct, hi_penalty) in zip(bounds, bounds[1:]):
            if v <= hi:
                pct = _interp(v, lo, hi, lo_pct, hi_pct)
                penalty = hi_penalty
                grade = "failed" if v > failed_bound else "good"
                break

    passed = GRADES.index(grade) < failed_index
    return Verdict(
        criterion=criterion.name,
        calculation=criterion.calculation,
        value=value,
        grade=grade,
        passed=passed,
        violation_pct=round(pct, 3),
        penalty=penalty,
        weight=criterion.weight,
    )


def evaluate_all(
    criteria: QualityCriteria,
    values: Dict[str, float],
    domain: str = "shells",
) -> List[Verdict]:
    """按卡里**启用**的判据逐条评。

    只评 enabled 的:那张 5mm 卡 40 条 shells 判据里只开了 11 条,solids 全关。
    把关掉的也算上,等于用工程师没同意的标准去判他的网格。
    """
    out: List[Verdict] = []
    for crit in criteria.enabled_criteria(domain):
        if crit.name not in values:
            continue
        out.append(evaluate(
            crit,
            values[crit.name],
            ranges=criteria.ranges.get(domain),
            failed_index=criteria.failed_index.get(domain, 2),
        ))
    return out


# 钢的弹性波速(mm/s):E=210GPa, ρ=7.85e-9 t/mm³ → c=√(E/ρ)≈5.17e6 mm/s
STEEL_WAVE_SPEED_MM_S = 5.17e6


def estimate_time_step(min_length_mm: float, wave_speed_mm_s: float = STEEL_WAVE_SPEED_MM_S) -> float:
    """显式时间步估算 Δt ≈ L_min / c。

    这是碰撞里最该被看见的数:一个零件上残留几个 0.5mm 碎单元,整个模型的机时
    就翻几倍。质量卡里 `crash time step [shells]` 那条判据算的就是它
    (那张 5mm 卡把它关掉了,但阈值 1.0E-6 还留着)。
    """
    if min_length_mm <= 0 or wave_speed_mm_s <= 0:
        return 0.0
    return min_length_mm / wave_speed_mm_s


def added_mass_ratio(current_dt: float, target_dt: float) -> float:
    """质量缩放到目标时间步所需的加质量比例(粗估)。

    质量缩放按 Δt ∝ √m 抬时间步,故 m_new/m_old = (target/current)²。
    超过 5% 通常就认为结果被污染了——这条是碰撞工程师每天要看的数。
    """
    if current_dt <= 0 or target_dt <= 0 or target_dt <= current_dt:
        return 0.0
    return (target_dt / current_dt) ** 2 - 1.0
