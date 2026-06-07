"""百度 OAuth2 授权与 token 管理。

流程（授权码模式）：
  1. 引导用户在浏览器打开授权 URL，登录并同意授权
  2. oob 模式下页面会显示授权码 code，用户粘贴回来
     local_server 模式下回调服务器自动捕获 code
  3. 用 code 换取 access_token + refresh_token，持久化到本地
  4. 后续调用前若 token 即将过期，用 refresh_token 自动续期

参考：https://pan.baidu.com/union/doc/al0rwqzzl
"""
from __future__ import annotations

import json
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

import httpx

from ..config import Config
from ..logger import get_logger

log = get_logger(__name__)

AUTHORIZE_URL = "https://openapi.baidu.com/oauth/2.0/authorize"
TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"
# 提前这么多秒刷新，避免临界过期
REFRESH_MARGIN = 600


class TokenStore:
    """access_token / refresh_token 的本地持久化。"""

    def __init__(self, path: Path):
        self.path = path
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.expires_at: float = 0.0
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.access_token = data.get("access_token")
            self.refresh_token = data.get("refresh_token")
            self.expires_at = data.get("expires_at", 0.0)

    def save(self, access_token: str, refresh_token: str, expires_in: int) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = time.time() + int(expires_in)
        self.path.write_text(
            json.dumps(
                {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_at": self.expires_at,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        # token 文件权限收紧
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @property
    def is_valid(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at - REFRESH_MARGIN

    @property
    def has_refresh(self) -> bool:
        return bool(self.refresh_token)


class BaiduOAuth:
    def __init__(self, config: Config):
        self.cfg = config
        self.store = TokenStore(config.token_path)
        self._client = httpx.Client(timeout=30)

    # --- 公开方法 -------------------------------------------------------

    def get_access_token(self) -> str:
        """返回可用的 access_token；过期则自动刷新，无 token 则引导授权。"""
        if self.store.is_valid:
            return self.store.access_token  # type: ignore[return-value]
        if self.store.has_refresh:
            try:
                return self._refresh()
            except Exception as e:  # noqa: BLE001
                log.warning("刷新 token 失败，需要重新授权：%s", e)
        return self.authorize()

    def authorize(self) -> str:
        """首次授权，拿到并保存 token，返回 access_token。"""
        if self.cfg.auth.mode == "local_server":
            code = self._authorize_local_server()
        else:
            code = self._authorize_oob()
        return self._exchange_code(code)

    # --- 授权获取 code --------------------------------------------------

    def _build_authorize_url(self) -> str:
        params = {
            "response_type": "code",
            "client_id": self.cfg.app.app_key,
            "redirect_uri": self.cfg.auth.redirect_uri,
            "scope": self.cfg.auth.scope,
            "display": "page",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def _authorize_oob(self) -> str:
        url = self._build_authorize_url()
        print("\n请在浏览器中打开以下链接完成授权，然后把页面显示的授权码粘贴回来：\n")
        print(url + "\n")
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        code = input("请输入授权码 (code): ").strip()
        if not code:
            raise RuntimeError("未输入授权码")
        return code

    def _authorize_local_server(self) -> str:
        """本地起一次性 HTTP 服务器接收回调 code。"""
        import http.server
        import socketserver
        from urllib.parse import urlparse, parse_qs

        redirect = self.cfg.auth.redirect_uri
        parsed = urlparse(redirect)
        port = parsed.port or 8088
        captured: dict[str, str] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                qs = parse_qs(urlparse(self.path).query)
                if "code" in qs:
                    captured["code"] = qs["code"][0]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write("授权成功，可关闭此页面。".encode("utf-8"))
                else:
                    self.send_response(400)
                    self.end_headers()

            def log_message(self, *args):  # 静默
                pass

        url = self._build_authorize_url()
        print("\n正在打开浏览器进行授权，请在浏览器中同意授权...\n")
        print(url + "\n")
        webbrowser.open(url)
        with socketserver.TCPServer(("", port), Handler) as httpd:
            log.info("本地回调服务器已启动，监听端口 %s，等待授权回调...", port)
            while "code" not in captured:
                httpd.handle_request()
        return captured["code"]

    # --- token 交换与刷新 ----------------------------------------------

    def _exchange_code(self, code: str) -> str:
        params = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.cfg.app.app_key,
            "client_secret": self.cfg.app.secret_key,
            "redirect_uri": self.cfg.auth.redirect_uri,
        }
        resp = self._client.get(TOKEN_URL, params=params)
        data = self._parse_token_resp(resp)
        self.store.save(data["access_token"], data["refresh_token"], data["expires_in"])
        log.info("授权成功，token 已保存到 %s", self.store.path)
        return data["access_token"]

    def _refresh(self) -> str:
        log.info("access_token 即将过期，使用 refresh_token 刷新...")
        params = {
            "grant_type": "refresh_token",
            "refresh_token": self.store.refresh_token,
            "client_id": self.cfg.app.app_key,
            "client_secret": self.cfg.app.secret_key,
        }
        resp = self._client.get(TOKEN_URL, params=params)
        data = self._parse_token_resp(resp)
        self.store.save(data["access_token"], data["refresh_token"], data["expires_in"])
        log.info("token 刷新成功")
        return data["access_token"]

    @staticmethod
    def _parse_token_resp(resp: httpx.Response) -> dict:
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(
                f"OAuth 失败: {data.get('error')} - {data.get('error_description')}"
            )
        if "access_token" not in data:
            raise RuntimeError(f"OAuth 响应缺少 access_token: {data}")
        return data
