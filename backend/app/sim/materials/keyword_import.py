"""LS-DYNA 关键字材料段解析与导入。

与 deck/parser.py 的定位不同：那边为渲染只取网格四类卡，这边只取材料三类卡
（*MAT_*、*DEFINE_CURVE、*DEFINE_TABLE）。列宽也不同——材料卡是 10 列宽 × 8 字段，
曲线点是 20 列宽 × 2 字段——所以不共用切分代码。

格式要点（种子文件 ref/06_Material_T_mm_12.k.key 实测）：

  - `$` 开头是注释；逐行判定定长/自由格式（含逗号走自由格式）。
  - `*..._TITLE` 变体的第一条非注释行是标题。
  - `*DEFINE_TABLE` 的首数据行是 TBID 行（10 列宽），其后是 20 列宽的
    `VALUE [LCID]` 条目行；LCID 留空时，成员曲线**按文件顺序跟在表后面**
    （HyperMesh/Primer 都这么导出），按序认领。
  - MID 与 TBID/LCID 是不同的编号空间，同一个数可能同时是某材料的 MID 和
    某张表的 TBID（种子文件里真实存在）。因此**不做**"扫数据字段撞 id 集合"
    的猜测式关联，只按各材料类型的已知字段位置取曲线引用——未知类型宁可
    不关联，卡的原文块仍是完整的。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ...logger import get_logger

log = get_logger(__name__)

_W_MAT = 10    # 材料卡/表头行字段宽
_W_PT = 20     # 曲线点、表条目字段宽


# --- 关键字块切分 ---------------------------------------------------------

@dataclass
class KwBlock:
    order: int              # 文件内顺序，表→成员曲线的按序认领靠它
    keyword: str            # 大写关键字，含 _TITLE 后缀
    title: str              # _TITLE 变体的标题行，否则空
    data: List[str]         # 非注释数据行（不含标题行）
    raw: str                # 原文（含注释行），verbatim 保留用


def _split_fields(line: str, width: int, count: int) -> List[str]:
    if "," in line:
        out = [f.strip() for f in line.split(",")]
    else:
        out = [line[i * width: (i + 1) * width].strip() for i in range(count)]
    while len(out) < count:
        out.append("")
    return out


def _to_f(s: str) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _to_i(s: str) -> Optional[int]:
    v = _to_f(s)
    return None if v is None else int(v)


def read_blocks(text: str) -> List[KwBlock]:
    blocks: List[KwBlock] = []
    cur_kw = ""
    cur_lines: List[str] = []

    def flush() -> None:
        if not cur_kw:
            return
        titled = cur_kw.endswith("_TITLE")
        title = ""
        data: List[str] = []
        for ln in cur_lines[1:]:
            s = ln.rstrip("\r\n")
            if not s.strip() or s.lstrip().startswith("$"):
                continue
            if titled and not title:
                title = s.strip()
            else:
                data.append(s)
        blocks.append(KwBlock(len(blocks), cur_kw, title, data,
                              "".join(cur_lines).rstrip("\n")))

    for line in text.splitlines(keepends=True):
        if line.startswith("*"):
            flush()
            cur_kw = line.strip().upper()
            cur_lines = [line]
        elif cur_kw:
            cur_lines.append(line)
    flush()
    return blocks


# --- 三类卡的解析 ---------------------------------------------------------

@dataclass
class ParsedCurve:
    order: int
    lcid: int
    title: str
    sfa: float
    sfo: float
    offa: float
    offo: float
    points: List[Tuple[float, float]]
    raw: str


@dataclass
class ParsedTable:
    order: int
    tbid: int
    title: str
    entries: List[Tuple[float, Optional[int]]]   # (条件值, 显式 LCID 或 None)
    member_lcids: List[int] = field(default_factory=list)  # 认领后回填
    raw: str = ""


@dataclass
class ParsedMat:
    order: int
    mat_type: str            # 关键字去掉 *MAT_ 前缀与 _TITLE 后缀
    title: str
    mid: Optional[int]
    rows: List[List[str]]    # 10 列宽切好的数据行
    raw: str
    # [(引用 id, 曲线语义)]，id 可能是 TBID 或 LCID
    refs: List[Tuple[int, str]] = field(default_factory=list)


# 各材料类型的曲线引用字段位置 (行, 列) 与语义。未列出的类型不做关联——
# 见模块注释：编号空间会撞，猜比不猜更危险。
_CURVE_REFS: Dict[str, Tuple[Tuple[Tuple[int, int], str], ...]] = {
    "PIECEWISE_LINEAR_PLASTICITY": (((1, 2), "stress_strain"),
                                    ((1, 3), "strain_rate_scale")),
    "MODIFIED_PIECEWISE_LINEAR_PLASTICITY": (((1, 2), "stress_strain"),
                                             ((1, 3), "strain_rate_scale")),
    "PLASTICITY_COMPRESSION_TENSION": (((1, 0), "stress_strain"),
                                       ((1, 1), "stress_strain"),
                                       ((1, 2), "strain_rate_scale"),
                                       ((1, 3), "strain_rate_scale")),
    "MOONEY-RIVLIN_RUBBER": (((1, 3), "force_deflection"),),
    "MODIFIED_HONEYCOMB": tuple([((1, i), "stress_strain") for i in range(7)]
                                + [((1, 7), "strain_rate_scale")]),
}

# 各材料类型首行的标量性能字段位置。密度所有类型都在 (0,1)。
_PROP_FIELDS: Dict[str, Dict[str, Tuple[int, int]]] = {
    "ELASTIC": {"youngs_modulus": (0, 2), "poisson_ratio": (0, 3)},
    "RIGID": {"youngs_modulus": (0, 2), "poisson_ratio": (0, 3)},
    "NULL": {"youngs_modulus": (0, 6), "poisson_ratio": (0, 7)},
    "PIECEWISE_LINEAR_PLASTICITY": {"youngs_modulus": (0, 2),
                                    "poisson_ratio": (0, 3),
                                    "yield_strength": (0, 4)},
    "MODIFIED_PIECEWISE_LINEAR_PLASTICITY": {"youngs_modulus": (0, 2),
                                             "poisson_ratio": (0, 3),
                                             "yield_strength": (0, 4)},
    "PLASTICITY_COMPRESSION_TENSION": {"youngs_modulus": (0, 2),
                                       "poisson_ratio": (0, 3)},
    "SPOTWELD": {"youngs_modulus": (0, 2), "poisson_ratio": (0, 3),
                 "yield_strength": (0, 4)},
    "MODIFIED_HONEYCOMB": {"youngs_modulus": (0, 2), "poisson_ratio": (0, 3),
                           "yield_strength": (0, 4)},
    "MOONEY-RIVLIN_RUBBER": {"poisson_ratio": (0, 2)},
}

# 单位随导入时声明的单位制走，不强转 SI（见设计文档）。
_UNIT_MAPS: Dict[str, Dict[str, str]] = {
    "t-mm-s": {"density": "t/mm^3", "youngs_modulus": "MPa",
               "yield_strength": "MPa", "poisson_ratio": ""},
    "SI": {"density": "kg/m^3", "youngs_modulus": "Pa",
           "yield_strength": "Pa", "poisson_ratio": ""},
}

_STRESS_STRAIN_AXES = {
    "x_quantity": "effective_plastic_strain", "x_unit": "-",
    "y_quantity": "effective_stress",
}
_RATE_AXES = {
    "x_quantity": "strain_rate", "x_unit": "1/s",
    "y_quantity": "scale_factor", "y_unit": "-",
}


@dataclass
class ParseResult:
    mats: List[ParsedMat]
    curves: Dict[int, ParsedCurve]
    tables: Dict[int, ParsedTable]
    warnings: List[str]


def parse_material_file(text: str) -> ParseResult:
    mats: List[ParsedMat] = []
    curves: Dict[int, ParsedCurve] = {}
    tables: List[ParsedTable] = []
    warnings: List[str] = []

    for b in read_blocks(text):
        if b.keyword.startswith("*MAT_"):
            mat_type = re.sub(r"_TITLE$", "", b.keyword[len("*MAT_"):])
            rows = [_split_fields(r, _W_MAT, 8) for r in b.data]
            mid = _to_i(rows[0][0]) if rows else None
            if mid is None:
                warnings.append(f"材料卡缺 MID，跳过: {b.keyword} {b.title}")
                continue
            m = ParsedMat(b.order, mat_type, b.title, mid, rows, b.raw)
            for (ri, ci), semantic in _CURVE_REFS.get(mat_type, ()):
                if ri < len(rows):
                    ref = _to_i(rows[ri][ci])
                    if ref:
                        m.refs.append((ref, semantic))
            mats.append(m)

        elif b.keyword.startswith("*DEFINE_CURVE"):
            if not b.data:
                continue
            head = _split_fields(b.data[0], _W_MAT, 8)
            lcid = _to_i(head[0])
            if lcid is None:
                warnings.append(f"曲线缺 LCID，跳过: {b.title}")
                continue
            pts: List[Tuple[float, float]] = []
            for r in b.data[1:]:
                f = _split_fields(r, _W_PT, 2)
                x, y = _to_f(f[0]), _to_f(f[1])
                if x is not None and y is not None:
                    pts.append((x, y))
            curves[lcid] = ParsedCurve(
                b.order, lcid, b.title,
                _to_f(head[2]) or 1.0, _to_f(head[3]) or 1.0,
                _to_f(head[4]) or 0.0, _to_f(head[5]) or 0.0,
                pts, b.raw,
            )

        elif b.keyword.startswith("*DEFINE_TABLE"):
            if not b.data:
                continue
            tbid = _to_i(_split_fields(b.data[0], _W_MAT, 8)[0])
            if tbid is None:
                warnings.append(f"表缺 TBID，跳过: {b.title}")
                continue
            entries: List[Tuple[float, Optional[int]]] = []
            for r in b.data[1:]:
                f = _split_fields(r, _W_PT, 2)
                v = _to_f(f[0])
                if v is not None:
                    entries.append((v, _to_i(f[1])))
            tables.append(ParsedTable(b.order, tbid, b.title, entries, raw=b.raw))

    # 表条目没写 LCID 的，按文件顺序认领跟在表后面的曲线
    claimed: set = set()
    for t in sorted(tables, key=lambda x: x.order):
        pending = sum(1 for _, lc in t.entries if lc is None)
        pool = sorted(
            (c for c in curves.values() if c.order > t.order and c.lcid not in claimed),
            key=lambda c: c.order,
        )
        if pending > len(pool):
            warnings.append(f"表 {t.tbid} 需认领 {pending} 条曲线，文件里只剩 {len(pool)} 条")
        it = iter(pool)
        for value, lc in t.entries:
            if lc is None:
                nxt = next(it, None)
                lc = nxt.lcid if nxt else None
            if lc is not None:
                claimed.add(lc)
                t.member_lcids.append(lc)

    return ParseResult(mats, curves, {t.tbid: t for t in tables}, warnings)


# --- 物理材料归一 ---------------------------------------------------------

def normalize_title(title: str, mat_type: str) -> Tuple[str, str]:
    """卡标题 → (物理材料名, 变体)。

    剥两类后缀：Primer 重名后缀 `.1`（同名材料的模型变体），以及 `_NULL`
    （接触用伴生卡）。MAT_NULL 类型本身即 null 变体——不管叫什么名字，
    它都不是一个独立的物理材料模型。
    """
    name = title.strip() or f"MAT_{mat_type}"
    variant = "primary"
    m = re.match(r"^(.+)\.(\d+)$", name)
    if m:
        name, variant = m.group(1).strip(), "alt"
    if re.search(r"[_ ]NULL$", name, re.IGNORECASE):
        name = re.sub(r"[_ ]NULL$", "", name, flags=re.IGNORECASE).strip()
        variant = "null"
    if mat_type == "NULL":
        variant = "null"
    return name, variant


def guess_category(name: str, mat_type: str) -> str:
    n = name.upper()
    if "GLUE" in n or "ADHESIVE" in n:
        return "adhesive"
    if "GLASS" in n or mat_type == "LAMINATED_GLASS":
        return "glass"
    if "RUBBER" in n or "EPDM" in n or "MOONEY" in mat_type:
        return "rubber"
    if "HONEYCOMB" in mat_type or "FOAM" in n:
        return "foam"
    if re.search(r"(^|[-_ (])AA?\d{4}", n) or "-AL-" in n or "ALUMIN" in n:
        return "aluminum"
    if re.search(r"^(PP|PA\d|PC\b|ABS|POM|TPO)", n) or re.search(r"[-.]PP[-.]", n) \
            or re.search(r"GF\d", n):
        return "plastic"
    if re.search(r"^(HC|DC|DX|SAPH|SPC|SPH|SPFC|QSTE|Q\d|B\d{3}|HS\d|CR\d|GMW)", n) \
            or "STEEL" in n or mat_type == "SPOTWELD":
        return "steel"
    return "other"


# --- 导入 -----------------------------------------------------------------

def _bundle_raw(m: ParsedMat, pr: ParseResult) -> str:
    """材料卡 + 它引用的表/曲线，拼成自包含关键字块。顺序保持文件原序。"""
    parts: List[Tuple[int, str]] = [(m.order, m.raw)]
    seen = {m.order}

    def add(order: int, raw: str) -> None:
        if order not in seen:
            seen.add(order)
            parts.append((order, raw))

    for ref, _sem in m.refs:
        t = pr.tables.get(ref)
        if t is not None:
            add(t.order, t.raw)
            for lc in t.member_lcids:
                c = pr.curves.get(lc)
                if c is not None:
                    add(c.order, c.raw)
        elif ref in pr.curves:
            c = pr.curves[ref]
            add(c.order, c.raw)
    return "\n".join(raw for _, raw in sorted(parts))


def _curve_dicts(m: ParsedMat, pr: ParseResult, y_stress_unit: str,
                 taken: set, warnings: List[str]) -> List[Dict]:
    """一张卡引用的曲线 → 曲线行。表展开成族：family_key=tbid，条件=应变率。"""
    out: List[Dict] = []

    def one(c: ParsedCurve, semantic: str, family: str,
            condition: Optional[Dict]) -> Dict:
        axes = dict(_STRESS_STRAIN_AXES, y_unit=y_stress_unit) \
            if semantic == "stress_strain" else dict(_RATE_AXES) \
            if semantic == "strain_rate_scale" else {}
        return {
            "curve_type": semantic, "title": c.title, "family_key": family,
            "condition": condition, "points": [[x, y] for x, y in c.points],
            "scale": {"sfa": c.sfa, "sfo": c.sfo, "offa": c.offa, "offo": c.offo},
            "source_lcid": c.lcid, **axes,
        }

    for ref, semantic in m.refs:
        t = pr.tables.get(ref)
        if t is not None:
            for (value, _), lc in zip(t.entries, t.member_lcids + [None] * len(t.entries)):
                c = pr.curves.get(lc) if lc else None
                if c is None or lc in taken:
                    continue
                taken.add(lc)
                out.append(one(c, semantic, f"table:{ref}",
                               {"strain_rate": value, "unit": "1/s"}))
        elif ref in pr.curves:
            if ref in taken:
                continue
            taken.add(ref)
            out.append(one(pr.curves[ref], semantic, f"curve:{ref}", None))
        else:
            warnings.append(f"卡「{m.title}」引用的曲线/表 {ref} 不在文件里")
    return out


def _props(m: ParsedMat, units: Dict[str, str]) -> List[Dict]:
    out: List[Dict] = []
    rho = _to_f(m.rows[0][1]) if m.rows else None
    if rho:
        out.append({"name": "density", "value": rho, "unit": units.get("density", "")})
    for prop, (ri, ci) in _PROP_FIELDS.get(m.mat_type, {}).items():
        if ri < len(m.rows):
            v = _to_f(m.rows[ri][ci])
            if v:  # 0 在这些字段里意味着"没填"（种子文件里大量 SIGY=0 而曲线给定）
                out.append({"name": prop, "value": v, "unit": units.get(prop, "")})
    return out


def _fingerprint(texts: List[str]) -> str:
    h = hashlib.sha1()
    for t in sorted(texts):
        h.update(t.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()


def import_material_text(
    db,
    text: str,
    unit_system: str = "t-mm-s",
    solver_type: str = "lsdyna",
    source: str = "",
    actor: str = "",
) -> Dict:
    """把一份关键字文本导入材料库。

    幂等：按物理材料名比对内容指纹——不变则跳过；变了则整体替换并
    revision+1。半新半旧比过时更危险，所以是整体替换而非逐条合并。
    """
    pr = parse_material_file(text)
    warnings = list(pr.warnings)
    units = _UNIT_MAPS.get(unit_system, {})
    y_stress_unit = units.get("yield_strength", "")

    # 按物理材料分组；组内 primary 卡在前（性能参数从它取）
    groups: Dict[str, List[Tuple[ParsedMat, str]]] = {}
    for m in pr.mats:
        name, variant = normalize_title(m.title, m.mat_type)
        groups.setdefault(name, []).append((m, variant))
    for g in groups.values():
        g.sort(key=lambda mv: (mv[1] != "primary", mv[0].order))

    created = updated = unchanged = 0
    cards_total = curves_total = 0
    taken_curves: set = set()

    for name, members in groups.items():
        primary, _ = members[0]
        cards: List[Dict] = []
        curves: List[Dict] = []
        for m, variant in members:
            cards.append({
                "solver_type": solver_type,
                "mat_type": m.mat_type,
                "title": m.title,
                "variant": variant,
                "unit_system": unit_system,
                "source_mid": m.mid,
                "params": {p["name"]: p["value"] for p in _props(m, units)},
                "keyword_text": _bundle_raw(m, pr),
            })
            curves.extend(_curve_dicts(m, pr, y_stress_unit, taken_curves, warnings))

        fp = _fingerprint([c["keyword_text"] for c in cards])
        row = db.get_material_by_name(name)
        if row is None:
            mid = db.create_material(
                name,
                category=guess_category(name, primary.mat_type),
                source=source,
                created_by=actor,
            )
            db.replace_material_content(mid, _props(primary, units), curves, cards)
            created += 1
        else:
            old_fp = _fingerprint(
                [r["keyword_text"] for r in db.material_cards(row["id"])]
            )
            if old_fp == fp:
                unchanged += 1
                continue
            db.replace_material_content(
                row["id"], _props(primary, units), curves, cards, bump_revision=True
            )
            updated += 1
        cards_total += len(cards)
        curves_total += len(curves)

    orphan = len(pr.curves) - len(taken_curves)
    if orphan > 0:
        warnings.append(f"{orphan} 条曲线未被任何材料卡引用，未入库（原文完整保留在来源文件）")

    return {
        "materials_created": created,
        "materials_updated": updated,
        "materials_unchanged": unchanged,
        "cards": cards_total,
        "curves": curves_total,
        "warnings": warnings,
    }
