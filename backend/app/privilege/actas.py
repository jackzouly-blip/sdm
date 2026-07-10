"""以目标用户身份执行操作（act-as-user）。

服务以 root 运行。任何"碰用户资源"的操作（列目录、读文件、打包、
删除、qdel 等）都必须降权到目标 Linux 用户执行，让权限完全由操作系统
强制（含用户组 / ACL / 路径穿越），而不是在应用层手写权限判断。

提供两种原语：
  - run_as_user(): 以目标用户身份执行外部命令（subprocess），用于 tar / qdel 等。
  - call_as_user(): fork 子进程降权后执行一个 Python 可调用对象，结果经
    管道 pickle 回传，用于 os.listdir / open 等 Python 级文件操作。

安全要点：
  - 绝不使用 shell；命令一律以 argv 列表传入。
  - 降权顺序必须先 setgid + 设置附加组，再 setuid（顺序反了会丢权限）。
  - 仅 root 可降权；非 root 运行将直接报错，避免出现"看似降权实则没降"的假象。
"""
from __future__ import annotations

import fcntl
import grp
import os
import pickle
import pty
import pwd
import shutil
import struct
import subprocess
import termios
from dataclasses import dataclass

from ..logger import get_logger

log = get_logger(__name__)


class PrivilegeError(RuntimeError):
    pass


@dataclass(frozen=True)
class UserIdentity:
    name: str
    uid: int
    gid: int
    home: str
    shell: str


def resolve_user(username: str) -> UserIdentity:
    """解析系统用户信息；用户不存在则抛错。"""
    try:
        pw = pwd.getpwnam(username)
    except KeyError as e:
        raise PrivilegeError(f"系统用户不存在: {username}") from e
    return UserIdentity(
        name=pw.pw_name,
        uid=pw.pw_uid,
        gid=pw.pw_gid,
        home=pw.pw_dir,
        shell=pw.pw_shell,
    )


def _require_root() -> None:
    if os.geteuid() != 0:
        raise PrivilegeError("act-as-user 需要服务以 root 运行才能安全降权")


def _is_self(user: UserIdentity) -> bool:
    """目标用户是否就是当前进程用户（此时无需降权，直接执行即可）。"""
    return os.geteuid() == user.uid


def _supplementary_gids(user: UserIdentity) -> list[int]:
    """目标用户所属的全部附加组 gid。"""
    gids = {user.gid}
    for g in grp.getgrall():
        if user.name in g.gr_mem:
            gids.add(g.gr_gid)
    return sorted(gids)


def _demote(user: UserIdentity):
    """返回一个 preexec_fn：在子进程 exec 前降权到目标用户。"""

    def preexec() -> None:
        os.setgid(user.gid)
        os.setgroups(_supplementary_gids(user))
        os.setuid(user.uid)
        os.umask(0o077)

    return preexec


def run_as_user(
    username: str,
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """以目标用户身份执行外部命令（无 shell）。"""
    user = resolve_user(username)
    if not isinstance(argv, (list, tuple)) or not argv:
        raise PrivilegeError("argv 必须是非空列表，禁止 shell 拼接")
    # 已经是目标用户则无需降权；否则必须 root 才能 setuid
    preexec = None if _is_self(user) else _demote(user)
    if preexec is not None:
        _require_root()

    run_env = {
        "HOME": user.home,
        "USER": user.name,
        "LOGNAME": user.name,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    if env:
        run_env.update(env)

    log.info("以用户 %s 执行: %s", username, " ".join(argv))
    return subprocess.run(
        list(argv),
        cwd=cwd,
        env=run_env,
        input=input_bytes,
        capture_output=True,
        preexec_fn=preexec,
        timeout=timeout,
        check=False,
    )


def popen_as_user(
    username: str,
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stdout=None,
    stderr=None,
    start_new_session: bool = False,
) -> subprocess.Popen:
    """以目标用户身份启动一个**非阻塞**子进程（run_as_user 的 Popen 版）。

    与 run_as_user 的区别：不等待、不捕获输出，直接返回 Popen 句柄，供调用方
    自行保存 pid、轮询存活、随时中断——用于试算这类"在管理节点直接跑、要拿
    pid、要能中断/看输出"的长命令。

    start_new_session=True：子进程自成会话/进程组（pid==pgid），便于用
    os.killpg 连同其派生子进程整组中断，也让它不受服务端控制终端影响。
    降权顺序与 run_as_user 一致：先 setgid+附加组，再 setuid（在 preexec 内）。
    """
    user = resolve_user(username)
    if not isinstance(argv, (list, tuple)) or not argv:
        raise PrivilegeError("argv 必须是非空列表，禁止 shell 拼接")
    preexec = None if _is_self(user) else _demote(user)
    if preexec is not None:
        _require_root()

    run_env = {
        "HOME": user.home,
        "USER": user.name,
        "LOGNAME": user.name,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    if env:
        run_env.update(env)

    log.info("以用户 %s 启动进程: %s", username, " ".join(argv))
    return subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=run_env,
        stdout=stdout,
        stderr=stderr,
        preexec_fn=preexec,
        start_new_session=start_new_session,
    )


def fork_pty_as_user(
    username: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, int]:
    """以目标用户身份在伪终端（PTY）中启动其登录 shell。

    返回 (pid, master_fd)：父进程通过 master_fd 读写终端，子进程是降权后的
    交互式登录 shell，用于在线终端功能。

    与 run_as_user 一样：已是目标用户则不降权；否则必须 root。降权顺序同样是
    setgid + 附加组，再 setuid。
    """
    user = resolve_user(username)
    demote = not _is_self(user)
    if demote:
        _require_root()

    shell = user.shell or "/bin/bash"
    # 登录 shell 约定：argv[0] 以 '-' 开头
    argv0 = "-" + os.path.basename(shell)
    run_env = {
        "HOME": user.home,
        "USER": user.name,
        "LOGNAME": user.name,
        "SHELL": shell,
        "TERM": "xterm-256color",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    if env:
        run_env.update(env)
    target_cwd = cwd or user.home

    log.info("为用户 %s 启动 PTY 登录 shell: %s", username, shell)
    pid, master_fd = pty.fork()
    if pid == 0:
        # 子进程：降权后切目录并 exec shell；任何异常都以非零码退出
        try:
            if demote:
                os.setgid(user.gid)
                os.setgroups(_supplementary_gids(user))
                os.setuid(user.uid)
            os.umask(0o077)
            try:
                os.chdir(target_cwd)
            except OSError:
                os.chdir("/")
            os.execvpe(shell, [argv0], run_env)
        except BaseException:  # noqa: BLE001
            os._exit(127)
    return pid, master_fd


def set_winsize(fd: int, rows: int, cols: int) -> None:
    """设置伪终端窗口大小（行/列），让远端程序按真实尺寸渲染。"""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def call_as_user(username: str, func, *args, **kwargs):
    """fork 子进程降权后执行 Python 可调用对象，结果经管道 pickle 回传。

    func 必须可 pickle（顶层函数），其返回值同样需可 pickle。
    子进程内发生的异常会被捕获并在父进程重新抛出。
    """
    user = resolve_user(username)
    # 已经是目标用户：直接执行，无需 fork/降权
    if _is_self(user):
        return func(*args, **kwargs)
    _require_root()

    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        # 子进程：降权后执行
        os.close(rfd)
        try:
            os.setgid(user.gid)
            os.setgroups(_supplementary_gids(user))
            os.setuid(user.uid)
            os.umask(0o077)
            os.environ.update(
                {"HOME": user.home, "USER": user.name, "LOGNAME": user.name}
            )
            result = ("ok", func(*args, **kwargs))
        except BaseException as e:  # noqa: BLE001
            result = ("err", repr(e))
        try:
            payload = pickle.dumps(result)
            with os.fdopen(wfd, "wb") as w:
                w.write(payload)
        except BaseException:  # noqa: BLE001
            os.close(wfd)
        os._exit(0)

    # 父进程：读取结果
    os.close(wfd)
    with os.fdopen(rfd, "rb") as r:
        data = r.read()
    os.waitpid(pid, 0)
    if not data:
        raise PrivilegeError(f"以用户 {username} 执行子进程无返回（可能被信号终止）")
    status, value = pickle.loads(data)
    if status == "err":
        raise PrivilegeError(f"以用户 {username} 执行失败: {value}")
    return value


def stream_file_as_user(username: str, path: str, chunk: int = 1 << 20):
    """以目标用户身份流式读取文件：只 fork 一次，子进程降权后把文件写入管道，
    父进程从管道边读边 yield。避免按块多次 fork+pickle，适合大文件下载。"""
    user = resolve_user(username)
    demote = not _is_self(user)
    if demote:
        _require_root()
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        # 子进程：降权后读文件写入管道
        try:
            os.close(rfd)
            if demote:
                os.setgid(user.gid)
                os.setgroups(_supplementary_gids(user))
                os.setuid(user.uid)
            fd = os.open(path, os.O_RDONLY)
            while True:
                b = os.read(fd, chunk)
                if not b:
                    break
                off = 0
                while off < len(b):
                    off += os.write(wfd, b[off:])
        except BaseException:  # noqa: BLE001
            pass
        finally:
            try:
                os.close(wfd)
            except OSError:
                pass
            os._exit(0)

    os.close(wfd)

    def _gen():
        try:
            while True:
                b = os.read(rfd, chunk)
                if not b:
                    break
                yield b
        finally:
            try:
                os.close(rfd)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass

    return _gen()


def stream_tar_as_user(
    username: str, base: str, rel_paths: list[str], chunk: int = 1 << 20
):
    """以目标用户身份流式打 tar：`tar -cf - -C base rel...` 的 stdout 边读边 yield。

    不在服务器上落临时包，下载即刻开始（方案 B：尽快把数据传给客户端）。
    用 store 模式(无压缩)，对已压缩的 CAE 结果最快、最省 CPU。
    """
    user = resolve_user(username)
    preexec = None if _is_self(user) else _demote(user)
    if preexec is not None:
        _require_root()
    tar_bin = shutil.which("tar") or "tar"
    proc = subprocess.Popen(
        [tar_bin, "-cf", "-", "-C", base, *rel_paths],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        preexec_fn=preexec,
        bufsize=0,
    )

    def _gen():
        try:
            while True:
                b = proc.stdout.read(chunk)
                if not b:
                    break
                yield b
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()

    return _gen()
