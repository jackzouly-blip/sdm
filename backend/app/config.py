"""应用配置。环境变量优先，前缀 HPC_。"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HPC_", env_file=".env", extra="ignore")

    # 服务
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # 会话 / JWT
    jwt_secret: str = "CHANGE_ME_IN_PRODUCTION"
    jwt_algorithm: str = "HS256"
    session_ttl_minutes: int = 480  # 8 小时

    # Agent 级 Bearer Token：供 3dix 门户作为可信内部调用方使用（与按用户 JWT 并存）。
    # 留空=禁用 agent 调用。配置后，携带此 token 的请求需用 X-Act-As-User 头指定
    # 目标用户名，agent 据该用户名 setuid 执行——信任由门户的认证与本 token 共同保证。
    agent_api_token: str = ""

    # PBS / Torque
    pbs_poll_interval: int = 15  # 轮询 qstat 间隔（秒）
    accounting_dir: str = "/var/spool/torque/server_priv/accounting"

    # 文件浏览允许的根白名单（冒号分隔，类似 PATH）。仅这些根及其子目录可访问，
    # 再叠加 OS 权限以登录用户身份兜底。
    fs_roots: str = "/data"
    # 预览文本文件的大小上限（字节）
    fs_preview_max_bytes: int = 1024 * 1024

    # 数据库
    db_path: str = str(BACKEND_DIR / "state" / "portal.db")

    # 打包临时目录（打 tar 落盘处）
    scratch_dir: str = "/tmp/hpc-portal-scratch"
    # Office 在线预览：LibreOffice 转 PDF
    office_soffice: str = ""  # soffice/libreoffice 可执行路径；留空则在 PATH 搜
    office_cache_dir: str = "/tmp/hpc-portal-office"
    office_timeout: int = 120
    # 单个归档体积阈值（字节）：一组文件总量超过该值时，按文件分到多个包，
    # 每包不超过此阈值。百度单文件硬上限约 4GiB（4MiB×1024 分片），这里按原始
    # 大小留余量默认 3.5GiB；压缩后通常更小，故偏保守不会超限。
    package_volume_bytes: int = 3584 * 1024 * 1024  # 3.5 GiB

    # 网盘上传配置（baidu_uploader）
    baidu_config: str = str(BACKEND_DIR / "baidu_uploader_config.yaml")
    # 提取完成后自动上传网盘并分享的用户白名单（冒号分隔）。
    # 与 admin_users 语义相反：留空=对所有人关闭（自动上传为按用户显式开通），
    # 仅名单内用户的任务在后处理完成后才会自动触发上传分享。
    netdisk_auto_users: str = ""
    # 自动/手动分享链接有效天数（0=永久）
    netdisk_share_period: int = 30
    # 运行中流式上传：对白名单用户的“计算中”任务，提前上传已写完的 d3plot
    # 并尽早生成分享链接（其余文件等任务结束后由最终补传上传）。
    netdisk_stream_enabled: bool = True
    netdisk_stream_interval: int = 60        # 流式扫描间隔（秒）
    netdisk_stream_stable_seconds: int = 120  # d3plot 距上次写入多久算“写完稳定”

    # d3plot 网页可视化
    # 装有 lasso-python 的 Python 解释器路径；留空则用后端自身解释器（须已装 lasso）
    d3plot_python: str = ""
    d3plot_cache_dir: str = "/tmp/hpc-portal-d3plot"  # 解析产物缓存
    d3plot_timeout: int = 1800
    d3plot_max_states: int = 40  # 状态(帧)数上限，超过则全程均匀抽样以控产物体积；0=全部
    d3plot_max_tris: int = 1_200_000  # 三角面预算：超过则服务器端体素聚类减面，0=不减面

    # 作业提交：脚本内路径前缀映射(head 路径→计算节点路径)。
    # 例 "/data=/caedata"：门户在 /data 下操作(head)，但提交脚本里的初始路径写成
    # /caedata(计算节点挂载路径)。多组用逗号分隔。留空=不映射。
    submit_path_map: str = ""
    # 作业提交默认队列（Torque 未设 default_queue 时必须 qsub -q 指定）
    submit_default_queue: str = "batch"

    # 数据提取：任务完成后在 workdir 运行第三方提取命令的超时（秒）
    extract_timeout: int = 1800
    # 可管理提取规则的运维白名单（冒号分隔）。留空表示不限制（适合本地联调）。
    # 规则为全局生效且会以任务属主身份执行命令，应仅限运维配置。
    admin_users: str = ""

    @property
    def fs_root_list(self) -> list[str]:
        """归一化后的根白名单（去尾斜杠、去空项）。"""
        roots = []
        for r in self.fs_roots.split(":"):
            r = r.strip().rstrip("/")
            if r:
                roots.append(r)
        return roots

    @property
    def admin_user_list(self) -> list[str]:
        """可管理提取规则的用户白名单。"""
        return [u.strip() for u in self.admin_users.split(":") if u.strip()]

    def is_admin(self, user: str) -> bool:
        """白名单为空时放开所有人，否则仅白名单内用户可管理规则。"""
        admins = self.admin_user_list
        return not admins or user in admins

    @property
    def netdisk_auto_user_list(self) -> list[str]:
        """提取完成后自动上传网盘的用户白名单。"""
        return [u.strip() for u in self.netdisk_auto_users.split(":") if u.strip()]

    def netdisk_auto_enabled(self, user: str) -> bool:
        """该用户的任务是否在后处理完成后自动上传分享（按用户显式开通）。"""
        return user in self.netdisk_auto_user_list

    def map_path(self, p: str) -> str:
        """按 submit_path_map 把路径前缀从 head 路径换成计算节点路径(如 /data→/caedata)。
        用于:作业提交脚本内的初始路径、作业列表/详情展示的 workdir。"""
        if not p:
            return p
        for item in (self.submit_path_map or "").split(","):
            item = item.strip()
            if "=" not in item:
                continue
            src, dst = (x.rstrip("/") for x in item.split("=", 1))
            if src and (p == src or p.startswith(src + "/")):
                return dst + p[len(src):]
        return p


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
