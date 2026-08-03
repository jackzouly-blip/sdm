"""文件浏览核心：路径白名单校验 + 以登录用户身份执行文件操作。

安全分两层：
  1. 白名单 + 防穿越（lexical）：请求路径先做词法归一（消解 .. 与多余斜杠），
     再校验落在 fs_roots（如 /data）之内，挡掉 ../../etc/passwd 这类穿越。
  2. OS 权限兜底：真正的列目录/读文件都通过 call_as_user 降权到登录用户执行，
     用户能不能看、能不能读完全由操作系统决定。
  另对最终真实路径（realpath，已解析符号链接）再做一次根包含校验，防止
  /data 内的符号链接指向白名单外。

在子进程（降权后）执行的函数必须是顶层函数且返回值可 pickle。
"""
from __future__ import annotations

import errno
import os
import shutil
import stat
from typing import Dict, List, Optional

from ..privilege.actas import call_as_user


class FsError(Exception):
    """文件操作错误，message 适合直接回给前端。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def normalize_under_roots(path: str, roots: List[str]) -> str:
    """词法归一并校验路径在某个根之下，返回归一化绝对路径。"""
    if not path or not path.startswith("/"):
        raise FsError("路径必须是绝对路径", 400)
    # 词法归一：消解 . / .. / 重复斜杠，不触碰文件系统
    norm = os.path.normpath(path)
    for root in roots:
        if norm == root or norm.startswith(root + "/"):
            return norm
    raise FsError(f"路径超出允许范围: {norm}", 403)


def _contains(roots: List[str], real: str) -> bool:
    """real（已 realpath）是否落在某个根内。根本身也做 realpath，
    以容忍根路径上游存在符号链接（如 /var -> /private/var）。"""
    for r in roots:
        rr = os.path.realpath(r)
        if real == rr or real.startswith(rr + "/"):
            return True
    return False


# --- 在降权子进程中执行的纯函数（顶层，可 pickle）-------------------

def _safe_str(s: str) -> str:
    """把可能含 surrogate 的字符串（非 UTF-8 文件名，os.listdir 以 surrogateescape
    保留原始字节）净化为可 JSON 序列化的合法字符串，避免整个列目录序列化失败（500）。

    很多文件名其实是 GBK 中文（Windows/中文系统所建），故先按 UTF-8，失败再尝试
    GBK/GB18030 还原中文，都不行才用 � 替换，尽量保住可读的真实名字。"""
    if not isinstance(s, str):
        return s
    try:
        s.encode("utf-8")
        return s  # 本就是合法 UTF-8
    except UnicodeEncodeError:
        raw = s.encode("utf-8", "surrogateescape")  # 还原原始字节
        for enc in ("utf-8", "gb18030", "gbk"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")


def _do_listdir(path: str) -> Dict:
    """列目录。返回 {entries: [...], realpath: str}。"""
    real = os.path.realpath(path)
    entries = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        is_link = stat.S_ISLNK(st.st_mode)
        # 符号链接解析到目标：大小/时间/类型用目标的（lstat 给的是链接自身，
        # 其 size 是目标路径字符串长度，会误导）。断链则回退到链接自身。
        target_st = st
        if is_link:
            try:
                target_st = os.stat(full)
            except OSError:
                target_st = st
        is_dir = stat.S_ISDIR(target_st.st_mode)
        entries.append(
            {
                "name": _safe_str(name),
                "is_dir": is_dir,
                "is_link": is_link,
                "size": int(target_st.st_size),
                "mtime": float(target_st.st_mtime),
                "mode": stat.filemode(st.st_mode),
            }
        )
    return {"entries": entries, "realpath": _safe_str(real)}


def _do_read_text(path: str, max_bytes: int) -> Dict:
    """读取文本预览。返回 {realpath, size, truncated, content}。"""
    real = os.path.realpath(path)
    st = os.stat(path)
    if stat.S_ISDIR(st.st_mode):
        raise IsADirectoryError(path)
    size = int(st.st_size)
    with open(path, "rb") as f:
        data = f.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    data = data[:max_bytes]
    # 二进制判定：含空字节，或采样中不可打印字节比例过高
    binary = b"\x00" in data
    if not binary and data:
        sample = data[:4096]
        nonprint = sum(1 for b in sample if b < 9 or (13 < b < 32))
        binary = nonprint / len(sample) > 0.15
    content = "" if binary else data.decode("utf-8", errors="replace")
    return {
        "realpath": real,
        "size": size,
        "truncated": truncated,
        "binary": binary,
        "content": content,
    }


def _do_stat(path: str) -> Dict:
    real = os.path.realpath(path)
    st = os.stat(path)
    return {
        "realpath": real,
        "is_dir": stat.S_ISDIR(st.st_mode),
        "size": int(st.st_size),
        "mtime": float(st.st_mtime),
    }


def _do_mkdir(path: str) -> Dict:
    """新建目录。父目录须已存在；返回父目录的 realpath 供二次校验。"""
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise FileNotFoundError(parent)
    real_parent = os.path.realpath(parent)
    os.mkdir(path)  # 受降权子进程 umask(077) 约束，权限为 0700
    return {"realpath_parent": real_parent}


def _write_all(fd: int, data: bytes) -> None:
    mv = memoryview(data)
    while mv:
        n = os.write(fd, mv)
        mv = mv[n:]


def _do_write_file(path: str, data: bytes, replace: bool = False) -> Dict:
    """写入文件。返回父目录 realpath 供二次校验。

    父目录不存在时按降权身份自动创建（目录上传需保留层级）。

    `replace` 只给**派生产物**用（轻量化 glb 等重跑后要换新的），普通上传保持
    不覆盖：用户手工传文件时静默盖掉同名件是数据丢失。
    """
    parent = os.path.dirname(path)
    # 目录上传：中间目录可能尚不存在，按当前（已降权）身份创建，umask 077 → 0700
    os.makedirs(parent, exist_ok=True)
    real_parent = os.path.realpath(parent)
    if replace:
        # 先写临时文件再原子换名：传到一半失败不会把已有产物毁掉，
        # 也不会让读的人看到半截文件。
        tmp = "%s.%d.tmp" % (path, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            try:
                _write_all(fd, data)
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except BaseException:
            # 任何一步失败都要收走临时文件，否则目录里会积一堆 .tmp
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return {"realpath_parent": real_parent}
    # O_EXCL：目标已存在则报错，避免静默覆盖
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(fd, data)
    finally:
        os.close(fd)
    return {"realpath_parent": real_parent}


# --- 对外 API（在 root 主进程调用，内部降权）------------------------

def list_dir(user: str, path: str, roots: List[str]) -> Dict:
    norm = normalize_under_roots(path, roots)
    result = call_as_user(user, _do_listdir, norm)
    # 二次校验：realpath 解析符号链接后仍须在根内
    if not _contains(roots, result["realpath"]):
        raise FsError("目标经符号链接解析后超出允许范围", 403)
    result["path"] = norm
    return result


def read_text(user: str, path: str, roots: List[str], max_bytes: int) -> Dict:
    norm = normalize_under_roots(path, roots)
    result = call_as_user(user, _do_read_text, norm, max_bytes)
    if not _contains(roots, result["realpath"]):
        raise FsError("目标经符号链接解析后超出允许范围", 403)
    result["path"] = norm
    return result


def stat_path(user: str, path: str, roots: List[str]) -> Dict:
    norm = normalize_under_roots(path, roots)
    result = call_as_user(user, _do_stat, norm)
    if not _contains(roots, result["realpath"]):
        raise FsError("目标经符号链接解析后超出允许范围", 403)
    result["path"] = norm
    return result


def _do_size(path: str) -> Dict:
    """计算路径占用大小：文件取自身大小，目录递归累加（忽略符号链接）。"""
    real = os.path.realpath(path)
    total = 0
    if os.path.isdir(path) and not os.path.islink(path):
        for root, _dirs, files in os.walk(path):
            for fn in files:
                fp = os.path.join(root, fn)
                try:
                    if not os.path.islink(fp):
                        total += os.path.getsize(fp)
                except OSError:
                    continue
    else:
        try:
            total = int(os.path.getsize(path))
        except OSError:
            total = 0
    return {"realpath": real, "size": int(total)}


def path_size(user: str, path: str, roots: List[str]) -> int:
    """以登录用户身份计算路径大小（目录递归）。"""
    norm = normalize_under_roots(path, roots)
    result = call_as_user(user, _do_size, norm)
    if not _contains(roots, result["realpath"]):
        raise FsError("目标经符号链接解析后超出允许范围", 403)
    return int(result["size"])


def make_dir(user: str, parent: str, name: str, roots: List[str]) -> Dict:
    """在 parent 下新建名为 name 的目录。"""
    safe = name.strip()
    if not safe or "/" in safe or safe in (".", ".."):
        raise FsError("非法目录名", 400)
    parent_norm = normalize_under_roots(parent, roots)
    target = os.path.join(parent_norm, safe)
    # 目标本身仍须落在根内（normpath 兜底）
    normalize_under_roots(target, roots)
    result = call_as_user(user, _do_mkdir, target)
    if not _contains(roots, result["realpath_parent"]):
        raise FsError("目标经符号链接解析后超出允许范围", 403)
    return {"path": target}


def _do_delete(path: str) -> Dict:
    """删除文件/目录(目录递归);软链只删链接本身。"""
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.exists(path):
        os.remove(path)
    else:
        raise FileNotFoundError(path)
    return {"ok": True}


def delete_path(user: str, path: str, roots: List[str]) -> Dict:
    """删除 path（文件或目录，目录递归）。禁止删除白名单根本身。"""
    norm = normalize_under_roots(path, roots)
    real = os.path.realpath(norm)
    for r in roots:
        if real == os.path.realpath(r):
            raise FsError("不能删除根目录", 400)
    if not _contains(roots, real):
        raise FsError("目标经符号链接解析后超出允许范围", 403)
    call_as_user(user, _do_delete, norm)
    return {"ok": True}


def _do_find_files(root: str, exts: tuple, limit: int, max_depth: int) -> List[str]:
    """在 root 下递归查找扩展名匹配的文件，返回相对 root 的路径列表（限深、限量）。"""
    out: List[str] = []
    base = root.rstrip("/")
    base_depth = base.count(os.sep)
    for dirpath, dirs, files in os.walk(base):
        if dirpath.count(os.sep) - base_depth >= max_depth:
            dirs[:] = []  # 达到深度上限不再下钻
        for fn in sorted(files):
            if fn.lower().endswith(exts):
                out.append(os.path.relpath(os.path.join(dirpath, fn), base))
                if len(out) >= limit:
                    return out
    return out


def find_files(
    user: str, dir_path: str, exts: tuple, roots: List[str],
    limit: int = 500, max_depth: int = 8,
) -> List[str]:
    """以用户身份在目录下递归查找指定扩展名文件，返回相对路径。"""
    norm = normalize_under_roots(dir_path, roots)
    return call_as_user(user, _do_find_files, norm, exts, limit, max_depth)


def _do_rename(src: str, dst: str) -> Dict:
    """同目录改名。源须存在、目标不得已存在（不覆盖）。返回父目录 realpath 供二次校验。"""
    if not os.path.lexists(src):
        raise FileNotFoundError(src)
    if os.path.lexists(dst):
        raise FileExistsError(dst)
    real_parent = os.path.realpath(os.path.dirname(dst))
    os.rename(src, dst)
    return {"realpath_parent": real_parent}


def rename_path(user: str, path: str, new_name: str, roots: List[str]) -> Dict:
    """把 path 在其所在目录内改名为 new_name（单段，不跨目录、不覆盖）。"""
    safe = (new_name or "").strip()
    if not safe or "/" in safe or safe in (".", ".."):
        raise FsError("非法名称", 400)
    src = normalize_under_roots(path, roots)
    dst = os.path.join(os.path.dirname(src), safe)
    normalize_under_roots(dst, roots)
    result = call_as_user(user, _do_rename, src, dst)
    if not _contains(roots, result["realpath_parent"]):
        raise FsError("目标经符号链接解析后超出允许范围", 403)
    return {"path": dst}


# --- 跨目录移动 -------------------------------------------------------

def _do_probe_move(srcs: List[str], dst_dir: str) -> Dict:
    """降权后勘察：目标是否目录、各源是否存在、是否同一文件系统。

    同设备可用 os.rename 瞬时完成；跨设备要复制字节，对大目录是分钟级操作，
    必须走异步任务。故先勘察再决定同步还是异步——这一步很轻，只做 stat。

    业务性错误经返回值回传而非抛出：本函数在降权子进程里跑，call_as_user 会
    把任何异常压成 PrivilegeError(repr)，类型与可读文案都会丢失。故只让
    OS 级异常（不存在/无权限）自然抛出，其余交父进程构造 FsError。
    """
    if not os.path.isdir(dst_dir):
        raise NotADirectoryError(dst_dir)
    dst_dev = os.stat(dst_dir).st_dev
    real_dst = os.path.realpath(dst_dir)
    items = []
    cross = False
    for s in srcs:
        if not os.path.lexists(s):
            raise FileNotFoundError(s)
        name = os.path.basename(s)
        real_src = os.path.realpath(s)
        # 把目录移进它自己（或自己的子孙）里，会造出无法访问的自嵌套结构
        if os.path.isdir(s) and (real_dst == real_src
                                 or real_dst.startswith(real_src + os.sep)):
            return {"error": {"kind": "self_nest", "name": name}}
        dst = os.path.join(dst_dir, name)
        if os.path.lexists(dst):
            return {"error": {"kind": "exists", "name": name}}
        # 用 lstat：符号链接移动的是链接本身，跟它指向哪儿无关
        if os.lstat(s).st_dev != dst_dev:
            cross = True
        items.append({"src": s, "dst": dst})
    return {"items": items, "cross_device": cross, "realpath_dst": real_dst,
            "error": None}


def _do_move_one(src: str, dst: str) -> Dict:
    """移动单个条目。同设备 os.rename，跨设备退化为复制+删除。"""
    if not os.path.lexists(src):
        raise FileNotFoundError(src)
    if os.path.lexists(dst):
        raise FileExistsError(dst)
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        # 跨文件系统：shutil.move 会复制后删源，保留符号链接语义
        shutil.move(src, dst)
    return {"dst": dst}


def probe_move(user: str, paths: List[str], dst_dir: str, roots: List[str]) -> Dict:
    """校验一次移动请求，返回 {items, cross_device}。不实际移动。

    路径白名单、根本身保护、符号链接逃逸都在这里挡掉；真正的可读可写
    仍由降权后的 OS 权限兜底。
    """
    if not paths:
        raise FsError("没有要移动的文件", 400)
    dst = normalize_under_roots(dst_dir, roots)
    srcs = []
    for p in paths:
        norm = normalize_under_roots(p, roots)
        for r in roots:
            if os.path.realpath(norm) == os.path.realpath(r):
                raise FsError("不能移动根目录", 400)
        if os.path.dirname(norm) == dst:
            raise FsError(f"{os.path.basename(norm)} 已在目标目录中", 400)
        srcs.append(norm)

    result = call_as_user(user, _do_probe_move, srcs, dst)
    err = result.get("error")
    if err:
        if err["kind"] == "self_nest":
            raise FsError(f"不能把目录移动到它自己内部：{err['name']}", 400)
        raise FsError(f"目标目录中已存在同名文件：{err['name']}", 409)
    if not _contains(roots, result["realpath_dst"]):
        raise FsError("目标经符号链接解析后超出允许范围", 403)
    return result


def move_paths(
    user: str, paths: List[str], dst_dir: str, roots: List[str],
    progress_cb=None,
) -> Dict:
    """把 paths 移动到 dst_dir。返回 {moved: [目标路径...]}。

    progress_cb(done, total, name) 供异步任务上报进度；同步调用可不传。
    逐个移动而非整体事务：部分失败时已完成的保持已完成，错误如实上报，
    比回滚更符合"移动文件"的直觉（用户重试只会补上剩下的）。
    """
    probe = probe_move(user, paths, dst_dir, roots)
    items = probe["items"]
    moved, failed = [], []
    for i, it in enumerate(items):
        name = os.path.basename(it["src"])
        if progress_cb is not None:
            progress_cb(i, len(items), name)
        try:
            call_as_user(user, _do_move_one, it["src"], it["dst"])
            moved.append(it["dst"])
        except Exception as e:  # noqa: BLE001
            failed.append({"path": it["src"], "error": str(e)[:200]})
    if progress_cb is not None:
        progress_cb(len(items), len(items), "")
    return {"moved": moved, "failed": failed, "cross_device": probe["cross_device"]}


def write_file(
    user: str, parent: str, filename: str, data: bytes, roots: List[str],
    replace: bool = False
) -> Dict:
    """把上传的文件写入 parent 目录，默认不覆盖已有（`replace=True` 时原子替换）。

    filename 可含相对子路径（目录上传时保留层级，如 "sub/a.txt"）：逐段校验，
    禁止空段 / "." / ".." / 绝对路径；中间目录按目标用户身份自动创建。
    """
    parts = [p.strip() for p in (filename or "").replace("\\", "/").split("/")]
    parts = [p for p in parts if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise FsError("非法文件名", 400)
    rel = os.path.join(*parts)
    parent_norm = normalize_under_roots(parent, roots)
    target = os.path.join(parent_norm, rel)
    normalize_under_roots(target, roots)
    result = call_as_user(user, _do_write_file, target, data, replace)
    if not _contains(roots, result["realpath_parent"]):
        raise FsError("目标经符号链接解析后超出允许范围", 403)
    return {"path": target}
