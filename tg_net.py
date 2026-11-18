"""Kết nối Telegram của bot tự đổi đường: đi thẳng hoặc qua proxy (PROXY_URL trong .env).

Máy chạy bot lúc ở mạng công ty (bắt buộc qua proxy), lúc ở ngoài (proxy không tới được): mỗi request thử đường
đang dùng trước, lỗi kết nối thì thử đường còn lại và nhớ đường chạy được. Không đặt PROXY_URL = luôn đi thẳng.
(Tra vận đơn chạy ở web, có net.py riêng bên đó.)
"""
import logging
import os
import time

import httpx
from telegram.error import NetworkError
from telegram.request import BaseRequest, HTTPXRequest

log = logging.getLogger(__name__)

# lỗi do đường mạng (không tới được server / proxy) → đáng thử đường khác.
# Lỗi đọc (server chậm trả lời) thì không thử lại để tránh gửi trùng tin.
ROUTE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)

_use_proxy = False


def proxy_url() -> str | None:
    return os.getenv("PROXY_URL", "").strip() or None


def routes() -> list[str | None]:
    """Các đường theo thứ tự thử: đường đang dùng trước. None = đi thẳng."""
    proxy = proxy_url()
    if not proxy:
        return [None]
    return [proxy, None] if _use_proxy else [None, proxy]


def _remember(proxy: str | None):
    global _use_proxy
    if (proxy is not None) != _use_proxy:
        _use_proxy = proxy is not None
        log.info("Mạng: chuyển sang %s", f"proxy {proxy}" if proxy else "kết nối trực tiếp")


def route_name() -> str:
    return f"proxy {proxy_url()}" if _use_proxy else "kết nối trực tiếp"


class FailoverRequest(BaseRequest):
    """Request cho python-telegram-bot: 1 HTTPXRequest cho mỗi đường, lỗi kết nối thì đổi đường."""

    def __init__(self, connection_pool_size: int = 1):
        self._pool = connection_pool_size
        self._reqs = {p: self._new(p) for p in [None, proxy_url()]}
        self.last_ok = time.monotonic()  # lần cuối có request thành công (watchdog polling đọc)

    def _new(self, proxy):
        return HTTPXRequest(connection_pool_size=self._pool, proxy=proxy, httpx_kwargs={"trust_env": False})

    async def reset(self):
        """Bỏ hết kết nối cũ, tạo client mới. Pool 1 kết nối (get_updates) có thể bị kẹt sau lỗi mạng:
        mọi lần sau đều TimedOut (python-telegram-bot chỉ log debug, thử lại mãi) → bot im lặng."""
        old, self._reqs = self._reqs, {p: self._new(p) for p in self._reqs}
        for r in self._reqs.values():
            await r.initialize()
        for r in old.values():
            try:
                await r.shutdown()
            except Exception as e:  # noqa: BLE001
                log.debug("Đóng client cũ lỗi: %s", e)

    @property
    def read_timeout(self):
        return self._reqs[None].read_timeout

    async def initialize(self):
        for r in self._reqs.values():
            await r.initialize()

    async def shutdown(self):
        for r in self._reqs.values():
            await r.shutdown()

    async def do_request(self, url, method, request_data=None, read_timeout=BaseRequest.DEFAULT_NONE,
                         write_timeout=BaseRequest.DEFAULT_NONE, connect_timeout=BaseRequest.DEFAULT_NONE,
                         pool_timeout=BaseRequest.DEFAULT_NONE):
        last = None
        for proxy in routes():
            try:
                res = await self._reqs[proxy].do_request(url, method, request_data, read_timeout,
                                                         write_timeout, connect_timeout, pool_timeout)
            except NetworkError as e:
                if not isinstance(e.__cause__, ROUTE_ERRORS):
                    raise
                last = e
                continue
            _remember(proxy)
            self.last_ok = time.monotonic()
            return res
        raise last


def tg_bot_kwargs() -> dict:
    """Tham số cho telegram.Bot(...) để bot alert cũng đổi đường."""
    return {"request": FailoverRequest(8), "get_updates_request": FailoverRequest(1)}
