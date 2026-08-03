"""跨目录移动：白名单、防穿越、防覆盖、防自嵌套。

这个能力是"把网盘同步下来的文件挪进工作目录"的最后一步，操作的是用户真实
数据，所以每条拒绝路径都值得单独钉住——移动出错的代价是数据跑到不该去的地方，
或者静默覆盖掉同名文件。

在临时目录里造沙盒作为 fs_root，当前用户即"登录用户"（act-as-user 走自身
直通分支，无需 root）。
"""
import getpass
import os

import pytest

# 以当前用户作为"登录用户"：真实 call_as_user 走 _is_self 直通分支，无需 root。
# 不能用任意假用户名——全量跑时 app.privilege.actas 可能已被别的测试导入，
# patch_or_stub 便打不进 browser 已绑定的 call_as_user，会落到真实 resolve_user。
ME = getpass.getuser()


@pytest.fixture
def fsb(patch_or_stub):
    """导入 fs.browser。

    它传递依赖 actas（fcntl，Unix 专有）。Linux 上 patch_or_stub 打的是真实模块，
    走的是与生产完全相同的导入路径（call_as_user 的 _is_self 直通分支）；
    非 Unix 开发机上模块导不进来，才退化为下面这组桩。两边跑同一套移动逻辑。
    """
    patch_or_stub("app.privilege.actas", {
        "call_as_user": lambda user, fn, *a, **k: fn(*a, **k),
        "run_as_user": lambda *a, **k: None,
        "stream_file_as_user": lambda *a, **k: iter(()),
        "stream_tar_as_user": lambda *a, **k: iter(()),
        "write_stream_as_user": lambda *a, **k: 0,
    })
    from app.fs import browser

    return browser


@pytest.fixture
def sandbox(tmp_path):
    """root/
         inbox/model.k, inbox/mesh/(dir)
         work/
    """
    root = tmp_path / "root"
    (root / "inbox" / "mesh").mkdir(parents=True)
    (root / "work").mkdir()
    (root / "inbox" / "model.k").write_text("keyword deck\n")
    (root / "inbox" / "mesh" / "part.k").write_text("mesh\n")
    (root / "work" / "model.k").write_text("已有的同名文件\n")
    return root


def _p(*parts) -> str:
    return os.path.join(*[str(x) for x in parts])


# --- 正常路径 -----------------------------------------------------------

def test_move_file_into_another_dir(fsb, sandbox):
    roots = [str(sandbox)]
    src = _p(sandbox, "inbox", "model.k")
    dst_dir = _p(sandbox, "inbox", "mesh")
    r = fsb.move_paths(ME, [src], dst_dir, roots)

    assert r["failed"] == []
    assert r["moved"] == [_p(dst_dir, "model.k")]
    assert not os.path.exists(src)
    assert open(_p(dst_dir, "model.k")).read() == "keyword deck\n"


def test_move_directory_with_contents(fsb, sandbox):
    """移动目录要连内容一起走，不能只挪个空壳。"""
    roots = [str(sandbox)]
    src = _p(sandbox, "inbox", "mesh")
    r = fsb.move_paths(ME, [src], _p(sandbox, "work"), roots)

    assert r["failed"] == []
    assert not os.path.exists(src)
    assert open(_p(sandbox, "work", "mesh", "part.k")).read() == "mesh\n"


def test_move_multiple_at_once(fsb, sandbox):
    roots = [str(sandbox)]
    srcs = [_p(sandbox, "inbox", "model.k"), _p(sandbox, "inbox", "mesh")]
    r = fsb.move_paths(ME, srcs, _p(sandbox, "work", ".."), roots)
    # dst 归一化到 sandbox 根本身
    assert len(r["moved"]) == 2
    assert os.path.exists(_p(sandbox, "model.k"))
    assert os.path.exists(_p(sandbox, "mesh", "part.k"))


def test_same_filesystem_is_not_cross_device(fsb, sandbox):
    """同一挂载点内应判为非跨设备——否则会白白走异步复制路径。"""
    roots = [str(sandbox)]
    probe = fsb.probe_move(ME, [_p(sandbox, "inbox", "model.k")],
                           _p(sandbox, "inbox", "mesh"), roots)
    assert probe["cross_device"] is False
    assert len(probe["items"]) == 1


def test_probe_does_not_move_anything(fsb, sandbox):
    """勘察必须是只读的——路由要靠它决定同步还是异步，不能有副作用。"""
    roots = [str(sandbox)]
    src = _p(sandbox, "inbox", "model.k")
    fsb.probe_move(ME, [src], _p(sandbox, "work", "..", "inbox", "mesh"), roots)
    assert os.path.exists(src)


# --- 拒绝路径 -----------------------------------------------------------

def test_reject_destination_outside_roots(fsb, sandbox, tmp_path):
    """目标在白名单外：这是把用户数据搬出受管范围，必须拒。"""
    roots = [str(sandbox)]
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(fsb.FsError) as e:
        fsb.move_paths(ME, [_p(sandbox, "inbox", "model.k")], str(outside), roots)
    assert e.value.status == 403


def test_reject_source_outside_roots(fsb, sandbox, tmp_path):
    roots = [str(sandbox)]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.k").write_text("x")
    with pytest.raises(fsb.FsError):
        fsb.move_paths(ME, [str(outside / "x.k")], _p(sandbox, "work"), roots)


def test_reject_traversal_in_destination(fsb, sandbox):
    roots = [str(sandbox)]
    with pytest.raises(fsb.FsError):
        fsb.move_paths(ME, [_p(sandbox, "inbox", "model.k")],
                       _p(sandbox, "..", "..", "etc"), roots)


def test_reject_overwrite_existing(fsb, sandbox):
    """目标已有同名文件：静默覆盖就是数据丢失，必须拒并说清是哪个文件。"""
    roots = [str(sandbox)]
    with pytest.raises(fsb.FsError) as e:
        fsb.move_paths(ME, [_p(sandbox, "inbox", "model.k")],
                       _p(sandbox, "work"), roots)
    assert e.value.status == 409
    assert "model.k" in e.value.message
    # 源与目标都原样保留
    assert open(_p(sandbox, "inbox", "model.k")).read() == "keyword deck\n"
    assert open(_p(sandbox, "work", "model.k")).read() == "已有的同名文件\n"


def test_reject_moving_dir_into_itself(fsb, sandbox):
    """把目录移进自己的子孙里会造出无法访问的自嵌套结构。"""
    roots = [str(sandbox)]
    with pytest.raises(fsb.FsError) as e:
        fsb.move_paths(ME, [_p(sandbox, "inbox")], _p(sandbox, "inbox", "mesh"),
                       roots)
    assert "自己内部" in e.value.message


def test_reject_moving_root_itself(fsb, sandbox):
    roots = [str(sandbox)]
    with pytest.raises(fsb.FsError) as e:
        fsb.move_paths(ME, [str(sandbox)], _p(sandbox, "work"), roots)
    assert "根目录" in e.value.message


def test_reject_move_into_current_parent(fsb, sandbox):
    """已经在目标目录里了——多半是误操作，明确告诉用户比默默成功好。"""
    roots = [str(sandbox)]
    with pytest.raises(fsb.FsError) as e:
        fsb.move_paths(ME, [_p(sandbox, "inbox", "model.k")],
                       _p(sandbox, "inbox"), roots)
    assert "已在目标目录" in e.value.message


def test_reject_empty_selection(fsb, sandbox):
    with pytest.raises(fsb.FsError):
        fsb.move_paths(ME, [], _p(sandbox, "work"), [str(sandbox)])


def test_missing_source_reports_not_found(fsb, sandbox):
    roots = [str(sandbox)]
    with pytest.raises(FileNotFoundError):
        fsb.move_paths(ME, [_p(sandbox, "inbox", "nope.k")],
                       _p(sandbox, "work"), roots)


def test_nothing_moves_when_one_item_is_rejected(fsb, sandbox):
    """勘察是整体的：一项不合法则整批不动，避免移一半的中间态。"""
    roots = [str(sandbox)]
    srcs = [_p(sandbox, "inbox", "mesh"), _p(sandbox, "inbox", "model.k")]
    with pytest.raises(fsb.FsError):
        fsb.move_paths(ME, srcs, _p(sandbox, "work"), roots)  # model.k 撞名
    assert os.path.exists(_p(sandbox, "inbox", "mesh"))
    assert os.path.exists(_p(sandbox, "inbox", "model.k"))
