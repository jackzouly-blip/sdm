# -*- coding: utf-8 -*-
"""把 5P-BAG 工程师的材料卡/控制卡种进模板库并发布 v1。幂等，可重复执行。

发布内容用**文件原样**而不是库内渲染：MAT.K 里有 *PARAMETER R TTF（起爆时刻，
曲线 15 的时间轴用 1.0&TTF 平移），材料导入器不认识 *PARAMETER，渲染会把它丢掉
——丢了之后曲线引用未定义参数，求解器直接报错。原样快照是当前唯一保真的版本；
在线编辑后再发布走渲染路径时，这一点要先解决（见 templates.py 模块文档）。
"""
import sys

sys.path.insert(0, "/opt/hpc-portal/backend")

from app.sim.db import SimDB  # noqa: E402
from app.sim.materials.keyword_import import import_material_text  # noqa: E402

TDIR = "/opt/hpc-portal/backend/app/sim/templates/airbag"
UNITS = "mm-kg-ms"   # 密度 1.1E-6 kg/mm3 / 应力 GPa / 时间 ms，随工程师模型


def main():
    db = SimDB("/opt/hpc-portal/backend/state/sim.db")
    mat = open(f"{TDIR}/MAT.K", encoding="latin-1").read()
    ctrl = open(f"{TDIR}/03_Control_card.k", encoding="latin-1").read()

    # 1) 材料卡进材料库（浏览/复用；幂等，按内容指纹跳过）
    r = import_material_text(db, mat, unit_system=UNITS,
                             source="5P-BAG MAT.K", actor="seed")
    print("材料导入:", {k: r.get(k) for k in ("created", "updated", "unchanged")},
          "warnings:", len(r.get("warnings", [])))

    # 2) 控制卡模板（整份存档）
    name_c = "5P-BAG 气囊展开控制卡"
    t = db.get_control_template_by_name(name_c)
    if t is None:
        tid_c = db.create_control_template(
            name_c, UNITS, ctrl, description="工程师 03_Control_card.k 原样",
            analysis_type="气囊展开", source_name="03_Control_card.k",
            created_by="seed")
        print("控制卡模板已创建:", tid_c)
    else:
        tid_c = t["id"]
        print("控制卡模板已存在:", tid_c)
    if not db.list_template_releases("control", tid_c):
        rid = db.create_template_release(
            "control", tid_c, name_c, UNITS, ctrl,
            "工程师 03_Control_card.k 原样快照", "seed")
        print("控制卡 v1 已发布:", rid)

    # 3) 材料模板发布 v1 —— 内容取 MAT.K 原样（保住 *PARAMETER TTF）
    name_m = "5P-BAG 织物材料(MAT.K)"
    m = db.get_material_template_by_name(name_m)
    if m is None:
        tid_m = db.create_material_template(
            name_m, UNITS, description="工程师 MAT.K 原样(含 TTF 参数与曲线 15~20)",
            created_by="seed")
        print("材料模板已创建:", tid_m)
    else:
        tid_m = m["id"]
        print("材料模板已存在:", tid_m)
    if not db.list_template_releases("material", tid_m):
        rid = db.create_template_release(
            "material", tid_m, name_m, UNITS, mat,
            "工程师 MAT.K 原样快照(渲染路径会丢 *PARAMETER, 故存原样)", "seed")
        print("材料 v1 已发布:", rid)

    for kind, tid in (("control", tid_c), ("material", tid_m)):
        for rel in db.list_template_releases(kind, tid):
            print(f"  {kind} v{rel['version_no']} release={rel['id']}"
                  f" bytes={rel['content_bytes']}")


if __name__ == "__main__":
    main()
