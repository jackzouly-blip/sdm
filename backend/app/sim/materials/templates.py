"""材料模板文件与控制卡模板的组装、校验与导出。

**材料模板是组装式的**：模板只记录"选了哪几张材料卡"，导出时按各卡的
`keyword_text`（自包含块：MAT + 其引用的 DEFINE_CURVE/TABLE 原文）拼出一份
可 `*INCLUDE` 的 MAT.K。不另存一份整文件——库内材料是正本，再存副本必然分叉。

**组装前必须过三道校验**，都是"语法合法但求解器静默算错"的来源：

1. **单位制一致** —— LS-DYNA 无量纲。t-mm-s 的 7.85E-9 和 mm-kg-ms 的 7.85E-6
   都是钢，混进同一份 deck 求解器不会报错，结果差 1000 倍。
2. **MID 不撞车** —— 两张卡同号，后加载的静默覆盖前者，*PART 引到的是谁看运气。
3. **LCID 不撞车** —— 各卡的 keyword_text 里带着自己的曲线原文；来自不同源文件
   的卡，曲线号极易重号。撞了之后应力-应变曲线张冠李戴，照样算得出结果。

第 2、3 条不必重新解析原文：导入时已把 `source_mid` / `source_lcid` 结构化留下了。
"""
from __future__ import annotations

import re
import time
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ...logger import get_logger

log = get_logger(__name__)


# ── 卡内曲线号 ──────────────────────────────────────────────

_KW_LINE = re.compile(r"^\*([A-Z0-9_]+)", re.I)


def lcids_in_card(keyword_text: str) -> Set[int]:
    """扫出卡的自包含块里自己定义的曲线/表号。

    直接扫原文而不是查 sim_material_curve：曲线挂在 material 上，同一材料的主卡
    与 NULL 变体卡共用 material_id，按材料查会把不属于本卡的曲线也算进来，
    组装两张同材料的卡时就会误报撞号。原文是最终要落进 deck 的东西，以它为准。
    """
    out: Set[int] = set()
    lines = (keyword_text or "").splitlines()
    i = 0
    while i < len(lines):
        m = _KW_LINE.match(lines[i].strip())
        if m and m.group(1).upper().startswith(("DEFINE_CURVE", "DEFINE_TABLE")):
            titled = m.group(1).upper().endswith("_TITLE")
            j = i + 1
            if titled:
                j += 1
            while j < len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith("$")):
                j += 1
            if j < len(lines):
                head = lines[j]
                field = head[:10].strip() if len(head) >= 10 else head.strip()
                if "," in head:
                    field = head.split(",")[0].strip()
                try:
                    out.add(int(float(field)))
                except (TypeError, ValueError):
                    pass
            i = j
        i += 1
    return out


# ── 组装校验 ────────────────────────────────────────────────

def check_material_template(db, card_ids: Sequence[str],
                            unit_system: Optional[str] = None) -> Dict:
    """校验一组材料卡能否组装成一份 deck。

    :returns: {ok, problems:[{level,kind,message,...}], cards:[...], id_ranges:{...}}
              level: REJECT=不可组装  CONFLICT=需重编号  WARN=提示
    """
    problems: List[Dict] = []
    cards: List[Dict] = []
    for cid in card_ids:
        row = db.get_material_card(cid)
        if row is None:
            problems.append({"level": "REJECT", "kind": "missing_card",
                             "message": f"材料卡不存在: {cid}", "card_id": cid})
            continue
        cards.append(dict(row))

    if not cards:
        return {"ok": False, "problems": problems or [
            {"level": "REJECT", "kind": "empty", "message": "模板至少要包含一张材料卡"}],
            "cards": [], "id_ranges": {"MID": [], "LCID": []}}

    # ① 单位制一致
    units = sorted({c["unit_system"] for c in cards if c.get("unit_system")})
    target = unit_system or (units[0] if len(units) == 1 else None)
    if len(units) > 1:
        problems.append({
            "level": "REJECT", "kind": "unit_system",
            "message": f"成员卡单位制不一致: {' vs '.join(units)}；"
                       f"LS-DYNA 无量纲，混用不会报错但结果差若干个数量级",
            "units": units,
        })
    elif unit_system and units and units[0] != unit_system:
        problems.append({
            "level": "REJECT", "kind": "unit_system",
            "message": f"模板声明 {unit_system}，成员卡是 {units[0]}",
            "units": units,
        })

    # ② MID 撞车
    by_mid: Dict[int, List[Dict]] = {}
    for c in cards:
        if c.get("source_mid") is None:
            continue
        by_mid.setdefault(int(c["source_mid"]), []).append(c)
    for mid, group in sorted(by_mid.items()):
        if len(group) > 1:
            names = "、".join(g.get("title") or g["id"][:8] for g in group)
            problems.append({
                "level": "CONFLICT", "kind": "mid_collision", "mid": mid,
                "message": f"MID {mid} 被 {len(group)} 张卡同时占用({names})，"
                           f"后加载的会静默覆盖前者",
                "card_ids": [g["id"] for g in group],
            })

    # ③ LCID 撞车（曲线原文随卡走，跨源文件极易重号）
    lcid_owner: Dict[int, List[str]] = {}
    for c in cards:
        for lcid in lcids_in_card(c.get("keyword_text") or ""):
            lcid_owner.setdefault(int(lcid), []).append(c["id"])
    for lcid, owners in sorted(lcid_owner.items()):
        uniq = sorted(set(owners))
        if len(uniq) > 1:
            problems.append({
                "level": "CONFLICT", "kind": "lcid_collision", "lcid": lcid,
                "message": f"曲线号 {lcid} 被 {len(uniq)} 张卡各自定义，"
                           f"撞号后曲线会张冠李戴且求解器不报错",
                "card_ids": uniq,
            })

    ok = not any(p["level"] in ("REJECT", "CONFLICT") for p in problems)
    return {
        "ok": ok,
        "unit_system": target,
        "problems": problems,
        "cards": [{"id": c["id"], "title": c.get("title"), "mat_type": c.get("mat_type"),
                   "unit_system": c.get("unit_system"), "source_mid": c.get("source_mid")}
                  for c in cards],
        "id_ranges": {"MID": sorted(by_mid), "LCID": sorted(lcid_owner)},
    }


# ── 导出 ────────────────────────────────────────────────────

_HEADER = "$" + "=" * 70


def render_material_template(db, template_id: str) -> str:
    """把模板渲染成一份可 *INCLUDE 的 MAT.K。各卡原文原样拼接，不重新生成。"""
    tpl = db.get_material_template(template_id)
    if tpl is None:
        raise ValueError(f"模板不存在: {template_id}")
    items = db.list_material_template_items(template_id)
    out: List[str] = [
        "*KEYWORD",
        f"$ 材料模板: {tpl['name']}",
        f"$ 单位制: {tpl['unit_system']}   求解器: {tpl['solver_type']}   修订: {tpl['revision']}",
        f"$ 由 SDM 材料库导出，共 {len(items)} 张材料卡；各卡原文原样拼接",
        "$",
    ]
    for it in items:
        row = db.get_material_card(it["card_id"])
        if row is None:
            log.warning("模板 %s 的成员卡 %s 已不存在，跳过", template_id, it["card_id"])
            continue
        card = dict(row)          # sqlite3.Row 没有 .get()
        out.append(_HEADER)
        out.append(f"$ {card.get('title') or card.get('mat_type')}"
                   f"   MID={card.get('source_mid')}   {card.get('unit_system')}")
        out.append(_HEADER)
        text = (card["keyword_text"] or "").strip("\n")
        # 卡的原文可能自带 *KEYWORD/*END（导入时按整块存的），拼接时要去掉
        lines = [ln for ln in text.split("\n")
                 if ln.strip().upper() not in ("*KEYWORD", "*END")]
        out.extend(lines)
    out.append("*END")
    return "\n".join(out) + "\n"


def render_control_template(db, template_id: str) -> str:
    """控制卡模板是整份存档，原样吐出。"""
    tpl = db.get_control_template(template_id)
    if tpl is None:
        raise ValueError(f"控制卡模板不存在: {template_id}")
    return tpl["keyword_text"]
