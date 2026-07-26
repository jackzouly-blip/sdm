"""网盘上传的分片上限与断点续传前提测试。

不触网：用假 client 替掉百度接口，只验证两处纯本地判断——
  - 分片数超过百度 uploadid 上限（MAX_PARTS=2048）时，上传前就报错，
    而不是先老实传满 2048 片再对剩余每片各撞一次 400
  - chunk_size 变了就不能复用旧 uploadid 续传，否则新 block_list 会配上旧
    uploadid，合并必然对不上
"""
import hashlib
import json

import httpx
import pytest

from baidu_uploader.api.client import BaiduHttpError, BaiduPanClient
from baidu_uploader.core.uploader import MAX_PARTS, Uploader, UploadTooLargeError
from baidu_uploader.state.store import StateStore


class FakeClient:
    """记录调用的假 xpan client，行为按“全部分片都待传”返回。"""

    def __init__(self):
        self.parts = []
        self.created = False

    def precreate(self, remote_path, size, block_list, rtype=3):
        return {"uploadid": "FAKE-UPLOADID", "block_list": list(range(len(block_list)))}

    def upload_part(self, remote_path, uploadid, partseq, chunk):
        self.parts.append(partseq)
        return {"md5": hashlib.md5(chunk).hexdigest()}

    def create(self, remote_path, size, block_list, uploadid, rtype=3):
        self.created = True
        return {"path": remote_path, "size": size, "fs_id": 1}


def _write(path, size):
    path.write_bytes(b"\0" * size)
    return path


def test_preflight_rejects_over_limit_before_any_upload(tmp_path):
    """超限文件必须在发出任何请求之前失败。

    用 1 字节分片构造 MAX_PARTS+1 片，避免真的造一个 8 GiB 文件。
    """
    local = _write(tmp_path / "huge.h3d", MAX_PARTS + 1)
    client = FakeClient()
    up = Uploader(client, chunk_size=1, concurrency=1)

    with pytest.raises(UploadTooLargeError) as ei:
        up.upload_file(local, "/apps/HPC/test/huge.h3d")

    assert ei.value.parts == MAX_PARTS + 1
    assert client.parts == [], "超限时不应发出任何分片请求"
    assert not client.created


def test_preflight_error_message_is_actionable(tmp_path):
    local = _write(tmp_path / "huge.h3d", MAX_PARTS + 1)
    up = Uploader(FakeClient(), chunk_size=1, concurrency=1)

    with pytest.raises(UploadTooLargeError) as ei:
        up.upload_file(local, "/apps/HPC/test/huge.h3d")

    msg = str(ei.value)
    assert str(MAX_PARTS) in msg      # 说清上限是多少
    assert "chunk_size" in msg        # 说清该调哪个配置


def test_exactly_at_limit_still_uploads(tmp_path):
    """边界：正好 MAX_PARTS 片必须放行，不能把合法文件误杀。"""
    local = _write(tmp_path / "edge.h3d", MAX_PARTS)
    client = FakeClient()
    up = Uploader(client, chunk_size=1, concurrency=4)

    res = up.upload_file(local, "/apps/HPC/test/edge.h3d")

    assert client.created
    assert sorted(client.parts) == list(range(MAX_PARTS))
    assert res["fs_id"] == 1


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.url = "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2?x=1"

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _ScriptedHttp:
    """按脚本依次返回响应的假 httpx client。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def post(self, *a, **kw):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def no_sleep(monkeypatch):
    import baidu_uploader.api.client as mod

    monkeypatch.setattr(mod.time, "sleep", lambda s: None)


def test_upload_part_retries_transient_5xx(no_sleep):
    """偶发空体 500 必须就地重试，而不是让整个文件的上传失败。"""
    c = BaiduPanClient(lambda: "tok")
    c._client = _ScriptedHttp([
        _Resp(500, text=""),
        _Resp(500, text=""),
        _Resp(200, {"md5": "abc"}),
    ])

    assert c.upload_part("/p/f.h3d", "uid", 7, b"data")["md5"] == "abc"
    assert c._client.calls == 3


def test_upload_part_retries_transport_error(no_sleep):
    c = BaiduPanClient(lambda: "tok")
    c._client = _ScriptedHttp([
        httpx.ConnectError("connection reset"),
        _Resp(200, {"md5": "abc"}),
    ])

    assert c.upload_part("/p/f.h3d", "uid", 7, b"data")["md5"] == "abc"
    assert c._client.calls == 2


def test_upload_part_does_not_retry_4xx(no_sleep):
    """31299 这类确定性错误重试无意义，必须立即抛出。"""
    c = BaiduPanClient(lambda: "tok")
    c._client = _ScriptedHttp([
        _Resp(400, {"error_code": 31299, "error_msg": "Invalid param part_id"}),
        _Resp(200, {"md5": "abc"}),  # 不该被用到
    ])

    with pytest.raises(BaiduHttpError) as ei:
        c.upload_part("/p/f.h3d", "uid", 2048, b"data")
    assert ei.value.status == 400
    assert c._client.calls == 1, "4xx 不应重试"


def test_upload_part_gives_up_after_max_attempts(no_sleep):
    c = BaiduPanClient(lambda: "tok")
    c._client = _ScriptedHttp([_Resp(500, text="") for _ in range(4)])

    with pytest.raises(BaiduHttpError):
        c.upload_part("/p/f.h3d", "uid", 7, b"data")
    assert c._client.calls == 4


def test_resume_requires_same_chunk_size(tmp_path):
    """chunk_size 改变后，旧的未完成任务不得被复用（uploadid 必须作废）。"""
    store = StateStore(tmp_path / "state.db")
    remote = "/apps/HPC/backup/user07/case3/2329.h3d"
    md5 = hashlib.md5(b"payload").hexdigest()
    blocks_4m = ["a" * 32, "b" * 32]

    store.upsert_task(remote, "/data/2329.h3d", md5, 8 << 20, 1.0, 4 << 20, blocks_4m)
    store.set_uploadid(remote, "OLD-UPLOADID-4MIB")
    assert store.get(remote)["uploadid"] == "OLD-UPLOADID-4MIB"

    # 同一文件、同一分片大小 -> 保留 uploadid 续传
    row = store.upsert_task(
        remote, "/data/2329.h3d", md5, 8 << 20, 1.0, 4 << 20, blocks_4m
    )
    assert row["uploadid"] == "OLD-UPLOADID-4MIB"

    # 分片大小改成 32 MiB -> 必须重置，否则新 block_list 会配上旧 uploadid
    store.upsert_task(remote, "/data/2329.h3d", md5, 8 << 20, 1.0, 32 << 20, ["c" * 32])
    row = store.get(remote)
    assert row["uploadid"] is None
    assert row["chunk_size"] == 32 << 20
    assert json.loads(row["uploaded"]) == []
    store.close()
