"""`.ansa_mpar` 的解析与回写。

比 qual 简单:纯 `key = value` + `#` 注释 + 段标题。但同样要求往返保真——
值里混着数字（5.）、布尔（false）、枚举（Recognize features）和表达式
（0.667*Lmin）,一旦提前解析成数再写回去,`5.` 会变 `5.0`、表达式会被毁掉,
而这个文件是要交回 ANSA 的。所以值一律按字符串保管。

段标题（Perimeters / Fillets / Flanges 2D / Holes 3D / Stamps / Ribs …）被一并
记录:它们是网格生成侧的旋钮分类,后续按段做参数调优时要靠它定位。
"""
from __future__ import annotations

import re
from typing import Dict, List

from .model import MeshParams

_KV_RE = re.compile(r"^(?P<lead>\s*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*?)\s*$")
_SECTION_RE = re.compile(r"^#\s*(?P<title>[A-Za-z][A-Za-z0-9 /&\-]*?)\s*$")
# 纯装饰性的注释行,不当段标题
_NOT_SECTION = {"ANSA Version", "Mesh parameters"}


class MparFile:
    """一份 .ansa_mpar 的可回写视图。"""

    def __init__(self, lines: List[str]) -> None:
        self._lines = lines
        self._key_line: Dict[str, int] = {}
        self.data = MeshParams()
        self._parse()

    def _parse(self) -> None:
        section = ""
        for i, line in enumerate(self._lines):
            stripped = line.strip()
            if stripped.startswith("#"):
                m = _SECTION_RE.match(stripped)
                if m:
                    title = m.group("title").strip()
                    if title and title not in _NOT_SECTION:
                        section = title
                continue
            if not stripped:
                continue
            kv = _KV_RE.match(line)
            if not kv:
                continue
            key, value = kv.group("key"), kv.group("value")
            self.data.values[key] = value
            self.data.sections[key] = section
            self._key_line[key] = i
            if key == "ANSA_Version":
                self.data.ansa_version = value
            elif key == "mesh_parameters_name":
                self.data.name = value

    def set(self, key: str, value: str) -> None:
        """改一个参数。键必须已存在——凭空新增字段 ANSA 未必认,宁可报错。"""
        if key not in self._key_line:
            raise KeyError(f"网格参数卡里没有 {key}")
        i = self._key_line[key]
        lead = _KV_RE.match(self._lines[i]).group("lead")
        # 保持原有的列对齐:ANSA 自己导出的文件是对齐的,回写后错开虽不影响读取,
        # 但会让人 diff 两版卡时满屏噪声
        original_key_field = self._lines[i][len(lead):].split("=")[0]
        self._lines[i] = f"{lead}{original_key_field}= {value}\n"
        self.data.values[key] = value

    def dumps(self) -> str:
        return "".join(self._lines)


def load(path: str) -> MparFile:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return MparFile(f.readlines())


def loads(text: str) -> MparFile:
    return MparFile(text.splitlines(keepends=True))
