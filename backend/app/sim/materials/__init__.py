"""材料库：LS-DYNA 关键字材料导入。

设计见 docs/sdm-material-library.md。表结构与 DAO 在 sim/db.py，
路由在 sim/router.py（写操作仅管理员）。
"""
from .keyword_import import import_material_text, parse_material_file

__all__ = ["import_material_text", "parse_material_file"]
