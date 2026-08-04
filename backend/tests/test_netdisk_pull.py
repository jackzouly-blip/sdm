"""网盘入站同步：分享链接解析、errno 分类、文件名净化、落点派生。

这些都是不碰网络的纯逻辑，但每一条都对应一个真实故障模式：
链接形态多样、cookie 失效要能自诊断、网盘侧文件名不可信、落点必须落在白名单内。
"""
import pytest

from app import config
from app.netdisk.share_client import ShareError, is_dir, parse_surl


# --- isdir 类型归一 -----------------------------------------------------

def test_is_dir_handles_string_and_int():
    """share/list 的 isdir 类型不一致：根目录给字符串,子目录给整数。

    实测（2026-08-03）：root=1 时返回 {"isdir": "1", "size": "0"}，
    dir=... 时返回 {"isdir": 1, "size": 0}。字符串 "0" 在 Python 是真值，
    直接取真值会把根目录下的**文件**误判成目录——递归进去列不到东西，
    文件永远同步不到且不报错。
    """
    assert is_dir({"isdir": "1"}) is True
    assert is_dir({"isdir": 1}) is True
    assert is_dir({"isdir": "0"}) is False   # 曾经的 bug：字符串 "0" 是真值
    assert is_dir({"isdir": 0}) is False
    assert is_dir({}) is False               # 字段缺失按文件处理
    assert is_dir({"isdir": ""}) is False


# --- 分享链接解析 -------------------------------------------------------

@pytest.mark.parametrize(
    "url, expect",
    [
        ("https://pan.baidu.com/s/1AbC-dEf", "AbC-dEf"),
        ("https://pan.baidu.com/s/1AbC-dEf?pwd=x1y2", "AbC-dEf"),
        ("https://pan.baidu.com/share/init?surl=AbC-dEf", "AbC-dEf"),
        ("pan.baidu.com/s/1a_b-C9", "a_b-C9"),
    ],
)
def test_parse_surl_forms(url, expect):
    """客户粘过来的链接形态五花八门，短链/带提取码/init 页都要认。"""
    assert parse_surl(url) == expect


def test_parse_surl_rejects_garbage():
    with pytest.raises(ValueError):
        parse_surl("https://example.com/not-a-share")


# --- errno 分类：决定失败后是重试、告警运维、还是让用户重交链接 ---------

@pytest.mark.parametrize("errno", [-9, -12, -21, 105])
def test_link_invalid_errnos(errno):
    """链接侧问题 → 前端提示用户重新提交，不该惊动运维。"""
    e = ShareError("share/verify", errno, {})
    assert e.is_link_invalid
    assert not e.is_auth_failure


def test_auth_failure_errno():
    """cookie 失效 → 全局性故障，必须告警运维，用户自助无解。"""
    e = ShareError("share/list", -6, {})
    assert e.is_auth_failure
    assert not e.is_link_invalid


@pytest.mark.parametrize("errno", [-32, -33, 12])
def test_quota_errnos(errno):
    """配额触顶 → 减小批量后退避重试，不是永久失败。"""
    assert ShareError("share/transfer", errno, {}).is_quota_exceeded


def test_errno_zero_is_not_any_failure():
    e = ShareError("x", 0, {})
    assert not (e.is_link_invalid or e.is_auth_failure or e.is_quota_exceeded)


def test_share_error_keeps_payload():
    """原始返回必须留在异常里——私有接口漂移时这是唯一的线索。"""
    e = ShareError("share/list", -9, {"errno": -9, "show_msg": "提取码错误"})
    assert e.payload["show_msg"] == "提取码错误"
    assert "-9" in str(e)


# --- 网盘侧文件名净化：防路径穿越 ---------------------------------------

@pytest.fixture
def dl(patch_or_stub):
    """导入 download 模块。

    它传递依赖 actas（fcntl，Unix 专有）；在 Linux 上 patch_or_stub 打的是真实模块，
    非 Unix 开发机上才退化为桩，故这些纯逻辑用例两边都能跑。
    """
    patch_or_stub("app.privilege.actas", {
        "call_as_user": lambda *a, **k: None,
        "stream_file_as_user": lambda *a, **k: iter(()),
        "write_stream_as_user": lambda *a, **k: 0,
    })
    from app.netdisk import download

    return download


@pytest.mark.parametrize(
    "raw, banned",
    [
        ("../../etc/passwd", "/"),
        ("..\\..\\windows\\system32", "\\"),
        ("a/b/c.k", "/"),
    ],
)
def test_safe_filename_strips_separators(dl, raw, banned):
    """客户能在网盘里把文件命名成任意字符串，落盘前必须削平。"""
    out = dl._safe_filename(raw)
    assert banned not in out
    assert not out.startswith(".")


def test_safe_filename_never_empty(dl):
    assert dl._safe_filename("") == "unnamed"
    assert dl._safe_filename("...") == "unnamed"


def test_safe_filename_keeps_normal_names(dl):
    """净化不能把正常的 CAE 文件名改坏，否则用户对不上号。"""
    for name in ("model.k", "d3plot01", "结果-2024.h3d", "binout0000"):
        assert dl._safe_filename(name) == name


# --- dlink 拼接：踩过的坑不能再踩 ---------------------------------------

def test_is_real_md5_rejects_baidu_obfuscated(dl):
    """百度 share/list 返回的 md5 是混淆串，含非十六进制字符。

    实测（2026-08-03）拿到 `cbd02d4d0vd262cd69aa1cd072c3ac38` 这类值。若不判别就
    直接比对，校验会把**每一个**文件都判成失败——功能整体不可用。
    """
    assert dl.is_real_md5("cbd02d4d09d262cd69aa1cd072c3ac38") is True
    assert dl.is_real_md5("CBD02D4D09D262CD69AA1CD072C3AC38") is True
    # 真实踩到的混淆串：含 v / t / r / i / m
    for bad in (
        "cbd02d4d0vd262cd69aa1cd072c3ac38",
        "abf5223fatfe682e95736c945297079b",
        "909ea04a5r453db1d95223bc91d9b82f",
        "6545342a1i03a1eb3c4b66a7e37114ac",
        "fd527ec46m0ceff1c9d293d860e9fa7d",
    ):
        assert dl.is_real_md5(bad) is False, bad
    assert dl.is_real_md5("") is False
    assert dl.is_real_md5("abc") is False            # 长度不足
    assert dl.is_real_md5("0" * 33) is False         # 长度超出


def test_dl_url_appends_token_manually(dl):
    """dlink 自带已签名 query，token 只能手工拼——走 params 会破坏签名(31023)。"""
    assert dl._dl_url("https://d.pcs.baidu.com/file?sign=abc", "TK") == \
        "https://d.pcs.baidu.com/file?sign=abc&access_token=TK"
    assert dl._dl_url("https://d.pcs.baidu.com/file", "TK") == \
        "https://d.pcs.baidu.com/file?access_token=TK"


# --- 集群落点派生：必须落在 fs_roots 白名单内 ---------------------------

@pytest.fixture
def fresh_settings(monkeypatch):
    monkeypatch.setattr(config, "_settings", None)
    yield
    monkeypatch.setattr(config, "_settings", None)


def test_inbox_derives_from_first_root(monkeypatch, fresh_settings):
    """留空时派生到白名单第一个根下，保证写入不会被 403 拒掉。"""
    monkeypatch.setenv("HPC_FS_ROOTS", "/data:/caedata")
    monkeypatch.delenv("HPC_NETDISK_INBOX_ROOT", raising=False)
    s = config.get_settings()
    assert s.netdisk_inbox_base_dir == "/data/hpc-portal/netdisk-inbox"
    assert s.netdisk_inbox_base_dir.startswith(s.fs_root_list[0])


def test_inbox_explicit_config_wins(monkeypatch, fresh_settings):
    monkeypatch.setenv("HPC_FS_ROOTS", "/data")
    monkeypatch.setenv("HPC_NETDISK_INBOX_ROOT", "/data/inbox/")
    assert config.get_settings().netdisk_inbox_base_dir == "/data/inbox"


def test_pull_ready_requires_bduss(monkeypatch, fresh_settings):
    """没有 BDUSS 就没有转存能力，整个入站功能应当自认不可用。"""
    monkeypatch.delenv("HPC_NETDISK_BDUSS", raising=False)
    assert config.get_settings().netdisk_pull_ready is False
    monkeypatch.setattr(config, "_settings", None)
    monkeypatch.setenv("HPC_NETDISK_BDUSS", "x")
    assert config.get_settings().netdisk_pull_ready is True


# --- 同步库：增量判据与并发占位 -----------------------------------------

@pytest.fixture
def db(tmp_path):
    from app.netdisk.sync_db import NetdiskSyncDB

    d = NetdiskSyncDB(str(tmp_path / "netdisk_sync.db"))
    yield d
    d.close()


@pytest.fixture
def share(db):
    return db.create("alice", {
        "name": "项目A输入", "share_url": "https://pan.baidu.com/s/1abc",
        "pwd": "a1b2", "local_dir": "/data/inbox/alice/a", "poll_interval": 600,
    })


def _f(fs_id, name="model.k", size=100, md5="m"):
    return {"fs_id": fs_id, "filename": name, "share_path": f"/{name}",
            "size": size, "md5": md5}


def test_add_seen_returns_only_new(db, share):
    """add_seen 的返回值就是"用户这轮新加了几个"——增量同步的核心判据。"""
    sid = share["id"]
    assert db.add_seen(sid, [_f("1"), _f("2", "b.k")]) == 2
    # 同一批重复提交：一个都不算新增
    assert db.add_seen(sid, [_f("1"), _f("2", "b.k")]) == 0
    # 客户新加了一个
    assert db.add_seen(sid, [_f("1"), _f("2", "b.k"), _f("3", "c.k")]) == 1


def test_add_seen_preserves_huge_fs_id(db, share):
    """百度 fs_id 超出 JS 安全整数范围，必须原样保真——否则增量判据会错乱。"""
    sid = share["id"]
    huge = "1043260090845632"          # > 2^53 的真实量级
    neighbor = "1043260090845633"      # 相邻值：若被浮点化会与上面相撞
    assert db.add_seen(sid, [_f(huge), _f(neighbor, "b.k")]) == 2
    ids = db.known_fs_ids(sid)
    assert huge in ids and neighbor in ids
    assert db.add_seen(sid, [_f(huge)]) == 0  # 仍能精确命中


def test_add_seen_does_not_reset_state(db, share):
    """已完成的文件在下一轮 list 时会再次出现，不能被打回 seen 重下一遍。"""
    sid = share["id"]
    db.add_seen(sid, [_f("1")])
    db.mark(sid, ["1"], state="done", local_path="/data/x/model.k")
    db.add_seen(sid, [_f("1")])
    assert db.list_files(sid)[0]["state"] == "done"


def test_pending_includes_failed_for_retry(db, share):
    """失败的文件必须自动进入下一轮——否则用户得手工挑出来重试。"""
    sid = share["id"]
    db.add_seen(sid, [_f("1"), _f("2", "b.k"), _f("3", "c.k")])
    db.mark(sid, ["1"], state="done")
    db.mark(sid, ["2"], state="failed", error="网络中断")
    pending = {r["fs_id"] for r in db.pending_files(sid)}
    assert pending == {"2", "3"}  # done 的不再重来，failed 的自动重试


def test_try_begin_sync_is_exclusive(db, share):
    """手动按钮与定时轮询并发时只能有一个进去，否则会重复转存。"""
    sid = share["id"]
    assert db.try_begin_sync(sid) is True
    assert db.try_begin_sync(sid) is False
    db.end_sync(sid, "done", link_state="ok")
    assert db.try_begin_sync(sid) is True


def test_reset_stuck_frees_claims(db, share):
    """进程被杀后残留的占位若不清，该源将永远无法再同步。"""
    sid = share["id"]
    db.try_begin_sync(sid)
    assert db.reset_stuck() == 1
    assert db.try_begin_sync(sid) is True


def test_due_shares_respects_policy(db, share):
    """到期判定：停用、仅手动、正在同步的源都不该被定时轮询挑中。

    注意时间基准：end_sync 写入的是真实 time.time()，所以"未来"必须以真实时间
    为基准推算，不能混用假时间戳。
    """
    import time as _t

    sid = share["id"]
    assert [r["id"] for r in db.due_shares()] == [sid]  # last_poll_at=0，早已到期

    db.end_sync(sid, "done", link_state="ok")           # 刚轮询过
    assert db.due_shares() == []

    later = _t.time() + 99999                            # 间隔早已过去
    db.update(sid, {"poll_interval": 0})                 # 仅手动
    assert db.due_shares(later) == []

    db.update(sid, {"poll_interval": 600, "enabled": False})  # 已停用
    assert db.due_shares(later) == []

    db.update(sid, {"enabled": True})
    assert [r["id"] for r in db.due_shares(later)] == [sid]

    db.try_begin_sync(sid)                               # 正在同步
    assert db.due_shares(later) == []


def test_counts_and_batches(db, share):
    """列表页的计数与中转区批次汇总。"""
    sid = share["id"]
    db.add_seen(sid, [_f("1", size=10), _f("2", "b.k", size=20), _f("3", "c.k", size=30)])
    db.mark(sid, ["1", "2"], state="done", batch_id="20260802-100000")
    db.mark(sid, ["3"], state="failed", batch_id="20260802-100000")
    assert db.counts(sid) == {"done": 2, "failed": 1}
    b = db.batches(sid)[0]
    assert (b["batch_id"], b["files"], b["bytes"], b["done"]) == \
        ("20260802-100000", 3, 60, 2)


def test_delete_share_removes_its_files(db, share):
    sid = share["id"]
    db.add_seen(sid, [_f("1")])
    assert db.delete(sid) is True
    assert db.list_files(sid) == []


def test_owner_scoping(db, share):
    db.create("bob", {"name": "b", "share_url": "u", "local_dir": "/data/b"})
    assert [r["owner"] for r in db.list_by_owner("alice")] == ["alice"]
    assert len(db.list_all()) == 2


# --- 平台凭据存储 -------------------------------------------------------

def test_credentials_roundtrip(db):
    assert db.get_credentials() is None
    db.set_credentials("BD123", "ST456", "root")
    row = db.get_credentials()
    assert (row["bduss"], row["stoken"], row["updated_by"]) == ("BD123", "ST456", "root")
    # 单行表：再写一次是更新而不是插入第二行
    db.set_credentials("BD999", "", "admin2")
    assert db.get_credentials()["bduss"] == "BD999"
    assert db.get_credentials()["stoken"] == ""


def test_credentials_status_never_leaks_secrets(db):
    """状态是要发给前端的——凭据本身绝不能出现在里面。"""
    db.set_credentials("SUPER_SECRET_BDUSS", "SUPER_SECRET_STOKEN", "root")
    st = db.credentials_status()
    blob = repr(st)
    assert "SUPER_SECRET_BDUSS" not in blob
    assert "SUPER_SECRET_STOKEN" not in blob
    assert st["configured"] is True
    assert st["has_stoken"] is True
    assert st["source"] == "db"


def test_credentials_db_overrides_env(db, monkeypatch, fresh_settings):
    """库里配了就以库为准；env 只是首次引导/旧部署的回退。"""
    monkeypatch.setenv("HPC_NETDISK_BDUSS", "FROM_ENV")
    monkeypatch.setenv("HPC_NETDISK_STOKEN", "ENV_ST")
    assert db.resolve_credentials() == ("FROM_ENV", "ENV_ST")
    assert db.credentials_status()["source"] == "env"

    db.set_credentials("FROM_DB", "DB_ST", "root")
    assert db.resolve_credentials() == ("FROM_DB", "DB_ST")
    assert db.credentials_status()["source"] == "db"


def test_clearing_credentials_falls_back_to_env(db, monkeypatch, fresh_settings):
    monkeypatch.setenv("HPC_NETDISK_BDUSS", "FROM_ENV")
    db.set_credentials("FROM_DB", "", "root")
    db.clear_credentials("root")
    assert db.resolve_credentials()[0] == "FROM_ENV"


def test_no_credentials_anywhere_is_not_configured(db, monkeypatch, fresh_settings):
    monkeypatch.delenv("HPC_NETDISK_BDUSS", raising=False)
    monkeypatch.delenv("HPC_NETDISK_STOKEN", raising=False)
    st = db.credentials_status()
    assert st["configured"] is False
    assert st["source"] == "none"
    assert db.resolve_credentials() == ("", "")


def test_credential_state_transitions(db):
    """同步失败要能把状态打成 auth_failed，恢复后又能自动回 ok。"""
    db.set_credentials("BD", "ST", "root")
    assert db.credentials_status()["state"] == "unknown"  # 刚写入，未验证

    db.mark_credential_state("auth_failed")
    assert db.credentials_status()["state"] == "auth_failed"

    db.mark_credential_state("ok", "测试账号")
    st = db.credentials_status()
    assert st["state"] == "ok"
    assert st["account"] == "测试账号"
    assert st["last_checked_at"] > 0


def test_rewriting_credentials_resets_state(db):
    """换了新 cookie 就不该再顶着旧的 auth_failed 红标。"""
    db.set_credentials("BD", "", "root")
    db.mark_credential_state("auth_failed", "旧账号")
    db.set_credentials("BD_NEW", "", "root")
    st = db.credentials_status()
    assert st["state"] == "unknown"
    assert st["account"] == ""


# --- 落点派生 -----------------------------------------------------------

def test_local_dir_for_sanitizes_name(dl, monkeypatch, fresh_settings):
    """源名由用户自由填写，派生落点时必须削平，否则可逃出 inbox 根。"""
    monkeypatch.setenv("HPC_FS_ROOTS", "/data")
    monkeypatch.delenv("HPC_NETDISK_INBOX_ROOT", raising=False)
    from app.netdisk.puller import local_dir_for

    assert local_dir_for("alice", "项目A") == \
        "/data/hpc-portal/netdisk-inbox/alice/项目A"
    # 分隔符被削平、前导点被剥掉：剩下的 ".." 只是普通字符，不构成路径段
    got = local_dir_for("alice", "../../etc")
    assert got == "/data/hpc-portal/netdisk-inbox/alice/_.._etc"
    assert got.startswith("/data/hpc-portal/netdisk-inbox/alice/")


def test_safe_relpath_keeps_structure_but_blocks_traversal(dl):
    """逐段净化：既要保住层级，又不能让 `..` 逃出落点。"""
    assert dl.safe_relpath("Model/sub") == "Model/sub"
    assert dl.safe_relpath("/Model/sub/") == "Model/sub"
    assert dl.safe_relpath("") == ""
    # 穿越段被丢弃，其余层级保留
    assert dl.safe_relpath("../../etc") == "etc"
    assert dl.safe_relpath("a/../../b") == "a/b"
    assert dl.safe_relpath("a/./b") == "a/b"
    # 段内的分隔符与控制字符被净化，但不会把整串压平
    assert "/" in dl.safe_relpath("Model/sub")
    assert dl.safe_relpath(".hidden/x") == "hidden/x"


def test_rel_dir_strips_sub_dir_base(monkeypatch, fresh_settings):
    """相对目录以同步源的 sub_dir 为基准截断。"""
    from app.netdisk.puller import rel_dir_of

    sub = "/HPC/user07/ZXY/LEV05/try"
    assert rel_dir_of(f"{sub}/Model/main.key", sub) == "Model"
    assert rel_dir_of(f"{sub}/main.key", sub) == ""
    assert rel_dir_of(f"{sub}/a/b/c.k", sub) == "a/b"
    # sub_dir 为空 = 同步整个分享，相对目录即分享内的完整目录
    assert rel_dir_of("/HPC/user07/x.k", "") == "HPC/user07"
    # sub_dir 带尾斜杠也要能正确截断
    assert rel_dir_of(f"{sub}/Model/main.key", sub + "/") == "Model"


def test_same_name_in_different_dirs_do_not_collide(monkeypatch, fresh_settings):
    """不同子目录下的同名文件必须落到不同路径。

    这正是压平时会静默互相覆盖的场景（落盘走 os.replace，不报错）；
    CAE 的 deck 又常靠相对路径 *INCLUDE，压平即失效。
    """
    from app.netdisk.puller import rel_dir_of

    sub = "/share/try"
    a = rel_dir_of(f"{sub}/case5/main.key", sub)
    b = rel_dir_of(f"{sub}/case6/main.key", sub)
    assert a == "case5" and b == "case6"
    assert a != b


def test_remote_batch_dir_isolates_batches(monkeypatch, fresh_settings):
    """批次目录隔离是"同名不同版本"的解药，路径必须逐批不同。"""
    monkeypatch.setenv("HPC_NETDISK_INBOX_REMOTE", "/apps/HPC/inbox")
    from app.netdisk.puller import remote_batch_dir

    a = remote_batch_dir("alice", 7, "20260802-100000")
    b = remote_batch_dir("alice", 7, "20260802-110000")
    assert a == "/apps/HPC/inbox/alice/7/20260802-100000"
    assert a != b
