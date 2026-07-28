"""网格质量卡的结构化模型。

一张"质量卡实例"由两个来源合成，它们语义不同、必须分开存：

  .ansa_qual  判定侧——划完的网格算不算好（四档阈值 + 权重 + 惩罚）
  .ansa_mpar  生成侧——该怎么划（目标尺寸、特征识别、freeze 策略…）

同一份实例要能双向流动：导出回 ANSA 驱动批处理网格，也直接喂给 mesh.check
做判定。**绝不能 ANSA 一份、平台一份**——两份必然漂移，漂移之后"平台说合格、
ANSA 说不合格"没人能查。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ANSA 的四个档位列，顺序即 Best → Good → Failed → Worst
BANDS = ("best", "good", "failed", "worst")
# 判废分界落在第几档（failed_index_shells = 2，0-based 指向 Failed 列）
DEFAULT_FAILED_INDEX = 2


@dataclass
class RangeBand:
    """Ranges_* 里的一组 (百分比, 惩罚, 是否启用)。

    实测 Ranges_shells = 0./0./1, 60./0./1, 95./1./1, 100./10./1 —— 四组分别对应
    Best/Good/Failed/Worst 四档的"违规百分比"刻度与惩罚权重。
    """
    percentage: float
    penalty: float
    active: bool = True


@dataclass
class Criterion:
    """一条质量判据。

    ⚠ `calculation` 不是装饰性字段:同一个"长宽比",按 NASTRAN 算和按 IDEAS 算
    数值不同。质量卡规定的不只是阈值,还有用谁的公式——算错公式比不算更糟,
    因为读数看着像那么回事却和 ANSA 对不上。
    """
    name: str                       # 如 "aspect ratio"
    domain: str                     # shells / solids
    enabled: bool
    calculation: str                # NASTRAN / PATRAN / IDEAS / ANSA / LS-DYNA / ABAQUS...
    color: str                      # ANSA 里的显示色,回写时要原样带回
    weight: float
    # 四档阈值。BLANK（无阈值）用 None 表示,不能用 0 顶替——0 是合法阈值
    thresholds: Dict[str, Optional[float]] = field(default_factory=dict)
    # 每档的百分比覆盖值,-1. 表示"用 Ranges 的公共刻度"
    percentages: Dict[str, float] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.name} [{self.domain}]"

    @property
    def higher_is_better(self) -> bool:
        """由 Best 与 **Failed** 的关系判定方向,不给每条判据硬编码。

        ⚠ 不能用 Best 与 Worst 判:实测这张 5mm 卡里 min length / max length /
        min height 三条的 Good、Worst 列都是占位的 0.（ANSA 这几条只用 Best 与
        Failed 两个值）,四档并不单调。若按 Best>Worst 判方向,`max length`
        (Best=5, Failed=10, Worst=0) 会被判成"越大越好"——12mm 的超长单元
        反而算优秀,而这恰恰是它要拦的东西。

        改用 Best 与 Failed 后,11 条启用判据全部判对:
          min length  Best 5 → Failed 3   越大越好
          max length  Best 5 → Failed 10  越小越好
          warping     Best 0 → Failed 15  越小越好
          jacobian    Best 1 → Failed 0.6 越大越好
        """
        best = self.thresholds.get("best")
        failed = self.thresholds.get("failed")
        if best is None or failed is None:
            return False
        return best > failed


@dataclass
class QualityCriteria:
    """.ansa_qual 的结构化形态。"""
    ansa_version: str = ""
    name: str = ""
    ranges_enabled: bool = False
    criteria: List[Criterion] = field(default_factory=list)
    ranges: Dict[str, List[RangeBand]] = field(default_factory=dict)   # domain -> 四档
    failed_index: Dict[str, int] = field(default_factory=dict)          # domain -> 判废档位
    jacobian_settings: Dict[str, int] = field(default_factory=dict)

    def get(self, name: str, domain: str = "shells") -> Optional[Criterion]:
        for c in self.criteria:
            if c.name == name and c.domain == domain:
                return c
        return None

    def enabled_criteria(self, domain: str = "shells") -> List[Criterion]:
        return [c for c in self.criteria if c.domain == domain and c.enabled]


@dataclass
class MeshParams:
    """.ansa_mpar 的结构化形态。

    值一律保留**原始字符串**:里面既有数字（5.）、布尔（false）、枚举
    （Recognize features）,也有表达式（0.667*Lmin）。提前解析成 float 会在
    回写时把 `5.` 变成 `5.0`、把表达式毁掉——而这个文件是要交回 ANSA 的。
    """
    ansa_version: str = ""
    name: str = ""
    values: Dict[str, str] = field(default_factory=dict)
    # 键 → 所属段（"Perimeters" / "Fillets" / "Holes 2D"…）。这些段正是网格生成
    # 侧的可调旋钮分类,后续 AI 调参就在这个空间里动。
    sections: Dict[str, str] = field(default_factory=dict)

    def get_float(self, key: str, default: float = 0.0) -> float:
        raw = (self.values.get(key) or "").strip().rstrip(".")
        try:
            return float(raw)
        except ValueError:
            return default

    def keys_in_section(self, section: str) -> List[str]:
        return [k for k, s in self.sections.items() if s == section]


@dataclass
class QualityCard:
    """质量卡实例 = 判定侧 + 生成侧 + 出处元数据。

    元数据不是可选项:模板库要能回答"这张卡是哪个客户、哪份规范、哪一版、
    哪个 ANSA 版本"。ANSA 大版本之间字段会增删,不记版本的卡迟早回写失败。
    """
    id: str
    name: str
    criteria: QualityCriteria
    mesh_params: MeshParams
    source: str = ""            # 客户 / 规范号
    revision: str = ""
    scope: str = ""             # 适用范围:碰撞 / 钣金 / 5mm…
    description: str = ""
    builtin: bool = False       # 内置模板不可删

    @property
    def target_element_length(self) -> float:
        return self.mesh_params.get_float("target_element_length", 0.0)
