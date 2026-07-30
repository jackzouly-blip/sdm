"""规则抽取与 AI 解析的确定性合并。

两条线**独立跑、事后合**:规则侧不进 AI 的提示词(避免锚定——模型拿到现成答案
就退化成核对员),AI 侧的数值也不反过来改规则读数。合并只做三件事,全部是代码,
不引入模型:

  对齐  锚点优先 source_ref 的"页/表/行"(两边都记出处,比标题匹配可靠,且天然
        处理粒度差异——AI 把一项拆成 X/Z 两条,但都指向同一表行);表行锚点缺失
        时退到"同页 + 归一化标题"。对不齐的**不硬合**:重复是看得见的问题,
        错误合并是看不见的问题。

  裁决  同锚点两边读数一致 → 采信 AI 条目(粒度更细、描述更全),规则读数视为
        佐证;**数值冲突 → 强制 needs_clarification**,两个读数都写进
        clarification_hint 交给人——代码不判"谁对",模型更不行。

  兜底  仅规则抽到的条目按 extracted_by=rule 保留:视觉/AI 失败的页,表格内容
        规则侧本来就有,不该出现"原文缺失、靠 memory 推断"。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

_PAGE_RE = re.compile(r"第\s*(\d+)\s*页")
_TABLE_RE = re.compile(r"表\s*(\d+)")
_ROW_RE = re.compile(r"第\s*(\d+)\s*行")

# 标题归一化时剥掉的修饰:空白/连接符/编号后缀/"测试"这类动词尾巴。
# 只用于对齐,不改条目本身。
_TITLE_STRIP_RE = re.compile(r"[\s\-—_·、.·]+")
_TITLE_PAREN_RE = re.compile(r"[（(][^（）()]*[)）]")
_TITLE_SUFFIX_RE = re.compile(r"(测试|试验|分析|要求)+$")
_TITLE_NUM_TAIL_RE = re.compile(r"\d+$")


def _anchor(source_ref: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """出处 → (页, 表, 行)。缺失的段为 None。"""
    ref = source_ref or ""
    page = _PAGE_RE.search(ref)
    table = _TABLE_RE.search(ref)
    row = _ROW_RE.search(ref)
    return (
        int(page.group(1)) if page else None,
        int(table.group(1)) if table else None,
        int(row.group(1)) if row else None,
    )


def _norm_title(title: str) -> str:
    t = _TITLE_PAREN_RE.sub("", title or "")
    t = _TITLE_STRIP_RE.sub("", t).lower()
    t = _TITLE_NUM_TAIL_RE.sub("", t)
    return _TITLE_SUFFIX_RE.sub("", t)


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))


def _metric_values(items: List[Dict]) -> Dict[Tuple[str, str], List[float]]:
    """(量名, 单位) → 读到的值集合。

    kind(内控/对外/例外)不进键:同一个量两边按不同 kind 归类很常见,
    值能对上就不算冲突——宁可漏报冲突,不可把归类差异误报成读数矛盾。
    """
    out: Dict[Tuple[str, str], List[float]] = {}
    for it in items:
        for m in it.get("metrics") or []:
            q = str(m.get("quantity") or "").strip()
            unit = str(m.get("unit") or "").strip()
            if not q or not unit:
                continue      # 没有量名的裸数对不上号,不参与比对
            try:
                v = float(m.get("value"))
            except (TypeError, ValueError):
                continue
            out.setdefault((q, unit), []).append(v)
    return out


def _conflicts(rule_item: Dict, ai_group: List[Dict]) -> List[str]:
    """一组对齐条目里,规则与 AI 读数矛盾之处。空列表 = 无冲突。

    判"冲突"从严:两边**都明确给出**且对不上才算。一边缺失是遗漏不是矛盾,
    只计入统计,不打扰人。
    """
    out: List[str] = []

    rb = str(rule_item.get("baseline") or "")
    ai_bases = {str(a.get("baseline") or "") for a in ai_group} - {""}
    if rb and ai_bases and rb not in ai_bases:
        label = {"required": "门槛", "reference": "参考"}
        out.append(
            f"分析基准:规则读作'{label.get(rb, rb)}',"
            f"AI 读作'{'/'.join(label.get(b, b) for b in sorted(ai_bases))}'"
        )

    rp = int(rule_item.get("load_points") or 0)
    ap = sum(int(a.get("load_points") or 0) for a in ai_group)
    if rp and ap and rp != ap:
        out.append(f"加载点位数:规则数出 {rp} 个,AI 计 {ap} 个")

    rd = rule_item.get("indenter_diameter_mm")
    ai_ds = [a.get("indenter_diameter_mm") for a in ai_group
             if a.get("indenter_diameter_mm") is not None]
    if rd is not None and ai_ds and not any(_close(float(rd), float(d)) for d in ai_ds):
        out.append(f"压头直径:规则读作 {rd}mm,AI 读作 {'/'.join(str(d) for d in ai_ds)}mm")

    rule_vals = _metric_values([rule_item])
    ai_vals = _metric_values(ai_group)
    for key, rv in rule_vals.items():
        av = ai_vals.get(key)
        if not av:
            continue
        if not any(_close(x, y) for x in rv for y in av):
            q, unit = key
            out.append(
                f"{q}:规则读作 {'/'.join(str(v) for v in rv)}{unit},"
                f"AI 读作 {'/'.join(str(v) for v in av)}{unit}"
            )
    return out


def merge_rule_and_ai(rule_items: List[Dict], ai_items: List[Dict]) -> Tuple[List[Dict], Dict]:
    """合并两侧条目。返回 (合并结果, 统计)。

    结果顺序:AI 条目在前(维持其原有顺序),仅规则抽到的兜底条目排后。
    冲突不改任何一边的数值——只把 AI 条目标成待澄清并把两个读数写进 hint。
    """
    # AI 条目按锚点索引。表行级锚点一个键;只有页码的条目按 (页, 归一化标题) 索引
    by_row: Dict[Tuple[int, int, int], List[int]] = {}
    by_page_title: Dict[Tuple[int, str], List[int]] = {}
    by_title: Dict[str, List[int]] = {}
    for idx, it in enumerate(ai_items):
        page, table, row = _anchor(str(it.get("source_ref") or ""))
        title = _norm_title(str(it.get("title") or ""))
        if page is not None and table is not None and row is not None:
            by_row.setdefault((page, table, row), []).append(idx)
        elif page is not None and title:
            by_page_title.setdefault((page, title), []).append(idx)
        if title:
            by_title.setdefault(title, []).append(idx)

    merged = [dict(it) for it in ai_items]
    matched_ai: set = set()
    stats = {"ruleTotal": len(rule_items), "aiTotal": len(ai_items),
             "agreed": 0, "conflicts": 0, "ruleOnly": 0}
    rule_only: List[Dict] = []

    for r in rule_items:
        page, table, row = _anchor(str(r.get("source_ref") or ""))
        title = _norm_title(str(r.get("title") or ""))
        group = []
        if page is not None and table is not None and row is not None:
            group = by_row.get((page, table, row), [])
        if not group and page is not None and title:
            group = by_page_title.get((page, title), [])
        if not group and title:
            # 全局标题兜底收得很紧:类别必须一致,且仅当一侧缺页码锚点时才启用——
            # 真实文档里总表条目与工况页标题同名,跨页硬配就是看不见的错误合并。
            # 命中还必须唯一,一对多的模糊命中宁可不合。
            candidates = [
                i for i in by_title.get(title, [])
                if str(ai_items[i].get("category") or "") == str(r.get("category") or "")
                and (page is None or _anchor(str(ai_items[i].get("source_ref") or ""))[0] is None)
            ]
            if len(candidates) == 1:
                group = candidates

        if not group:
            item = dict(r)
            item["extracted_by"] = item.get("extracted_by") or "rule"
            rule_only.append(item)
            stats["ruleOnly"] += 1
            continue

        matched_ai.update(group)
        problems = _conflicts(r, [merged[i] for i in group])
        if not problems:
            stats["agreed"] += 1
            continue
        stats["conflicts"] += 1
        note = f"规则/AI 读数不一致({r.get('source_ref') or '出处见条目'}):" + ";".join(problems)
        for i in group:
            merged[i]["needs_clarification"] = True
            prev = str(merged[i].get("clarification_hint") or "").strip()
            merged[i]["clarification_hint"] = f"{prev} {note}".strip() if prev else note

    stats["aiOnly"] = len(ai_items) - len(matched_ai)
    return merged + rule_only, stats
