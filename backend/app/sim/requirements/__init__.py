"""需求文档解析：文档 → 需求条目。

条目是需求进入平台后的第一等公民：它带原文出处、量纲化的指标、是验收门槛还是
参考项、以及"这条还没定死"的标记。下游（工况、质量卡、交付跟踪）都从条目取数，
而不是各自再解析一遍文档。
"""
from .extract import (  # noqa: F401
    BASELINE_REFERENCE,
    BASELINE_REQUIRED,
    CATEGORY_DELIVERY,
    CATEGORY_LOADING,
    CATEGORY_MESH,
    CATEGORY_SUBJECT,
    RequirementItem,
    extract_from_file,
    extract_from_slides,
    items_to_json,
)
from .metrics import Metric, count_load_points, extract_diameter, extract_metrics  # noqa: F401
from .pptx_reader import Slide, read_slides  # noqa: F401
