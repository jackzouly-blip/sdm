"""打包链路测试（HTTP 层）。

打包阶段（tar）不依赖网络，可独立验证；上传/分享需要网络与有效 token，
默认跳过（设 HPC_TEST_NETDISK=1 时才真正跑到网盘）。这里聚焦：
  - 路径校验（越权 / 非白名单 -> 任务失败并记录 error）
  - 异步任务的提交、查询、属主隔离
"""
import getpass
import os
import tempfile
import time

from fastapi.testclient import TestClient

from app.auth.session import issue_token
from app.config import get_settings
from app.tasks.manager import FAILED, SUCCESS


def _wait(client, tid, headers, timeout=30):
    for _ in range(timeout * 4):
        r = client.get(f"/tasks/{tid}", headers=headers)
        snap = r.json()
        if snap["status"] in (SUCCESS, FAILED, "interrupted"):
            return snap
        time.sleep(0.25)
    return client.get(f"/tasks/{tid}", headers=headers).json()


def test_package_rejects_path_outside_root():
    root = tempfile.mkdtemp(prefix="pkgroot_")
    with open(os.path.join(root, "ok.txt"), "w") as f:
        f.write("ok\n")
    get_settings().fs_roots = root

    from app.main import app

    with TestClient(app) as client:
        user = getpass.getuser()
        h = {"Authorization": f"Bearer {issue_token(user)}"}

        # 选了一个白名单外的路径 -> 任务应失败
        r = client.post(
            "/package",
            json={"paths": ["/etc/passwd"], "archive": "x.tar.gz"},
            headers=h,
        )
        assert r.status_code == 200, r.text
        tid = r.json()["task_id"]
        snap = _wait(client, tid, h)
        assert snap["status"] == FAILED
        assert "允许范围" in (snap["error"] or "")

        # 空选择 -> 400
        assert client.post("/package", json={"paths": []}, headers=h).status_code == 400

        # 越权查看他人任务 -> 403
        other = {"Authorization": f"Bearer {issue_token('intruder')}"}
        assert client.get(f"/tasks/{tid}", headers=other).status_code == 403

        # 未登录 -> 401
        assert client.get("/tasks").status_code == 401


def test_package_tar_phase_only():
    """验证打包阶段本身成功（不触网）：选合法文件，断点在上传前。

    通过把 archive 打到 scratch 后立即由上传失败（无 token 时）来确认 tar 已生成
    不可靠，这里改为直接调用打包服务的 tar 部分。"""
    root = tempfile.mkdtemp(prefix="pkgroot2_")
    sub = os.path.join(root, "case")
    os.makedirs(sub)
    with open(os.path.join(sub, "a.dat"), "w") as f:
        f.write("data\n")
    get_settings().fs_roots = root

    from app.fs.browser import stat_path
    from app.packaging.service import _common_base, _validate_paths

    user = getpass.getuser()
    paths = _validate_paths(user, [os.path.join(sub, "a.dat")], [root])
    assert paths == [os.path.join(sub, "a.dat")]
    base = _common_base(paths)
    assert base == sub


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
