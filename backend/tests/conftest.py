"""测试公共夹具。

`patch_or_stub` 解决一个具体问题：`app.privilege.actas` 依赖 `fcntl`（Unix 专有），
凡是传递依赖它的模块（`fs.browser`、`submit.router`、`d3plot.service`…）在非 Unix
开发机上根本导入不了。

在 Linux（部署目标、CI）上它打桩**真实模块**，走的是与生产完全相同的导入路径；
只有导入失败时才退化为注入桩模块。因此不会掩盖真实的导入问题。
"""
import importlib
import sys
import types

import pytest


@pytest.fixture
def patch_or_stub(monkeypatch):
    """返回一个 (模块名, {属性: 替身}) 的打桩函数。"""

    def _apply(mod_name: str, attrs: dict) -> None:
        try:
            importlib.import_module(mod_name)
        except ImportError:
            stub = types.ModuleType(mod_name)
            for k, v in attrs.items():
                setattr(stub, k, v)
            stub.FsError = type("FsError", (Exception,), {"message": ""})
            monkeypatch.setitem(sys.modules, mod_name, stub)
            # 同时挂到父包上，否则测试里用点号路径 monkeypatch 会解析不到
            pkg_name, _, leaf = mod_name.rpartition(".")
            monkeypatch.setattr(importlib.import_module(pkg_name), leaf, stub,
                                raising=False)
            return
        for k, v in attrs.items():
            monkeypatch.setattr(f"{mod_name}.{k}", v)

    return _apply


@pytest.fixture
def restrict_admins(monkeypatch):
    """配置管理员白名单。

    admin_users 为空时 is_admin() 对所有人返回 True（本地联调语义），
    属主隔离类断言会变成假阴性——凡是测隔离的用例都必须先用这个夹具。
    """
    from app import config

    # 冒号分隔的列表（与生产同格式）。含各测试文件用作管理员的身份。
    monkeypatch.setenv("HPC_ADMIN_USERS", "root:3dixadmin")
    monkeypatch.setattr(config, "_settings", None)
    yield
    monkeypatch.setattr(config, "_settings", None)
