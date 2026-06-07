"""文件浏览测试：白名单/防穿越 + 以自身身份列目录/预览/下载 + 符号链接逃逸。

在临时目录里造一个沙盒作为 fs_root，当前用户即"登录用户"（act-as-user 走
自身直通分支，无需 root）。
"""
import getpass
import os
import tempfile

from fastapi.testclient import TestClient

from app.auth.session import issue_token
from app.config import get_settings
from app.fs.browser import FsError, normalize_under_roots


def test_normalize_rejects_traversal():
    roots = ["/data"]
    assert normalize_under_roots("/data/user07/case2", roots) == "/data/user07/case2"
    # 穿越被消解后落在根外 -> 拒绝
    for bad in ["/data/../etc/passwd", "/etc/passwd", "/data/../../root"]:
        try:
            normalize_under_roots(bad, roots)
            assert False, f"应拒绝 {bad}"
        except FsError as e:
            assert e.status == 403 or "绝对" in e.message
    # 相对路径拒绝
    try:
        normalize_under_roots("data/x", roots)
        assert False
    except FsError:
        pass


def _build_sandbox():
    root = tempfile.mkdtemp(prefix="fsroot_")
    os.makedirs(os.path.join(root, "sub"))
    with open(os.path.join(root, "hello.txt"), "w") as f:
        f.write("hello hpc\n")
    with open(os.path.join(root, "sub", "data.log"), "w") as f:
        f.write("line1\nline2\n")
    # 指向根外的符号链接
    os.symlink("/etc", os.path.join(root, "escape"))
    return root


def test_fs_endpoints_as_self():
    root = _build_sandbox()
    # 把白名单指向沙盒
    get_settings().fs_roots = root
    from app.main import app

    with TestClient(app) as client:
        token = issue_token(getpass.getuser())
        h = {"Authorization": f"Bearer {token}"}

        # 列目录
        r = client.get("/fs/list", params={"path": root}, headers=h)
        assert r.status_code == 200, r.text
        names = {e["name"] for e in r.json()["entries"]}
        assert {"hello.txt", "sub", "escape"} <= names

        # 预览文本
        r = client.get(
            "/fs/preview", params={"path": os.path.join(root, "hello.txt")}, headers=h
        )
        assert r.status_code == 200
        assert r.json()["content"] == "hello hpc\n"

        # 下载
        r = client.get(
            "/fs/download",
            params={"path": os.path.join(root, "sub", "data.log")},
            headers=h,
        )
        assert r.status_code == 200
        assert r.content == b"line1\nline2\n"

        # 符号链接逃逸：list /etc 经 escape -> realpath=/etc 不在根内 -> 403
        r = client.get(
            "/fs/list", params={"path": os.path.join(root, "escape")}, headers=h
        )
        assert r.status_code == 403, r.text

        # 路径穿越 -> 403
        r = client.get("/fs/list", params={"path": "/etc"}, headers=h)
        assert r.status_code == 403

        # 未登录 -> 401
        assert client.get("/fs/list", params={"path": root}).status_code == 401


if __name__ == "__main__":
    import sys

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"ERROR {name}: {e!r}")
    sys.exit(1 if failed else 0)
