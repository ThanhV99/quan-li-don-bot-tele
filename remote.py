"""Bot không có logic nghiệp vụ / database: các module db, tracking, sale_text, utils, alert_rules nằm ở web.
install() đặt vào sys.modules các module "giả" cùng tên: `db.get(...)`, `tracking.track_saved(...)`, `money(...)`…
thành lời gọi POST {API_URL}/api/svc/<module>.<hàm> (token SERVICE_TOKEN), nên code bot giữ nguyên `import db` …

- Hằng số (STATUS_*, TABLES, ALERT_KEYWORDS, VC_NEAR, URL_RE …) + danh sách hàm lấy 1 lần (<module>.__info__).
- Hàm thường gọi đồng bộ (như sqlite trước đây; trong mạng docker vài ms). Hàm async ở web (tracking.track_saved)
  thành hàm async ở bot, chạy HTTP trong thread → không chặn vòng lặp bot khi đang tra hãng.
- Hàm thuần (web báo trong __info__: money, fold, parse_money …) được nhớ kết quả theo tham số → gọi lại không tốn API.
"""
import asyncio
import builtins
import contextvars
import logging
import os
import sys
import threading
import time
import types

import httpx

import rpc

log = logging.getLogger("remote")

API_URL = os.getenv("API_URL", "").rstrip("/")
SERVICE_TOKEN = os.getenv("SERVICE_TOKEN", "")
TIMEOUT = float(os.getenv("API_TIMEOUT", "30"))
ASYNC_TIMEOUT = 180  # tra vận đơn: chờ lượt API hãng (throttle) + hãng trả chậm
CONNECT_RETRIES = 3  # web đang khởi động lại (deploy) → thử lại vài lần
STARTUP_WAIT = 120  # lần đầu lấy thông tin module: chờ web lên tối đa chừng này giây
MODULES = ("db", "tracking", "sale_text", "utils", "alert_rules")
PURE_CACHE_MAX = 20000

# giống db.py: nguồn theo tiến trình, người đổi theo từng lượt (gửi kèm mỗi lời gọi → nhật ký đổi trạng thái)
SOURCE = "bot"
ACTOR: contextvars.ContextVar[str | None] = contextvars.ContextVar("db_actor", default=None)

_client = httpx.Client(base_url=API_URL, timeout=TIMEOUT, trust_env=False,
                       headers={"X-Service-Token": SERVICE_TOKEN, "Content-Type": "application/json"})
_pure_cache: dict[tuple, bytes] = {}
_cache_lock = threading.Lock()


class RemoteError(RuntimeError):
    """Lỗi phía web khi chạy hàm (không phải lỗi kiểu có sẵn của Python)."""


def _post(target: str, body: bytes, retries: int, timeout: float) -> bytes:
    for attempt in range(retries + 1):
        try:
            resp = _client.post(f"/api/svc/{target}", content=body, timeout=timeout)
            break
        except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
            if attempt >= retries:
                raise ConnectionError(f"Không gọi được web API ({API_URL}): {e}") from e
            time.sleep(1 + attempt)
    if resp.status_code != 200:
        raise RemoteError(f"API {target}: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.content


def _result(target: str, raw: bytes):
    out = rpc.loads(raw)
    if err := out.get("error"):
        exc = getattr(builtins, err.get("type", ""), None)
        if isinstance(exc, type) and issubclass(exc, Exception):
            raise exc(err.get("message", ""))
        raise RemoteError(f"{target} – {err.get('type')}: {err.get('message')}")
    return out.get("result")


def call(target: str, *args, _pure: bool = False, _retries: int = CONNECT_RETRIES, _timeout: float = TIMEOUT,
         **kwargs):
    """Gọi <module>.<hàm> ở web, trả kết quả (cùng kiểu Python) hoặc ném lại lỗi của hàm."""
    if _pure:  # cùng tham số → cùng kết quả: lưu bản trả về (giải mã lại mỗi lần, khỏi bị sửa chung)
        body = rpc.dumps({"args": args, "kwargs": kwargs})
        key = (target, body)
        if (raw := _pure_cache.get(key)) is None:
            raw = _post(target, body, _retries, _timeout)
            if b'"error"' not in raw[:12]:
                with _cache_lock:
                    if len(_pure_cache) >= PURE_CACHE_MAX:
                        _pure_cache.clear()
                    _pure_cache[key] = raw
        return _result(target, raw)
    body = rpc.dumps({"args": args, "kwargs": kwargs, "actor": ACTOR.get(), "source": SOURCE})
    return _result(target, _post(target, body, _retries, _timeout))


class RemoteModule(types.ModuleType):
    """Module ở web, dùng ở bot như module thường: thuộc tính = hằng số hoặc hàm gọi API."""

    def __init__(self, name: str):
        super().__init__(name, f"{name} (ở web, gọi qua API)")
        self._info = None

    def _load(self) -> dict:
        if self._info is None:
            deadline = time.monotonic() + STARTUP_WAIT
            while True:
                try:
                    self._info = call(f"{self.__name__}.__info__", _retries=0)
                    break
                except ConnectionError as e:
                    if time.monotonic() > deadline:
                        raise
                    log.warning("Chờ web API: %s", e)
                    time.sleep(3)
        return self._info

    def __getattr__(self, attr: str):
        if attr.startswith("_"):
            raise AttributeError(attr)
        info = self._load()
        if attr in info["consts"]:
            value = info["consts"][attr]
        elif attr in info["funcs"]:
            value = _make_func(f"{self.__name__}.{attr}", attr in info["async"], attr in info["pure"])
        else:
            raise AttributeError(f"web không có {self.__name__}.{attr} (hoặc chưa mở cho bot: web.SVC_DB_FUNCS)")
        setattr(self, attr, value)  # lần sau khỏi qua __getattr__
        return value


def _make_func(target: str, is_async: bool, pure: bool):
    if is_async:
        async def afn(*args, **kwargs):
            return await asyncio.to_thread(call, target, *args, _timeout=ASYNC_TIMEOUT, **kwargs)
        afn.__name__ = target
        return afn

    def fn(*args, **kwargs):
        return call(target, *args, _pure=pure, **kwargs)
    fn.__name__ = target
    return fn


def init_db():
    """bot.main() gọi lúc khởi động: database do web lo; ở đây chỉ kiểm tra cấu hình + kết nối."""
    if not API_URL or not SERVICE_TOKEN:
        raise RuntimeError("Thiếu API_URL / SERVICE_TOKEN trong .env")
    sys.modules["db"]._load()


def install():
    """Đặt các module ở web vào sys.modules → `import db`, `from utils import money` … trong bot dùng bản qua API."""
    for name in MODULES:
        if not isinstance(sys.modules.get(name), RemoteModule):
            sys.modules[name] = RemoteModule(name)
    db = sys.modules["db"]
    db.ACTOR = ACTOR  # bot đặt db.ACTOR (người đổi) và db.SOURCE như với db.py
    db.SOURCE = SOURCE
    db.init_db = init_db
