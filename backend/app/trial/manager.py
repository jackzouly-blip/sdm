"""试算任务管理器：不进 PBS，直接在管理节点以属主身份跑一条命令。

与 JobPoller / SubmissionScheduler 同构：独立后台"收割"线程 + SQLite 持久化。
职责：
  - start_trial(): 以属主身份 Popen 一条命令（自成进程组），stdout/stderr 重定向
    到 workdir 下的日志文件，退出码经 status 文件落盘；pid/pgid 落库。
  - 收割线程: 轮询 running 试算的进程是否仍在，退出即读退出码置终态。
  - cancel(): os.killpg 连同派生子进程整组中断。
  - read_output(): 以属主身份增量读日志文件，供前端轮询 tail。
  - 重启接管: 进程用 start_new_session 独立存活，服务重启后按 pid 校验
    /proc/<pid>/cmdline 是否仍是本试算（防 pid 复用误判），是则继续监控，
    否则按有无 status 文件判定 finished/failed/interrupted。

安全：命令仅来自管理员维护的试算模板（kind=trial），普通用户只能选模板+
输入文件，不存在任意命令注入面；命令以属主降权执行，权限由 OS 兜底。
"""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from typing import Dict, Optional

from ..db.jobs_db import (
    JobsDB,
    TRIAL_FAILED,
    TRIAL_FINISHED,
    TRIAL_INTERRUPTED,
    TRIAL_KILLED,
)
from ..logger import get_logger
from ..privilege.actas import call_as_user, popen_as_user

log = get_logger(__name__)


class TrialError(RuntimeError):
    pass


def _read_tail(path: str, offset: int) -> dict:
    """（在属主子进程内执行）从 offset 增量读日志，返回新增字节与新 offset。

    顶层函数以便 call_as_user 可 pickle。文件被截断/轮转（size<offset）时从头读。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"data": b"", "offset": offset, "size": offset}
    if offset > size:
        offset = 0
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return {"data": b"", "offset": offset, "size": size}
    return {"data": data, "offset": size, "size": size}


class TrialManager:
    def __init__(self, db: JobsDB, max_concurrent: int = 4, interval: int = 3):
        self.db = db
        self.max_concurrent = max(1, max_concurrent)
        self.interval = max(1, interval)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()  # 串行化并发准入 + create
        # 本进程亲手拉起、仍活着的子进程句柄（可 poll 到退出码）；
        # 服务重启后接管的进程不在此表，靠 /proc 判活 + status 文件取退出码。
        self._procs: Dict[int, subprocess.Popen] = {}

    # --- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._reconcile_on_start()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="trial-reaper", daemon=True
        )
        self._thread.start()
        log.info("试算收割线程已启动，间隔 %ds，并发上限 %d", self.interval, self.max_concurrent)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._reap()
            except Exception:  # noqa: BLE001
                log.exception("试算收割异常")
            self._stop.wait(self.interval)

    # --- 提交 -----------------------------------------------------------

    def start_trial(self, owner: str, name: str, workdir: str, command: str) -> int:
        """以属主身份启动一条试算命令，返回试算 id。"""
        import uuid

        with self._lock:
            running = self.db.trial_count_running()
            if running >= self.max_concurrent:
                raise TrialError(
                    f"管理节点试算并发已达上限（{self.max_concurrent}），请稍后再试或先中断其他试算"
                )
            tag = uuid.uuid4().hex[:8]
            log_path = os.path.join(workdir, f".portal_trial_{tag}.log")
            status_path = os.path.join(workdir, f".portal_trial_{tag}.status")
            # 退出码落 status 文件：进程即便被 init 收养，重启后也能恢复真实退出码。
            # 用 EXIT 陷阱写盘（而非结尾一条 echo）：无论命令正常结束、中途 return，
            # 还是收到 SIGTERM/SIGINT 等普通信号，退出时都会落盘退出码——只有 SIGKILL
            # 杀不掉陷阱。这修掉了此前"命令已正常结束、status 却偶发未写出"的问题，
            # 保证重启恢复逻辑能据 status 准确判定 finished/failed（无 status=真被硬杀）。
            wrapper = (
                f"exec >{shlex.quote(log_path)} 2>&1\n"
                f"trap 'echo $? >{shlex.quote(status_path)}' EXIT\n"
                f"{command}\n"
            )
            bash = shutil.which("bash") or "/bin/bash"
            trial_id = self.db.trial_create(
                owner, name, workdir, command, log_path, status_path
            )
            try:
                proc = popen_as_user(
                    owner,
                    [bash, "-c", wrapper],
                    cwd=workdir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,  # pid==pgid，便于整组中断
                )
            except Exception as e:  # noqa: BLE001
                self.db.trial_finish(trial_id, TRIAL_FAILED, msg=f"启动失败: {e}")
                log.exception("试算启动失败 id=%s owner=%s", trial_id, owner)
                raise TrialError(f"启动失败: {e}") from e
            self._procs[trial_id] = proc
            self.db.trial_set_started(trial_id, proc.pid, proc.pid)
            log.info(
                "试算已启动 id=%s pid=%s owner=%s workdir=%s", trial_id, proc.pid, owner, workdir
            )
            return trial_id

    # --- 中断 -----------------------------------------------------------

    def cancel(self, trial_id: int) -> None:
        row = self.db.trial_get(trial_id)
        if row is None:
            raise TrialError("试算不存在")
        if row["status"] != "running":
            raise TrialError("试算已结束，无需中断")
        pgid = row["pgid"] or row["pid"]
        if not pgid:
            self.db.trial_finish(trial_id, TRIAL_KILLED, msg="无有效进程组")
            return
        self._signal_group(pgid, signal.SIGTERM)
        # 宽限等待优雅退出，否则升级 SIGKILL
        if not self._wait_gone(trial_id, pgid, timeout=3.0):
            self._signal_group(pgid, signal.SIGKILL)
            self._wait_gone(trial_id, pgid, timeout=2.0)
        exit_code = self._read_exit_code(row["status_path"])
        self.db.trial_finish(trial_id, TRIAL_KILLED, exit_code=exit_code, msg="用户中断")
        proc = self._procs.pop(trial_id, None)
        if proc is not None:
            try:
                proc.wait(timeout=1)
            except Exception:  # noqa: BLE001
                pass
        log.info("试算已中断 id=%s pgid=%s", trial_id, pgid)

    def _signal_group(self, pgid: int, sig: int) -> None:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            log.warning("无权向进程组 %s 发送信号 %s（服务未以 root 运行？）", pgid, sig)
        except OSError as e:
            log.warning("向进程组 %s 发信号失败: %s", pgid, e)

    def _wait_gone(self, trial_id: int, pgid: int, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            proc = self._procs.get(trial_id)
            if proc is not None:
                if proc.poll() is not None:
                    return True
            elif not self._group_alive(pgid):
                return True
            time.sleep(0.1)
        # 最后再判一次
        proc = self._procs.get(trial_id)
        if proc is not None:
            return proc.poll() is not None
        return not self._group_alive(pgid)

    # --- 输出 -----------------------------------------------------------

    def read_output(self, row, offset: int) -> dict:
        """以属主身份增量读日志文件。返回 {data(str), offset, size}。"""
        try:
            res = call_as_user(row["owner"], _read_tail, row["log_path"], int(offset or 0))
        except Exception as e:  # noqa: BLE001
            log.warning("读取试算输出失败 id=%s: %s", row["id"], e)
            return {"data": "", "offset": offset, "size": offset}
        data = res.get("data", b"")
        text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
        return {"data": text, "offset": res.get("offset", offset), "size": res.get("size", offset)}

    # --- 收割 -----------------------------------------------------------

    def _reap(self) -> None:
        for row in self.db.trial_list_running():
            tid = row["id"]
            proc = self._procs.get(tid)
            # 启动竞态防护：start_trial 是"先 trial_create 落库(status=running,
            # pid 尚为 NULL) → Popen → 登记句柄 → 回填 pid"。若收割器恰好在这 <1s
            # 空窗里扫到这行，会既拿不到句柄、又读不到 pid/status 文件，从而误判
            # "进程已消失"。pid 为空即代表仍在启动窗口内，一律跳过、绝不据此判死。
            if proc is None and not row["pid"]:
                continue
            if proc is not None:
                rc = proc.poll()
                if rc is None:
                    continue  # 仍在跑
                # 亲生子进程已退出：proc.returncode 权威；再兜底读 status 文件
                exit_code = rc if rc is not None else self._read_exit_code(row["status_path"])
                self._finalize(tid, exit_code)
                self._procs.pop(tid, None)
            else:
                # 服务重启后接管的进程（无句柄）：靠 /proc 判活
                pgid = row["pgid"] or row["pid"]
                if pgid and self._proc_is_ours(row["pid"], row["status_path"]):
                    continue  # 仍是本试算，继续监控
                exit_code = self._read_exit_code(row["status_path"])
                if exit_code is None:
                    # 进程没了又没留下退出码：判为中断（多为被硬杀 SIGKILL/宕机）。
                    # 记一条 warning 便于事后定位——正常结束会有 status 文件走上面分支。
                    log.warning(
                        "试算 id=%s 判为中断：无进程句柄、pid=%s 已不属本试算、且无退出码文件",
                        tid, row["pid"],
                    )
                    self.db.trial_finish(
                        tid, TRIAL_INTERRUPTED, msg="进程已消失且无退出码记录（可能被中断）"
                    )
                else:
                    self._finalize(tid, exit_code)

    def _finalize(self, trial_id: int, exit_code: Optional[int]) -> None:
        if exit_code == 0:
            self.db.trial_finish(trial_id, TRIAL_FINISHED, exit_code=0)
        else:
            self.db.trial_finish(
                trial_id, TRIAL_FAILED, exit_code=exit_code,
                msg=f"退出码 {exit_code}" if exit_code is not None else "非正常退出",
            )

    def _reconcile_on_start(self) -> None:
        """启动自愈：对上次遗留的 running 试算逐一判定去留。

        进程用 start_new_session 独立存活，故服务重启后很可能仍在跑——校验
        /proc/<pid>/cmdline 仍是本试算（防 pid 复用）则保留继续监控；否则读
        status 文件定终态，无则判 interrupted。"""
        for row in self.db.trial_list_running():
            tid = row["id"]
            if row["pid"] and self._proc_is_ours(row["pid"], row["status_path"]):
                log.info("试算 id=%s pid=%s 重启后仍在运行，继续接管监控", tid, row["pid"])
                continue
            exit_code = self._read_exit_code(row["status_path"])
            if exit_code is None:
                self.db.trial_finish(
                    tid, TRIAL_INTERRUPTED, msg="服务重启期间进程已结束，无退出码记录"
                )
                log.warning("试算 id=%s 重启后进程已不在，标记为中断", tid)
            else:
                self._finalize(tid, exit_code)

    # --- /proc 工具 -----------------------------------------------------

    @staticmethod
    def _read_cmdline(pid: int) -> Optional[bytes]:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read()
        except (FileNotFoundError, ProcessLookupError, OSError):
            return None

    def _proc_is_ours(self, pid: Optional[int], token: str) -> bool:
        """pid 存活且其 cmdline 含本试算的唯一 status 路径 → 确属本试算。

        用 status_path（含 uuid）做指纹，避免 pid 被无关进程复用时误判为存活。"""
        if not pid:
            return False
        cl = self._read_cmdline(pid)
        if cl is None:
            return False
        return token.encode() in cl

    def _group_alive(self, pgid: int) -> bool:
        """进程组是否仍存在（发 0 信号探测）。"""
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # 存在但无权（理论上 root 不会遇到）
        except OSError:
            return False

    @staticmethod
    def _read_exit_code(status_path: str) -> Optional[int]:
        """读退出码 status 文件（属主写入，root 可读）。缺失/无法解析返回 None。"""
        try:
            with open(status_path, "r") as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None


_manager: Optional[TrialManager] = None


def set_trial_manager(m: TrialManager) -> None:
    global _manager
    _manager = m


def get_trial_manager() -> Optional[TrialManager]:
    return _manager
