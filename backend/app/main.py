"""HPC 任务管理 Web 门户 —— FastAPI 入口。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .auth.router import router as auth_router
from .config import get_settings
from .config import BACKEND_DIR
from .d3plot.router import router as d3plot_router
from .db.jobs_db import JobsDB
from .extract.dispatcher import Dispatcher, set_dispatcher
from .extract.router import router as extract_router
from .extract.rules_db import RulesDB
from .fs.favorites_db import FavoritesDB
from .fs.router import router as fs_router
from .jobs.poller import JobPoller
from .netdisk import puller as _netdisk_puller  # noqa: F401  注册 netdisk_pull 任务
from .netdisk.router import router as netdisk_router
from .netdisk.scanner import NetdiskScanner, set_scanner
from .netdisk.streamer import NetdiskStreamer, set_streamer
from .netdisk.sync_db import NetdiskSyncDB
from .jobs.router import router as jobs_router
from .nodes.router import router as nodes_router
from .logger import get_logger, setup_logging
from .packaging.router import router as packaging_router
from .shell.router import router as shell_router
from .sim.db import SimDB
from .sim.deck import service as _sim_deck_service  # noqa: F401  注册 sim_deck_convert 任务
from .sim.engine import PipelineEngine, set_engine
from .sim.nodes import register_builtin_node_types
from .sim.pipeline_router import router as sim_pipeline_router
from .sim.router import router as sim_router
from .stats.router import router as stats_router
from .submit.db import TemplatesDB
from .submit.router import router as submit_router
from .submit.scheduler import SubmissionScheduler, set_scheduler
from .tasks.manager import TaskManager
from .trial.manager import TrialManager, set_trial_manager
from .trial.router import router as trial_router

settings = get_settings()
setup_logging(settings.log_level)
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：初始化任务库 + 轮询器 + 异步任务管理器
    db = JobsDB(settings.db_path)
    poller = JobPoller(db, interval=settings.pbs_poll_interval)
    task_manager = TaskManager(str(BACKEND_DIR / "state" / "tasks.db"))
    rules_db = RulesDB(str(BACKEND_DIR / "state" / "extract_rules.db"))
    templates_db = TemplatesDB(str(BACKEND_DIR / "state" / "templates.db"))
    favorites_db = FavoritesDB(str(BACKEND_DIR / "state" / "favorites.db"))
    sim_db = SimDB(str(BACKEND_DIR / "state" / "sim.db"))
    register_builtin_node_types()
    # 编排节点复用现有链路：hpc.submit 走提交，viewer.prepare 走异步任务
    pipeline_engine = PipelineEngine(sim_db, jobs_db=db, templates_db=templates_db,
                                     settings=settings, task_manager=task_manager)
    set_engine(pipeline_engine)
    dispatcher = Dispatcher(db, rules_db, task_manager)
    set_dispatcher(dispatcher)
    streamer = NetdiskStreamer(
        db,
        interval=settings.netdisk_stream_interval,
        stable_seconds=settings.netdisk_stream_stable_seconds,
    )
    set_streamer(streamer)
    # 网盘入站同步：分享源库 + 定时轮询（凭据未配时轮询自身会空转，不影响启动）
    netdisk_sync_db = NetdiskSyncDB(str(BACKEND_DIR / "state" / "netdisk_sync.db"))
    _netdisk_puller.set_sync_db(netdisk_sync_db)
    netdisk_scanner = NetdiskScanner(
        netdisk_sync_db,
        task_manager,
        interval=settings.netdisk_pull_interval,
        jitter=settings.netdisk_pull_jitter,
    )
    set_scanner(netdisk_scanner)
    scheduler = SubmissionScheduler(db, interval=settings.submit_scheduler_interval)
    set_scheduler(scheduler)
    trial_manager = TrialManager(
        db,
        max_concurrent=settings.trial_max_concurrent,
        interval=settings.trial_reap_interval,
    )
    set_trial_manager(trial_manager)
    app.state.jobs_db = db
    app.state.poller = poller
    app.state.task_manager = task_manager
    app.state.netdisk_streamer = streamer
    app.state.netdisk_sync_db = netdisk_sync_db
    app.state.netdisk_scanner = netdisk_scanner
    app.state.rules_db = rules_db
    app.state.templates_db = templates_db
    app.state.favorites_db = favorites_db
    app.state.sim_db = sim_db
    app.state.pipeline_engine = pipeline_engine
    app.state.dispatcher = dispatcher
    app.state.scheduler = scheduler
    app.state.trial_manager = trial_manager
    if os.geteuid() != 0:
        log.warning("当前非 root 运行：act-as-user 降权将不可用，仅适合本地接口联调")
    else:
        log.info("以 root 运行，act-as-user 已就绪")
    # 服务重启后补派发上次未处理的已完成任务
    try:
        dispatcher.recover_pending()
    except Exception:  # noqa: BLE001
        log.exception("提取补派发失败")
    # 上次进程被杀时残留的 syncing 占位会让该源永远无法再同步，启动时清掉
    try:
        stuck = netdisk_sync_db.reset_stuck()
        if stuck:
            log.warning("清理 %d 个中断的网盘同步占位", stuck)
    except Exception:  # noqa: BLE001
        log.exception("清理网盘同步占位失败")
    poller.start()
    streamer.start()
    netdisk_scanner.start()
    scheduler.start()
    trial_manager.start()
    try:
        yield
    finally:
        poller.stop()
        streamer.stop()
        netdisk_scanner.stop()
        scheduler.stop()
        trial_manager.stop()
        task_manager.close()
        netdisk_sync_db.close()
        rules_db.close()
        templates_db.close()
        favorites_db.close()
        sim_db.close()
        db.close()


app = FastAPI(title="HPC Portal", version="0.1.0", lifespan=lifespan)

# 开发期允许前端跨域；生产由 nginx 同源反代，可收紧
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(jobs_router)
app.include_router(fs_router)
app.include_router(packaging_router)
app.include_router(extract_router)
app.include_router(shell_router)
app.include_router(d3plot_router)
app.include_router(submit_router)
app.include_router(stats_router)
app.include_router(trial_router)
app.include_router(nodes_router)
app.include_router(netdisk_router)
app.include_router(sim_router)
app.include_router(sim_pipeline_router)


@app.get("/health")
def health() -> dict:
    poller = getattr(app.state, "poller", None)
    return {
        "status": "ok",
        "running_as_root": os.geteuid() == 0,
        "version": app.version,
        "poller_last_ok": getattr(poller, "last_ok_ts", None),
        "poller_last_error": getattr(poller, "last_error", None),
    }


def _current_ipv6() -> Optional[str]:
    """读取本机当前全局 IPv6 地址（排除链路本地/回环/ULA），优先后缀 ::100。

    解析 /proc/net/if_inet6（无外部依赖）。供前端展示"IPv6 入口"，
    ISP 前缀变化由 ipv6-autoheal 跟进，本接口总是返回当下真实地址。"""
    import ipaddress

    gua = ipaddress.ip_network("2000::/3")  # 全局单播地址段
    cands: list[str] = []
    try:
        with open("/proc/net/if_inet6") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 6:
                    continue
                raw, _idx, _plen, scope, _flags, _ifname = parts[:6]
                if scope != "00":  # 00 = global scope（排除 fe80 链路本地等）
                    continue
                addr = ":".join(raw[i:i + 4] for i in range(0, 32, 4))
                try:
                    ip = ipaddress.IPv6Address(addr)
                except ValueError:
                    continue
                if ip in gua:  # 仅取全局单播，排除 ULA(fc00::/7)/回环等
                    cands.append(str(ip))
    except OSError:
        return None
    for a in cands:
        if a.endswith("::100"):
            return a
    return cands[0] if cands else None


@app.get("/system/ipv6")
def system_ipv6() -> dict:
    """当前服务器全局 IPv6 地址（无需登录，供登录前/顶栏展示 IPv6 入口）。"""
    return {"ipv6": _current_ipv6()}
