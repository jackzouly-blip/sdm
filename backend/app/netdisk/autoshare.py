"""任务结果自动上传百度网盘并生成分享链接。

流程：扫描任务 workdir 的结果文件（h3d / d3plot 家族 / binout 家族 / d3hsp）
→ 在网盘建立 `<remote_dir>/<owner>/<任务名>__<short_id>/` 目录并逐个上传
→ 对该目录生成带提取码的分享链接（有效期可配）
→ 写回 jobs 表（netdisk_state / 链接 / 提取码 / 过期时间 / 文件清单）。

复用 NetdiskEngine（OAuth/分片/断点续传/分享）。作为 TaskManager 异步任务运行。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional

from ..logger import get_logger
from ..tasks.manager import TaskHandle, register_task

log = get_logger(__name__)

# 说明：NetdiskEngine 依赖 baidu_uploader 及其第三方库，故采用惰性导入
# （在任务运行时才导入），避免该可选子系统的缺失在应用启动阶段拖垮整个门户。

# 结果文件匹配：h3d、d3plot 家族（d3plot/d3plot01/d3plotaa…）、binout 家族、d3hsp
_H3D = re.compile(r"\.h3d$", re.I)
_D3PLOT = re.compile(r"^d3plot(\d+|[a-z]+)?$", re.I)
_BINOUT = re.compile(r"^binout", re.I)
_D3HSP = re.compile(r"^d3hsp", re.I)


def _jobs_db():
    from ..extract.dispatcher import get_dispatcher

    d = get_dispatcher()
    return d.jobs_db if d else None


def maybe_auto_share(jobid: str) -> bool:
    """提取（后处理）完成后的自动触发钩子。

    仅当任务属主在 netdisk_auto_users 白名单内、且该任务网盘状态尚为 none 时，
    才原子占位并派发 netdisk_autoshare 任务。多条提取规则各自完成时都会调用，
    但靠 try_begin_netdisk 的 CAS 保证只触发一次。返回是否真正派发。

    本函数对异常零容忍地静默处理由调用方负责——这里只在确实满足条件时动作。
    """
    from ..config import get_settings
    from ..extract.dispatcher import get_dispatcher

    d = get_dispatcher()
    if d is None:
        return False
    jdb = d.jobs_db
    row = jdb.get(jobid)
    if row is None:
        return False
    owner = row["owner"]
    s = get_settings()
    if not s.netdisk_auto_enabled(owner):
        return False
    # 原子占位：none -> pending，多规则并发仅第一个成功
    if not jdb.try_begin_netdisk(jobid):
        return False
    d.tm.submit(
        "netdisk_autoshare",
        owner=owner,
        params={
            "jobid": jobid,
            "workdir": s.map_path(row["workdir"]),
            "owner": owner,
            "task_name": row["name"] or row["short_id"],
            "short_id": row["short_id"],
            "period": s.netdisk_share_period,
        },
    )
    log.info("自动网盘分享已派发: job=%s owner=%s", jobid, owner)
    return True


def retry_failed_uploads(
    attempts: dict,
    max_attempts: int = 3,
    backoff=(120, 600, 1800),
) -> int:
    """自动重试看门狗：把白名单用户处于 failed 的最终上传重新派发。

    复用与手动按钮完全相同的 netdisk_autoshare 任务路径——靠内容 MD5 去重，
    重试只补传未成功的文件（断点续传），通常一两次即完成。

    退避与封顶：每个任务自上次失败起按 backoff 秒退避，最多 max_attempts 次自动
    重试；超过则放弃、维持 failed，交由人工处理，避免对永久性故障无限重试。
    attempts: {jobid: 已自动重试次数}，由调用方（streamer）持有；任务恢复(非 failed)
    时自动清零。返回本轮新派发的重试任务数。
    """
    from ..config import get_settings
    from ..extract.dispatcher import get_dispatcher

    d = get_dispatcher()
    if d is None:
        return 0
    jdb = d.jobs_db
    s = get_settings()
    now = time.time()
    count = 0
    for owner in s.netdisk_auto_user_list:
        try:
            rows = jdb.list_by_owner(owner)  # 全状态，含已完成任务的最终上传失败
        except Exception:  # noqa: BLE001
            log.exception("看门狗列出 %s 任务失败", owner)
            continue
        for row in rows:
            jobid = row["jobid"]
            state = (row["netdisk_state"] if "netdisk_state" in row.keys() else None)
            if state != "failed":
                attempts.pop(jobid, None)  # 已恢复/非失败：清零计数
                continue
            n = attempts.get(jobid, 0)
            if n >= max_attempts:
                continue  # 已达上限，留给人工
            last = row["netdisk_updated"] or 0
            if now - last < backoff[min(n, len(backoff) - 1)]:
                continue  # 退避中
            if not jdb.try_retry_netdisk(jobid):  # 原子认领，避免与手动/并发重复
                continue
            attempts[jobid] = n + 1
            try:
                d.tm.submit(
                    "netdisk_autoshare",
                    owner=owner,
                    params={
                        "jobid": jobid,
                        "workdir": s.map_path(row["workdir"]),
                        "owner": owner,
                        "task_name": row["name"] or row["short_id"],
                        "short_id": row["short_id"],
                        "period": s.netdisk_share_period,
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception("看门狗派发重试失败 job=%s", jobid)
                continue
            log.info("自动重试网盘上传: job=%s owner=%s（第 %d/%d 次）",
                     jobid, owner, n + 1, max_attempts)
            count += 1
    return count


def _safe_name(name: str) -> str:
    """清理用于网盘目录名的字符串：去路径分隔符与不安全字符，限长。"""
    name = (name or "").strip().replace("/", "_").replace("\\", "_")
    name = re.sub(r'[\x00-\x1f<>:"|?*]+', "_", name)
    return (name or "task")[:120]


def _is_result_file(name: str) -> bool:
    return bool(
        _H3D.search(name) or _D3PLOT.match(name)
        or _BINOUT.match(name) or _D3HSP.match(name)
    )


def scan_result_entries(workdir: str) -> List[tuple]:
    """列出 workdir 顶层结果文件 (path, size)，按文件名排序。"""
    out: List[tuple] = []
    try:
        for e in os.scandir(workdir):
            if not e.is_file():
                continue
            if _is_result_file(e.name):
                try:
                    size = e.stat(follow_symlinks=False).st_size
                except OSError:
                    size = 0
                out.append((e.path, size))
    except OSError as ex:
        log.warning("扫描 workdir 失败 %s: %s", workdir, ex)
    out.sort(key=lambda t: os.path.basename(t[0]))
    return out


def scan_result_files(workdir: str) -> List[str]:
    """列出 workdir 下的结果文件路径（仅顶层，按名称排序）。"""
    return [p for p, _ in scan_result_entries(workdir)]


def scan_stable_d3plot(workdir: str, stable_seconds: int) -> List[str]:
    """列出 workdir 顶层、已“写完稳定”的 d3plot 文件路径。

    判定：最近 stable_seconds 秒内未再被写入（now - mtime >= stable_seconds）。
    这样正在写入的最新一个 d3plot（mtime 很新）会被自然排除，避免传到半截文件。
    """
    now = time.time()
    out: List[str] = []
    try:
        for e in os.scandir(workdir):
            if not e.is_file() or not _D3PLOT.match(e.name):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if now - st.st_mtime >= stable_seconds:
                out.append(e.path)
    except OSError as ex:
        log.warning("扫描稳定 d3plot 失败 %s: %s", workdir, ex)
    out.sort(key=lambda p: os.path.basename(p))
    return out


def scan_uploadable_d3plot(workdir: str, prev_sizes: dict) -> List[str]:
    """列出可入队上传的 d3plot：两次扫描尺寸不变，且排除当前活跃(最高序号)成员。

    判据（双保险）：
      1) 排除排序后最大的成员——它是正在写的活跃文件；只要存在更高序号成员，
         低序号成员就一定已 flush 完成（零误判，不依赖时间/mtime）。
      2) 仍要求 size 与上一轮相同且 >0，抵御共享文件系统 mtime 不可靠/罕见追加。

    prev_sizes: {abs_path: size} 上一轮快照，本函数就地更新（含活跃成员，
    以便它将来不再活跃时已有历史尺寸）。返回符合条件的绝对路径列表。
    """
    try:
        members = [
            e for e in os.scandir(workdir)
            if e.is_file() and _D3PLOT.match(e.name)
        ]
    except OSError as ex:
        log.warning("扫描 d3plot 失败 %s: %s", workdir, ex)
        return []
    if len(members) <= 1:
        # 只有活跃成员（或没有）：无可安全上传项，仅记录尺寸供下轮比对
        for e in members:
            try:
                prev_sizes[e.path] = e.stat(follow_symlinks=False).st_size
            except OSError:
                pass
        return []
    members.sort(key=lambda e: e.name)
    active = members[-1]  # 最高序号 = 正在写的活跃文件，排除
    eligible: List[str] = []
    for e in members[:-1]:
        try:
            size = e.stat(follow_symlinks=False).st_size
        except OSError:
            continue
        prev = prev_sizes.get(e.path)
        prev_sizes[e.path] = size
        if size > 0 and prev == size:
            eligible.append(e.path)
    try:
        prev_sizes[active.path] = active.stat(follow_symlinks=False).st_size
    except OSError:
        pass
    eligible.sort(key=lambda p: os.path.basename(p))
    return eligible


def _load_uploaded(row) -> List[str]:
    """从任务行解析已上传文件名列表（累计去重用）。"""
    if row is None:
        return []
    try:
        val = row["netdisk_files"]
    except (KeyError, IndexError):
        return []
    if not val:
        return []
    try:
        data = json.loads(val)
        return list(data) if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def run_upload(
    jobid: str,
    workdir: str,
    owner: str,
    task_name: str,
    short_id: str,
    period: int,
    *,
    files: Optional[List[str]] = None,
    finalize: bool = True,
    handle: Optional[TaskHandle] = None,
) -> Dict:
    """上传一组结果文件到网盘并（首次时）创建目录分享链接，写回任务状态。

    手动/最终上传与流式上传共用此核心：
      finalize=True ：一次性/最终补传。开始置 uploading，结束置 done；
                      files=None 时全量扫描结果文件；无文件则置 skipped。
                      所有状态写入为无条件（set_netdisk）。
      finalize=False：流式（任务运行中）。状态置/保持 partial；
                      写入走条件更新（set_netdisk_if_streamable），
                      一旦最终补传接管（pending/uploading/done）即自动让位，
                      绝不把 done 覆盖回 partial。
    分享链接“早建复用”：目录已存在链接则沿用，不重复创建。
    已上传文件靠 StateStore 内容 MD5 去重，重复调用只补新文件。
    """
    jdb = _jobs_db()
    row = jdb.get(jobid) if jdb else None

    # 根据 finalize 选择无条件/条件写入器
    def _set(**f):
        if not jdb:
            return
        if finalize:
            jdb.set_netdisk(jobid, **f)
        else:
            jdb.set_netdisk_if_streamable(jobid, **f)

    def _phase(text=None, pct=None):
        if handle is not None:
            handle.update(phase=text, progress=pct)

    running_state = "uploading" if finalize else "partial"
    _set(netdisk_state=running_state, netdisk_msg=None)
    _phase("扫描结果文件", 1)

    if not workdir or not os.path.isdir(workdir):
        if finalize:
            _set(netdisk_state="failed", netdisk_msg="任务工作目录不存在")
            raise RuntimeError(f"workdir 不存在: {workdir}")
        return {"skipped": True, "reason": "workdir missing"}

    paths = files if files is not None else scan_result_files(workdir)
    if not paths:
        if finalize:
            _set(netdisk_state="skipped",
                 netdisk_msg="未找到 h3d / d3plot / binout / d3hsp 结果文件")
            _phase("无可上传文件，跳过", 100)
            return {"skipped": True, "reason": "no result files"}
        return {"skipped": True, "reason": "no stable files"}

    dirname = _safe_name(f"{task_name}__{short_id}")
    uploaded = _load_uploaded(row)  # 累计已上传文件名
    existing_link = row["netdisk_share_url"] if row else None
    existing_pwd = row["netdisk_share_pwd"] if row else None
    existing_expire = row["netdisk_expire_at"] if row else None

    try:
        from .engine import NetdiskEngine  # 惰性导入：仅运行时依赖 baidu_uploader
    except Exception as e:  # noqa: BLE001
        msg = f"网盘引擎不可用: {e}"[:300]
        _set(netdisk_state=("failed" if finalize else "partial"), netdisk_msg=msg)
        log.exception("加载 NetdiskEngine 失败 jobid=%s", jobid)
        if finalize:
            raise
        return {"error": msg}

    engine = NetdiskEngine()
    link, pwd = existing_link, existing_pwd
    try:
        dir_remote = engine.remote_path_for(owner, dirname)
        n = len(paths)
        for i, local in enumerate(paths):
            fname = os.path.basename(local)
            remote = engine.remote_path_for(owner, f"{dirname}/{fname}")

            def _cb(done: int, total: int, _i: int = i, _f: str = fname) -> None:
                if handle is None:
                    return
                seg = 92.0 / n
                handle.update(phase=f"上传 {_i + 1}/{n}：{_f}",
                              progress=2 + _i * seg + (done / max(total, 1)) * seg)

            engine.upload(local, remote, progress_cb=_cb)  # 已传内容自动去重跳过
            if fname not in uploaded:
                uploaded.append(fname)
                _set(netdisk_files=json.dumps(uploaded, ensure_ascii=False))

        # 早建/复用分享链接（目录分享：后续新增文件自动包含在同一链接内）
        if not link:
            _phase("生成分享链接", 95)
            share = engine.share(dir_remote, pwd=None, period=period)
            link, pwd = share.get("link"), share.get("pwd")
    except Exception as e:  # noqa: BLE001
        _set(netdisk_state=("failed" if finalize else "partial"),
             netdisk_msg=str(e)[:300])
        log.exception("网盘上传/分享失败 jobid=%s", jobid)
        if finalize:
            raise
        return {"error": str(e)[:300]}
    finally:
        engine.close()

    fields = {
        "netdisk_state": "done" if finalize else "partial",
        "netdisk_dir": dir_remote,
        "netdisk_files": json.dumps(uploaded, ensure_ascii=False),
        "netdisk_msg": None,
    }
    if not existing_link and link:  # 首次创建链接才写入链接/提取码/有效期
        fields["netdisk_share_url"] = link
        fields["netdisk_share_pwd"] = pwd
        fields["netdisk_expire_at"] = (
            time.time() + period * 86400 if period > 0 else 0
        )
    else:
        fields["netdisk_expire_at"] = existing_expire
    _set(**fields)
    _phase("完成" if finalize else None, 100 if finalize else None)
    return {"link": link, "pwd": pwd, "dir": dirname, "files": uploaded,
            "finalize": finalize}


@register_task("netdisk_autoshare")
def netdisk_autoshare(handle: TaskHandle, params: dict) -> Dict:
    """结果文件一次性/最终上传网盘并分享（手动按钮与后处理完成后的最终补传）。

    params:
      jobid / workdir / owner / task_name / short_id / period
    """
    jobid = params["jobid"]
    short_id = params.get("short_id") or jobid
    return run_upload(
        jobid,
        params.get("workdir") or "",
        params["owner"],
        params.get("task_name") or short_id,
        short_id,
        int(params.get("period", 30)),
        files=None,
        finalize=True,
        handle=handle,
    )
