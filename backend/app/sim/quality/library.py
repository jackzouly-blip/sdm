"""质量卡模板库。

分两层,刻意的:

  **内置模板**(随代码走,只读):目前只有一张"碰撞通用 5mm 网格卡"。它是缺省底座,
  任何项目没指定模板时用它。不可改不可删——改了它等于悄悄改掉所有历史项目的
  验收标准。

  **用户模板**(落数据目录,可增删改):客户自己的规范放这里。做法是从某张模板
  派生 + 记录差异,而不是从零填一张——从零填必然漏项,漏掉的项会静默变成"不检查"。

模板 → 项目实例也是同样的派生关系:实例记的是"基于哪张模板 + 改了哪几项 + 每项
的依据"。这样工程师看到的永远是"这 3 项按客户要求改了、依据在这、其余沿用 XX",
而不是一张来历不明的卡。
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from . import ansa_mpar, ansa_qual
from .model import QualityCard

BUILTIN_DIR = os.path.join(os.path.dirname(__file__), "templates")
CRITERIA_FILE = "criteria.ansa_qual"
MESH_FILE = "mesh.ansa_mpar"
META_FILE = "template.json"

DEFAULT_TEMPLATE_ID = "generic-crash-5mm"


@dataclass
class Override:
    """实例相对模板的一项改动。

    `source` 是**强制**的:AI 从需求文档生成实例时,每一项覆盖都必须能指回原文
    的哪一句。没有出处的数值就是编的——而这个数字会一路流进网格验收。
    """
    target: str          # "criteria:warping [shells]:failed" 或 "mesh:target_element_length"
    old_value: str
    new_value: str
    source: str = ""     # 依据出处(需求文档某页某句 / 人工调整说明)
    by: str = ""         # 谁改的:ai / 用户名


@dataclass
class TemplateMeta:
    id: str
    name: str
    source: str = ""
    revision: str = ""
    scope: str = ""
    description: str = ""
    builtin: bool = False
    based_on: str = ""                      # 派生自哪张模板
    created_by: str = ""                    # 导入/派生者;老模板此字段为空,视为仅管理员可管
    overrides: List[Override] = field(default_factory=list)


class QualityCardLibrary:
    """模板库:内置只读 + 用户可写,按 id 统一寻址。

    extra_dirs 是额外的只读检索目录。项目派生实例时用它把全局模板库挂进来:
    派生的**产物**落项目目录(user_dir),而派生的**底座**可以来自全局库——
    没有它,导入的客户模板在项目里根本选不着。
    """

    def __init__(self, user_dir: str = "", extra_dirs: Optional[List[str]] = None) -> None:
        self.user_dir = user_dir
        self.extra_dirs = list(extra_dirs or [])
        if user_dir:
            os.makedirs(user_dir, exist_ok=True)

    # ── 查 ──────────────────────────────────────────────────────────────────
    def _dirs(self) -> List[str]:
        return [d for d in (BUILTIN_DIR, *self.extra_dirs, self.user_dir)
                if d and os.path.isdir(d)]

    def _resolve(self, template_id: str) -> str:
        # 用户目录后查,因此同名时用户模板生效——但内置的 builtin 标记会跟着变,
        # 所以 UI 上要能看出"这张覆盖了同名内置模板"
        found = ""
        for base in self._dirs():
            path = os.path.join(base, template_id)
            if os.path.isfile(os.path.join(path, META_FILE)):
                found = path
        if not found:
            raise KeyError(f"质量卡模板不存在: {template_id}")
        return found

    def list_templates(self) -> List[TemplateMeta]:
        out: Dict[str, TemplateMeta] = {}
        for base in self._dirs():
            for name in sorted(os.listdir(base)):
                meta_path = os.path.join(base, name, META_FILE)
                if not os.path.isfile(meta_path):
                    continue
                out[name] = self._read_meta(meta_path)
        return list(out.values())

    def get_meta(self, template_id: str) -> TemplateMeta:
        return self._read_meta(os.path.join(self._resolve(template_id), META_FILE))

    @staticmethod
    def _read_meta(path: str) -> TemplateMeta:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        overrides = [Override(**o) for o in raw.pop("overrides", [])]
        return TemplateMeta(**raw, overrides=overrides)

    def load(self, template_id: str = DEFAULT_TEMPLATE_ID) -> QualityCard:
        """载入一张卡(判定侧 + 生成侧 + 元数据)。"""
        path = self._resolve(template_id)
        meta = self._read_meta(os.path.join(path, META_FILE))
        qual = ansa_qual.load(os.path.join(path, CRITERIA_FILE))
        mpar = ansa_mpar.load(os.path.join(path, MESH_FILE))
        return QualityCard(
            id=meta.id,
            name=meta.name,
            criteria=qual.data,
            mesh_params=mpar.data,
            source=meta.source,
            revision=meta.revision,
            scope=meta.scope,
            description=meta.description,
            builtin=meta.builtin,
        )

    def open_files(self, template_id: str):
        """拿到可回写的文件视图,供派生实例时改值后导出给 ANSA。"""
        path = self._resolve(template_id)
        return (
            ansa_qual.load(os.path.join(path, CRITERIA_FILE)),
            ansa_mpar.load(os.path.join(path, MESH_FILE)),
        )

    # ── 派生 ────────────────────────────────────────────────────────────────
    def derive(
        self,
        base_id: str,
        new_id: str,
        name: str,
        overrides: Optional[List[Override]] = None,
        *,
        source: str = "",
        revision: str = "",
        scope: str = "",
        description: str = "",
        created_by: str = "",
    ) -> TemplateMeta:
        """从一张模板派生出新模板/项目实例,并把改动逐项留痕。

        改动直接写进 .ansa_qual / .ansa_mpar 的副本里,因此派生结果**本身就是
        一份合法的 ANSA 卡**,可以直接交回 ANSA 跑批处理——不需要再做一次转换。
        """
        if not self.user_dir:
            raise RuntimeError("未配置用户模板目录,无法派生")
        base_path = self._resolve(base_id)
        dest = os.path.join(self.user_dir, new_id)
        if os.path.exists(dest):
            raise FileExistsError(f"模板已存在: {new_id}")
        os.makedirs(dest)
        shutil.copyfile(os.path.join(base_path, CRITERIA_FILE), os.path.join(dest, CRITERIA_FILE))
        shutil.copyfile(os.path.join(base_path, MESH_FILE), os.path.join(dest, MESH_FILE))

        # 覆盖项有一条无效就整体回滚:半张卡比没有卡更危险——它看起来是完整的,
        # 而且会占住 id 让重试直接 FileExistsError。
        try:
            applied: List[Override] = []
            qual = ansa_qual.load(os.path.join(dest, CRITERIA_FILE))
            mpar = ansa_mpar.load(os.path.join(dest, MESH_FILE))
            for ov in overrides or []:
                applied.append(_apply_override(qual, mpar, ov))
            with open(os.path.join(dest, CRITERIA_FILE), "w", encoding="utf-8", newline="") as f:
                f.write(qual.dumps())
            with open(os.path.join(dest, MESH_FILE), "w", encoding="utf-8", newline="") as f:
                f.write(mpar.dumps())

            meta = TemplateMeta(
                id=new_id, name=name, source=source, revision=revision, scope=scope,
                description=description, builtin=False, based_on=base_id,
                created_by=created_by, overrides=applied,
            )
            with open(os.path.join(dest, META_FILE), "w", encoding="utf-8") as f:
                json.dump(asdict(meta), f, ensure_ascii=False, indent=2)
            return meta
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)
            raise

    def edit_content(self, template_id: str, overrides: List[Override]) -> TemplateMeta:
        """在线修改用户模板的内容(阈值/网格参数),逐项留痕。

        与派生同一条规矩:改动直接落进 .ansa_qual/.ansa_mpar 并追加进 overrides
        ——模板改完仍是一份可直接交回 ANSA 的合法卡,且每项改动都能指回出处。
        内置模板不可改:改了它等于悄悄改掉所有历史项目的验收标准,想改就以它
        为底座派生一张用户模板。
        """
        path = self._resolve(template_id)
        if _in_builtin(path):
            raise PermissionError("内置模板不可修改,请从它派生一张用户模板后再改")
        meta = self._read_meta(os.path.join(path, META_FILE))
        qual = ansa_qual.load(os.path.join(path, CRITERIA_FILE))
        mpar = ansa_mpar.load(os.path.join(path, MESH_FILE))
        # 先全部在内存里落好:有一条无效就一个字节都不写盘,不留半张卡
        applied = [_apply_override(qual, mpar, ov) for ov in overrides]
        with open(os.path.join(path, CRITERIA_FILE), "w", encoding="utf-8", newline="") as f:
            f.write(qual.dumps())
        with open(os.path.join(path, MESH_FILE), "w", encoding="utf-8", newline="") as f:
            f.write(mpar.dumps())
        meta.overrides.extend(applied)
        with open(os.path.join(path, META_FILE), "w", encoding="utf-8") as f:
            json.dump(asdict(meta), f, ensure_ascii=False, indent=2)
        return meta

    def update_meta(self, template_id: str, **fields) -> TemplateMeta:
        """改用户模板的元数据(名称/来源/说明等)。

        只动 template.json,不碰 .ansa_qual/.ansa_mpar——阈值改动必须走
        edit_content 或派生,两条路都逐项留痕;元数据编辑不能成为绕过留痕
        改卡的口子。
        """
        path = self._resolve(template_id)
        if _in_builtin(path):
            raise PermissionError("内置模板不可修改")
        meta = self._read_meta(os.path.join(path, META_FILE))
        allowed = {"name", "source", "revision", "scope", "description"}
        for k, v in fields.items():
            if k in allowed and v is not None:
                setattr(meta, k, str(v))
        with open(os.path.join(path, META_FILE), "w", encoding="utf-8") as f:
            json.dump(asdict(meta), f, ensure_ascii=False, indent=2)
        return meta

    def delete(self, template_id: str) -> None:
        path = self._resolve(template_id)
        if _in_builtin(path):
            raise PermissionError("内置模板不可删除")
        shutil.rmtree(path)


def _in_builtin(path: str) -> bool:
    # 不用 os.path.commonpath:Windows 上跨盘符比较会直接抛 ValueError
    return os.path.normcase(os.path.dirname(os.path.abspath(path))) == \
        os.path.normcase(os.path.abspath(BUILTIN_DIR))


def _apply_override(qual: "ansa_qual.QualFile", mpar: "ansa_mpar.MparFile", ov: Override) -> Override:
    """把一项覆盖落到文件上,并回填改前的真实旧值(而不是信调用方给的)。"""
    parts = ov.target.split(":")
    if parts[0] == "criteria":
        if len(parts) != 3:
            raise ValueError(f"判据覆盖目标格式应为 criteria:<name> [<domain>]:<band>,收到 {ov.target}")
        key, band = parts[1], parts[2]
        name, domain = key.rsplit(" [", 1)
        domain = domain.rstrip("]")
        crit = qual.data.get(name, domain)
        if crit is None:
            raise KeyError(f"质量卡里没有判据 {key}")
        old = crit.thresholds.get(band)
        qual.set_criterion(name, domain, thresholds={band: float(ov.new_value)})
        return Override(ov.target, "" if old is None else str(old), ov.new_value, ov.source, ov.by)
    if parts[0] == "mesh":
        key = parts[1]
        old = mpar.data.values.get(key, "")
        mpar.set(key, ov.new_value)
        return Override(ov.target, old, ov.new_value, ov.source, ov.by)
    raise ValueError(f"未知的覆盖目标域: {parts[0]}(应为 criteria / mesh)")
