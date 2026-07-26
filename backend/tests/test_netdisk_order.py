"""结果文件上传顺序测试。

分享链接在首个文件落地后就建好，所以先传 d3plot 能让用户几分钟内拿到可用链接，
不必等几个 GB 的 h3d。此前按文件名排序，而 h3d 常以数字开头（2434.h3d），
反而排在所有 d3plot 之前。
"""
import os

from app.netdisk.autoshare import scan_result_files


def _touch(d, name, size=0):
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(b"\0" * size)
    return p


def test_h3d_uploaded_last_even_when_name_sorts_first(tmp_path):
    d = str(tmp_path)
    _touch(d, "2434.h3d", 300)      # 数字开头，按名称排序会排在最前
    _touch(d, "d3plot", 10)
    _touch(d, "d3plot01", 10)
    _touch(d, "d3plot02", 10)
    _touch(d, "binout0000", 10)
    _touch(d, "d3hsp", 10)

    names = [os.path.basename(p) for p in scan_result_files(d)]

    assert names == [
        "d3plot", "d3plot01", "d3plot02",  # d3plot 优先，且保持时序
        "binout0000",
        "d3hsp",
        "2434.h3d",                        # 最大的垫底
    ]


def test_multiple_h3d_smaller_first(tmp_path):
    d = str(tmp_path)
    _touch(d, "2434.h3d", 900)
    _touch(d, "case3_22.h3d", 100)
    _touch(d, "d3plot", 10)

    names = [os.path.basename(p) for p in scan_result_files(d)]

    assert names == ["d3plot", "case3_22.h3d", "2434.h3d"]


def test_d3plot_sequence_order_is_preserved(tmp_path):
    """d3plot 是时序状态，即使体积不同也必须按名称顺序传。"""
    d = str(tmp_path)
    _touch(d, "d3plot", 999)      # 首个反而最大
    _touch(d, "d3plot01", 10)
    _touch(d, "d3plot10", 10)
    _touch(d, "d3plot02", 10)

    names = [os.path.basename(p) for p in scan_result_files(d)]

    assert names == ["d3plot", "d3plot01", "d3plot02", "d3plot10"]


def test_non_result_files_are_excluded(tmp_path):
    d = str(tmp_path)
    _touch(d, "d3plot", 10)
    _touch(d, "input.k", 10)
    _touch(d, "messag", 10)

    names = [os.path.basename(p) for p in scan_result_files(d)]

    assert names == ["d3plot"]
