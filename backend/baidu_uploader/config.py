"""配置加载：从 config.yaml 读取，支持环境变量覆盖敏感项。"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

# 项目根目录（config.yaml 所在处）
ROOT_DIR = Path(__file__).resolve().parent.parent


class AppConfig(BaseModel):
    app_id: str
    app_key: str
    secret_key: str
    sign_key: str = ""


class AuthConfig(BaseModel):
    mode: str = "oob"
    redirect_uri: str = "oob"
    scope: str = "basic,netdisk"
    token_file: str = "baidu.token.json"


class UploadConfig(BaseModel):
    local_dir: str
    remote_dir: str
    chunk_size: int = 4 * 1024 * 1024
    concurrency: int = 3


class StateConfig(BaseModel):
    db_path: str = "state/uploader.db"


class LogConfig(BaseModel):
    level: str = "INFO"
    file: str = "uploader.log"


class Config(BaseModel):
    app: AppConfig
    auth: AuthConfig = Field(default_factory=AuthConfig)
    upload: UploadConfig
    state: StateConfig = Field(default_factory=StateConfig)
    log: LogConfig = Field(default_factory=LogConfig)

    @property
    def token_path(self) -> Path:
        return ROOT_DIR / self.auth.token_file


def load_config(path: str | os.PathLike | None = None) -> Config:
    """加载配置；环境变量 BAIDU_APP_KEY / BAIDU_SECRET_KEY 优先于文件。"""
    cfg_path = Path(path) if path else ROOT_DIR / "config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"未找到配置文件 {cfg_path}，请复制 config.example.yaml 为 config.yaml 并填写。"
        )
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    # 环境变量覆盖
    app = data.setdefault("app", {})
    if os.getenv("BAIDU_APP_KEY"):
        app["app_key"] = os.environ["BAIDU_APP_KEY"]
    if os.getenv("BAIDU_SECRET_KEY"):
        app["secret_key"] = os.environ["BAIDU_SECRET_KEY"]

    return Config.model_validate(data)
