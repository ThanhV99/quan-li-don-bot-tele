"""Bot không có logic nghiệp vụ / database: code của db, tracking, sale_text, utils, alert_rules, net nằm ở web.
install() đặt vào sys.modules các module cùng tên, nên code bot giữ nguyên `import db`, `from utils import money` …

- db: module "giả", mỗi `db.<hàm>(...)` = POST {API_URL}/api/svc/db.<hàm> (token SERVICE_TOKEN); hằng số (STATUS_*,
  TABLES …) lấy 1 lần (db.__info__). Hàm thuần của db (name_key, norm_phone) chạy tại chỗ (bản trong utils).
- utils, alert_rules, net, sale_text, tracking, track_text (LOCAL_MODULES): tải MÃ NGUỒN từ web (<module>.__source__) lúc khởi
  động rồi chạy tại chỗ (web ở Render: money(), is_supported() … gọi qua mạng thì /ds mất vài chục giây). Code vẫn chỉ
  nằm ở web. Sau khi nạp, mọi hằng số được ghi đè bằng giá trị bên web (<module>.__info__: kể cả lấy từ .env của web:
  VC_*, ADDR_ALIASES); hàm KHÔNG thuần (tracking: tra hãng, đọc/ghi hành trình, theo giờ) thay bằng lời gọi API.
  Web đổi code (code_version khác) → code_changed() → bot thoát để Docker chạy lại với code mới (bot.heartbeat_loop).
- shipwatch: như db (mọi hàm gọi API): kiểm tra vận chuyển + alert chạy ở web, bot chỉ gọi run_pass / delivering_messages.
- Hàm async ở web (tracking.track_saved) thành hàm async ở bot, chạy HTTP trong thread → không chặn vòng lặp bot.
- Hàm db CHỈ ĐỌC (__info__ "read": get, list_*, phone_by_number …) được nhớ trong 1 LƯỢT xử lý tin nhắn / nút bấm
  (begin_request() ở bot.check_access): web ở xa (Render) thì /ds đỡ hàng trăm lượt gọi qua mạng. Lệnh ghi hoặc hàm
  không thuần (tra vận đơn) nào cũng xóa đệm → không đọc phải dữ liệu cũ. Vòng chạy nền (watcher) không có đệm.
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

from urllib.parse import urlsplit

import httpx

import rpc

log = logging.getLogger("remote")

API_URL = os.getenv("API_URL", "").rstrip("/")
SERVICE_TOKEN = os.getenv("SERVICE_TOKEN", "")
TIMEOUT = float(os.getenv("API_TIMEOUT", "30"))
ASYNC_TIMEOUT = 180  # tra vận đơn: chờ lượt API hãng (throttle) + hãng trả chậm
CONNECT_RETRIES = 3  # web đang khởi động lại (deploy) → thử lại vài lần
STARTUP_WAIT = 120  # lần đầu lấy thông tin module: chờ web lên tối đa chừng này giây
REMOTE_MODULES = ("db", "shipwatch")  # shipwatch: kiểm tra vận chuyển + alert chạy ở web (/kiemtravc, /danggiao)
LOCAL_MODULES = ("utils", "alert_rules", "net", "sale_text", "tracking", "track_text")  # thứ tự nạp = thứ tự import giữa chúng
INFO_MODULES = ("utils", "alert_rules", "sale_text", "tracking")  # có __info__ (hằng số + hàm thuần / không thuần)
PURE_CACHE_MAX = 20000

# giống db.py: nguồn theo tiến trình, người đổi theo từng lượt (gửi kèm mỗi lời gọi → nhật ký đổi trạng thái)
SOURCE = "bot"
ACTOR: contextvars.ContextVar[str | None] = contextvars.ContextVar("db_actor", default=None)
_REQ_CACHE: contextvars.ContextVar[dict | None] = contextvars.ContextVar("remote_req_cache", default=None)

# web ở xa (Vercel / Render) mà máy chạy bot phải qua proxy công ty: đi thẳng trước, lỗi kết nối thì thử PROXY_URL và
# nhớ đường chạy được (giống tg_net / net). Web cùng máy (http://quan-li-don-web:8000) thì không dùng proxy.
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
ROUTE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)


def _new_client(proxy: str | None) -> httpx.Client:
    return httpx.Client(base_url=API_URL, timeout=httpx.Timeout(TIMEOUT, connect=8), trust_env=False, proxy=proxy,
                        headers={"X-Service-Token": SERVICE_TOKEN, "Content-Type": "application/json"})


_LOCAL_API = urlsplit(API_URL).hostname in ("quan-li-don-web", "127.0.0.1", "localhost")
_routes = [None] + ([PROXY_URL] if PROXY_URL and not _LOCAL_API else [])
_clients = {r: _new_client(r) for r in _routes}
_client = _clients[None]
_pure_cache: dict[tuple, bytes] = {}
_cache_lock = threading.Lock()
CODE_VERSION: str | None = None  # phiên bản code module thuần đã nạp (web tính từ mã nguồn)


class RemoteError(RuntimeError):
    """Lỗi phía web khi chạy hàm (không phải lỗi kiểu có sẵn của Python)."""


def _send(target: str, body: bytes, timeout: float) -> httpx.Response:
    """Gửi theo đường đang dùng; lỗi đường mạng → thử đường còn lại (thẳng ↔ proxy) và nhớ đường chạy được."""
    global _routes
    last = None
    for route in list(_routes):
        try:
            resp = _clients[route].post(f"/api/svc/{target}", content=body, timeout=httpx.Timeout(timeout, connect=8))
        except ROUTE_ERRORS as e:
            last = e
            continue
        if route != _routes[0]:
            _routes = [route] + [r for r in _routes if r != route]
            log.info("Web API: chuyển sang %s", f"proxy {route}" if route else "kết nối trực tiếp")
        return resp
    raise last


def _post(target: str, body: bytes, retries: int, timeout: float) -> bytes:
    for attempt in range(retries + 1):
        try:
            resp = _send(target, body, timeout)
            break
        except (*ROUTE_ERRORS, httpx.RemoteProtocolError) as e:
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


def begin_request():
    """Đầu mỗi tin nhắn / nút bấm (bot.check_access): đệm mới cho các hàm db chỉ đọc của lượt này."""
    _REQ_CACHE.set({})


def call(target: str, *args, _pure: bool = False, _read: bool = False, _retries: int = CONNECT_RETRIES,
         _timeout: float = TIMEOUT, **kwargs):
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
    cache = _REQ_CACHE.get()
    if cache is not None:
        if not _read:  # lệnh ghi / tra vận đơn … → dữ liệu có thể đã đổi
            cache.clear()
        elif (raw := cache.get((target, body))) is not None:
            return _result(target, raw)
    raw = _post(target, body, _retries, _timeout)
    if cache is not None and _read and b'"error"' not in raw[:12]:
        cache[(target, body)] = raw
    return _result(target, raw)


class RemoteModule(types.ModuleType):
    """Module ở web, dùng ở bot như module thường: thuộc tính = hằng số hoặc hàm gọi API."""

    def __init__(self, name: str):
        super().__init__(name, f"{name} (ở web, gọi qua API)")
        self._info = None

    def _load(self) -> dict:
        if self._info is None:
            self._info = _call_waiting(f"{self.__name__}.__info__")
        return self._info

    def __getattr__(self, attr: str):
        if attr.startswith("_"):
            raise AttributeError(attr)
        info = self._load()
        if attr in info["consts"]:
            value = info["consts"][attr]
        elif (local := info.get("local", {}).get(attr)) and local in sys.modules:  # vd db.name_key → utils.name_key
            value = getattr(sys.modules[local], attr)
        elif attr in info["funcs"]:
            value = _make_func(f"{self.__name__}.{attr}", attr in info["async"], attr in info["pure"],
                               attr in info.get("read", ()))
        else:
            raise AttributeError(f"web không có {self.__name__}.{attr} (hoặc chưa mở cho bot: web.SVC_DB_FUNCS)")
        setattr(self, attr, value)  # lần sau khỏi qua __getattr__
        return value


def _make_func(target: str, is_async: bool, pure: bool, read: bool = False):
    if is_async:
        async def afn(*args, **kwargs):
            return await asyncio.to_thread(call, target, *args, _timeout=ASYNC_TIMEOUT, **kwargs)
        afn.__name__ = target
        return afn

    def fn(*args, **kwargs):
        return call(target, *args, _pure=pure, _read=read, **kwargs)
    fn.__name__ = target
    return fn


def _call_waiting(target: str):
    """Gọi lúc khởi động: web chưa lên (deploy, Render đang thức dậy) thì chờ tối đa STARTUP_WAIT giây."""
    deadline = time.monotonic() + STARTUP_WAIT
    while True:
        try:
            return call(target, _retries=0)
        except ConnectionError as e:
            if time.monotonic() > deadline:
                raise
            log.warning("Chờ web API: %s", e)
            time.sleep(3)


def _load_local(name: str):
    """Tải mã nguồn module từ web, chạy tại chỗ; ghi đè hằng số bằng giá trị bên web; hàm không thuần → gọi API."""
    global CODE_VERSION
    src = _call_waiting(f"{name}.__source__")
    CODE_VERSION = src["code_version"]
    mod = types.ModuleType(name, f"{name} (mã nguồn từ web)")
    mod.__file__ = f"<web>/{name}.py"
    sys.modules[name] = mod  # trước exec: module khác import lẫn nhau
    exec(compile(src["source"], mod.__file__, "exec"), mod.__dict__)
    if name in INFO_MODULES:
        info = _call_waiting(f"{name}.__info__")
        for k, v in info["consts"].items():  # vd alert_rules.ALERT_KEYWORDS theo VC_* trong .env của web
            setattr(mod, k, v)
        for fn in info["funcs"]:
            if fn not in info["pure"]:  # tra hãng, đọc / ghi hành trình, theo giờ hiện tại → chạy ở web
                setattr(mod, fn, _make_func(f"{name}.{fn}", fn in info["async"], False))
    return mod


def code_changed() -> bool:
    """Web đã đổi code module thuần so với bản bot đang chạy (bot nên khởi động lại để nạp code mới)."""
    try:
        return CODE_VERSION is not None and call("db.__info__", _retries=0)["code_version"] != CODE_VERSION
    except Exception:  # noqa: BLE001 – lỗi mạng: coi như chưa đổi
        return False


def init_db():
    """bot.main() gọi lúc khởi động: database do web lo; ở đây chỉ kiểm tra cấu hình + kết nối."""
    if not API_URL or not SERVICE_TOKEN:
        raise RuntimeError("Thiếu API_URL / SERVICE_TOKEN trong .env")
    sys.modules["db"]._load()


def install(load_code: bool = True):
    """Đặt các module vào sys.modules → `import db`, `from utils import money` … trong bot dùng bản của web.
    load_code=False: chỉ db qua API (healthcheck của Docker, khỏi tải mã nguồn mỗi phút)."""
    for name in REMOTE_MODULES:
        if not isinstance(sys.modules.get(name), RemoteModule):
            sys.modules[name] = RemoteModule(name)
    db = sys.modules["db"]
    db.ACTOR = ACTOR  # bot đặt db.ACTOR (người đổi) và db.SOURCE như với db.py
    db.SOURCE = SOURCE
    db.init_db = init_db
    if load_code:
        for name in LOCAL_MODULES:
            if getattr(sys.modules.get(name), "__file__", "") != f"<web>/{name}.py":
                _load_local(name)
        tracking = sys.modules["tracking"]
        rpc.register(tracking.TrackResult, tracking.TrackEvent)  # hành trình từ web → đúng lớp của tracking tại chỗ
