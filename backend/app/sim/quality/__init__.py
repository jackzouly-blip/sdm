"""网格质量卡:解析、判据引擎、模板库。

判定侧(.ansa_qual)与生成侧(.ansa_mpar)是同一份真相的两个面,由 QualityCard
合成。它既能导出回 ANSA 驱动批处理网格,也直接喂给 mesh.check 做判定——
两个消费端共用一份数据,不各存一份。
"""
from .model import Criterion, MeshParams, QualityCard, QualityCriteria, RangeBand  # noqa: F401
from .library import DEFAULT_TEMPLATE_ID, Override, QualityCardLibrary, TemplateMeta  # noqa: F401
from .scoring import (  # noqa: F401
    Verdict,
    added_mass_ratio,
    estimate_time_step,
    evaluate,
    evaluate_all,
)
