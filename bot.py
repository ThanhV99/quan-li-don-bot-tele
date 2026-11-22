import asyncio
import csv
import html
import io
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv
from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

load_dotenv()  # trước `import remote` (đọc API_URL / SERVICE_TOKEN lúc import)
import imgmerge  # noqa: E402
import remote  # noqa: E402
import tg_net  # noqa: E402

# bot không có logic nghiệp vụ / database: các module dưới đây nằm ở web, gọi qua API (remote.py; cần
# API_URL + SERVICE_TOKEN). install() phải chạy trước các import này.
remote.install()
import alert_rules  # noqa: E402
import db  # noqa: E402
import sale_text  # noqa: E402
import tracking  # noqa: E402
from utils import (addr_groups, money, money_short, parse_money, parse_pct, parse_range, pct_text, price_after_discount, range_label,
                   range_short, social_link)  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
# Tự động kiểm tra vận chuyển các đơn "Đã đặt"; khớp cụm từ trong alert_rules.py thì bot alert nhắn hành trình
ALERT_BOT_TOKEN = os.getenv("ALERT_BOT_TOKEN", "")
ALERT_CHAT_IDS = [int(x) for x in os.getenv("ALERT_CHAT_IDS", "").replace(" ", "").split(",") if x]
TRACK_INTERVAL_HOURS = float(os.getenv("TRACK_INTERVAL_HOURS", "12"))

BOT_USERNAME = ""  # gán khi bot khởi động (post_init), dùng để tạo link t.me/<bot>?start=...
ALLOWED_USER_IDS = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}


# Các bước nhập liệu: (trường, kiểu, câu hỏi, lựa chọn, bắt buộc)
#   kiểu: "text" | "money" | "choice" (nút bấm) | "suggest" (bàn phím gợi ý, vẫn gõ tự do được)
#         | "pick_text" (nút bấm ngay dưới câu hỏi, vẫn gõ tự do được; lựa chọn = [(tên, đang sale)])
#         | "pick_id" (nút bấm, lưu id; lựa chọn = [(id, nhãn)], không có lựa chọn nào → bỏ qua bước)
ORDER_STEPS = [
    # gợi ý người đặt hay dùng (bảng nguoi_dat, /nguoidat) – cùng tên thì dùng chung ảnh QR
    ("ten_nguoi_dat", "suggest", "👤 Tên người đặt? (chọn gợi ý hoặc gõ tên mới)", lambda: db.person_names(), True),
    ("nguon_khach", "choice", "📲 Khách đến từ đâu?", ["Zalo", "Facebook"], True),
    # gợi ý tên từ danh mục sản phẩm (/sanpham), vẫn gõ tên mới được
    ("ten_sp", "pick_text", "📦 Tên sản phẩm? (bấm chọn hoặc gõ tên mới, 🔥 = đang sale)", db.product_picks, True),
    ("so_luong", "int", "🔢 Số lượng? (gõ - để bỏ qua)", None, False),
    ("nguon_don", "choice", "🛒 Đặt hàng từ sàn nào?", ["Shopee", "TikTok"], True),
    # chiến dịch thưởng săn sale chưa kết thúc; không có chiến dịch nào thì bỏ qua bước này
    ("chien_dich", "pick_id", "🎁 Đơn thuộc chiến dịch thưởng nào?", lambda: db.campaign_choices(), False),
    ("gia_dat", "money", "💸 Giá đặt? (tính theo nghìn: 150 = 150.000, 1.2tr = 1.200.000)", None, True),
    ("gia_thu", "money", "💰 Giá shop thu? (chưa bán thì gõ 0 hoặc -, bán được thì /ban sau)", None, False),
    ("cong_dat", "money", "🛠 Công đặt? (gõ - nếu không có)", None, False),
    # người đặt đã có → tên trên đơn lần trước thành nút gợi ý (ask_step)
    ("ten_tren_don", "text", "🪪 Tên trên đơn hàng? (tên người đặt ghi trên đơn Shopee/TikTok, gõ - để bỏ qua)", None, False),
    # gợi ý các số hay dùng trong danh bạ SĐT (/sdt), vẫn gõ số mới được
    ("sdt", "suggest", "📞 SĐT người đặt? (chọn số gợi ý hoặc gõ số mới, gõ - để bỏ qua)",
     lambda: recent_phones(), False),
    ("dia_chi", "text", "🏠 Địa chỉ? (gõ - để bỏ qua)", None, False),
    ("ma_don", "text", "🔖 Mã đơn hàng? (gõ - để bỏ qua)", None, False),
    # lựa chọn lấy từ bảng carriers (quản lý bằng /dvvc)
    ("don_vi_vc", "choice", "🚚 Đơn vị vận chuyển?", db.carrier_names, False),
    ("qr_file_id", "photo", "📷 Gửi ảnh QR để trả công người đặt (chưa có thì bấm Bỏ qua, gửi sau bằng /qr)",
     None, False),
]
SALE_STEPS = [
    ("ten_sp", "pick_text", "📦 Tên sản phẩm? (bấm chọn hoặc gõ tên mới, 🔥 = đang sale)", db.product_picks, True),
    # 1 sản phẩm nhiều shop: mỗi shop thêm 1 món riêng (giá / link riêng), /sale gộp theo tên sản phẩm
    ("shop", "suggest", "🏪 Shop nào? (chọn gợi ý hoặc gõ tên, gõ - để bỏ qua)", lambda: db.shop_names(), False),
    ("gia_goc", "money", "🏷 Giá gốc? (giá trước giảm, vd 5tr2; gõ - nếu không có)", None, False),
    ("gia_sale", "range", "💰 Giá mua được? (vd: 5000 hoặc 5tr → hiện “dưới 5tr”; khoảng giá: 4tr2 - 4tr4; "
                          "có giá gốc thì gõ % giảm, vd <code>10%</code>)", None, True),
    ("cong", "range", "🛠 Công? (vd: 70 → hiện “từ 70 trở lên”; khoảng: 150-200k; gõ - nếu không có)", None, False),
    ("muc_gia", "tiers", "💰 Mức giá khác? (vd: <code>dưới 4tr5 công 100</code>; nhiều mức thì mỗi dòng 1 mức "
                         "hoặc cách nhau bằng <code>;</code>; gõ - nếu không có)", None, False),
    ("so_luong", "int", "🔢 Số lượng? (gõ - nếu không rõ)", None, False),
    ("nhom", "int", "🗂 Nhóm sale? (1, 2… → xem riêng bằng <code>/sale 1</code>; gõ - nếu không chia nhóm)", None, False),
    ("link", "links", "🔗 Gửi link sản phẩm – gửi lần lượt từng link, hoặc nhiều link 1 lần.\n"
                      "Xong thì bấm <b>✅ Xong</b> (hoặc gõ <code>xong</code>). Không có link thì gõ <code>-</code>",
     None, False),
]
# sản phẩm không có luồng /them riêng; các bước chỉ dùng cho /suasp (bắt buộc / kiểu giá)
PRODUCT_STEPS = [
    ("ten", "text", "📦 Tên sản phẩm?", None, True),
    ("gia_goi_y", "range", "📈 Giá thu gợi ý? (vd: 4tr7, khoảng 4tr7 - 4tr750)", None, False),
]
# Mỗi luồng nhập liệu ứng với một bảng trong db
FLOWS = {
    "orders": {"steps": ORDER_STEPS, "name": "đơn", "intro": "📝 <b>Thêm đơn mới</b>"},
    "sales": {"steps": SALE_STEPS, "name": "món sale", "intro": "🔥 <b>Thêm món săn sale</b>"},
    "products": {"steps": PRODUCT_STEPS, "name": "sản phẩm", "intro": "📦 <b>Thêm sản phẩm</b>"},
    # chỉ dùng cho sửa (tên hiển thị + trường bắt buộc)
    "nguoi_dat": {"steps": [("ten", "text", "", None, True)], "name": "người đặt"},
    "carriers": {"steps": [("ten", "text", "", None, True)], "name": "đơn vị vận chuyển"},
    "phones": {"steps": [], "name": "số điện thoại"},
    "addresses": {"steps": [("noi_dung", "text", "", None, True)], "name": "địa chỉ"},
}
ASKING, CONFIRM = range(2)


# ---------- tiện ích ----------

def parse_bool(text: str) -> int | None:
    t = text.strip().lower()
    if t in ("1", "on", "co", "có", "con", "còn", "true", "yes"):
        return 1
    if t in ("0", "off", "khong", "không", "het", "hết", "false", "no"):
        return 0
    return None


STATUS_ICON = {db.STATUS_ORDERED: "⏳", db.STATUS_RECEIVED: "📦", db.STATUS_PAID: "💵", db.STATUS_DONE: "✅",
               db.STATUS_CANCELED: "❌"}
STATUS_ALIASES = {
    **dict.fromkeys(["dat", "da dat", "đặt", "đã đặt"], db.STATUS_ORDERED),
    **dict.fromkeys(["nhan", "da nhan", "nhận", "đã nhận", "danhan"], db.STATUS_RECEIVED),
    **dict.fromkeys(["tt", "thanh toan", "thanh toán", "thanhtoan"], db.STATUS_PAID),
    **dict.fromkeys(["xong", "hoan thanh", "hoàn thành", "hoanthanh", "ban", "bán"], db.STATUS_DONE),
    **dict.fromkeys(["huy", "hủy", "da huy", "đã hủy"], db.STATUS_CANCELED),
}


def parse_status(text: str) -> str | None:
    return STATUS_ALIASES.get(text.strip().lower())


def status_text(status) -> str:
    status = status or db.STATUS_ORDERED
    return f"{STATUS_ICON.get(status, '')} {status}"


def match_option(text: str, options) -> str | None:
    """Khớp không phân biệt hoa thường, trả về đúng cách viết của lựa chọn."""
    return next((o for o in options if o.lower() == text.lower()), None)


def margin(thu, dat, cong) -> int:
    """Chênh lệch = giá thu − (giá đặt + công đặt)."""
    return (thu or 0) - ((dat or 0) + (cong or 0))


def margin_text(thu, dat, cong) -> str:
    """Chênh lệch dạng chữ; đơn chưa bán (giá shop thu = 0) thì không tính."""
    return money(margin(thu, dat, cong)) if thu else "chưa bán"


def link_html(url) -> str:
    """1 hoặc nhiều link (cách nhau xuống dòng) → các link bấm được."""
    urls = (url or "").split()
    if not urls:
        return ""
    if not all(u.startswith(("http://", "https://")) for u in urls):
        return html.escape(url)
    return " ".join(f'<a href="{html.escape(u, quote=True)}">link{i + 1 if len(urls) > 1 else ""}</a>'
                    for i, u in enumerate(urls))


def money_range(lo, hi, field=None) -> str:
    if label := range_label(lo, hi, field, "money"):
        return label
    return f"{money(lo)} – {money(hi)}" if hi and hi != lo else money(lo)


def fmt_record(table: str, r, rec_id=None, created_at=None) -> str:
    get = r.get if isinstance(r, dict) else (lambda k: r[k])
    name = FLOWS[table]["name"]
    head = f"🧾 <b>{name.capitalize()} #{rec_id}</b>" if rec_id else f"🧾 <b>Xác nhận {name}</b>"
    if table == "phones":
        head += f" · <code>{get('sdt')}</code>"
    if created_at:
        head += f"  <i>({created_at})</i>"
    lines = [head]
    for field, label in db.TABLES[table]:
        val = get(field)
        if field in db.RANGE_FIELDS.values():
            continue  # đã gộp vào trường chính bên dưới
        if field in db.RANGE_FIELDS:
            val = money_range(val, get(db.RANGE_FIELDS[field]), field)
            lines.append(f"• {label}: <b>{val}</b>")
            continue
        if field == "active":
            val = "✅ Còn sale" if val in (None, 1) else "⛔ Hết sale"
        elif field == "da_su_dung":
            val = f"{PHONE_BUSY} Đang dùng" if val else f"{PHONE_FREE} Free"
        elif field == "cua_toi":
            val = f"{PHONE_MINE} Của tôi" if val else "👤 Người khác"
        elif field == "ha_noi":
            val = {1: "📍 Ở Hà Nội", 0: "Không ở Hà Nội"}.get(val)
        elif field == "giam_pt":
            val = pct_text(val) if val else None
        elif field == "uu_tien":
            val = f"số {val}" if val else "chưa xếp (sau cùng)"
        elif field == "nhom":
            val = f"🗂 nhóm {val}" if val else "chưa chia nhóm"
        elif field == "chien_dich":
            val = f"🎁 {db.campaign_name(val) or '#' + str(val)}" if val else None
        elif field in ("zalo", "facebook") and val:
            lines.append(f'• {label}: <a href="{html.escape(val)}">{html.escape(val)}</a>')
            continue
        elif field == "trang_thai":
            val = status_text(val)
        elif field in db.MONEY_FIELDS:
            val = money(val)
        elif field == "link" and val:
            lines.append(f"• {label}: {link_html(val)}")
            continue
        elif field == "ma_don" and val and rec_id:
            # r có thể là dict dữ liệu vừa nhập (chưa có id / thiếu trường) → ghép đủ khóa cho tracking_html
            lines.append(f"• {label}: {tracking_html({'id': rec_id, 'ma_don': val, 'don_vi_vc': get('don_vi_vc')})}")
            continue
        lines.append(f"• {label}: <b>{html.escape(str(val)) if val not in (None, '') else '—'}</b>")
    if table == "orders":
        lines.append(f"• Chênh lệch (thu − đặt − công): "
                     f"<b>{margin_text(get('gia_thu'), get('gia_dat'), get('cong_dat'))}</b>")
        has_qr = get("qr_file_id") if isinstance(r, dict) else ("qr_file_id" in r.keys() and r["qr_file_id"])
        qr_hint = f" – xem: /qr {rec_id}" if rec_id and has_qr else ""
        lines.append(f"• QR trả công: <b>{'📷 có' if has_qr else '—'}</b>{qr_hint}")
    return "\n".join(lines)


def tracking_html(r) -> str:
    """Mã vận đơn: hãng tra cứu được (SPX) thì là link bấm vào để bot trả hành trình."""
    code = r["ma_don"]
    if not code:
        return "—"
    esc = html.escape(code)
    if BOT_USERNAME and r["id"] and tracking.is_supported(code, r["don_vi_vc"]):
        return f'<a href="https://t.me/{BOT_USERNAME}?start=tr_{r["id"]}"><b>{esc}</b></a> 🔍'
    if url := tracking.web_url(code, r["don_vi_vc"]):  # hãng có captcha: copy mã + link web điền sẵn mã
        return f'<b><code>{esc}</code></b> <a href="{html.escape(url)}">🌐 tra web</a>'
    return f"<b><code>{esc}</code></b>"


def fmt_order_short(r) -> str:
    """Khối tóm tắt 1 đơn cho /ds và /tim: thông tin quan trọng in đậm, SĐT/mã vận đơn bấm để copy."""
    e = lambda v: html.escape(v) if v else "—"
    copy = lambda v: f"<b><code>{html.escape(v)}</code></b>" if v else "—"
    c = r["created_at"]  # YYYY-MM-DD HH:MM:SS
    status = r["trang_thai"] or db.STATUS_ORDERED
    if r["gia_thu"]:
        lai = margin(r["gia_thu"], r["gia_dat"], r["cong_dat"])
        sold = f"💰 Shop thu <b>{money(r['gia_thu'])}</b>  →  lãi <b>{'+' if lai >= 0 else ''}{money(lai)}</b>"
    else:
        sold = "💰 <i>Chưa bán</i>"
    return (f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
            f"{STATUS_ICON.get(status, '')} <b>#{r['id']} · {status.upper()}</b>   <i>{c[8:10]}/{c[5:7]} {c[11:16]}</i>\n"
            f"👤 <b>{e(r['ten_nguoi_dat'])}</b>"
            + (f"  🪪 <i>{e(r['ten_tren_don'])}</i>" if "ten_tren_don" in r.keys() and r["ten_tren_don"] else "")
            + (f"  🎁 <i>{e(db.campaign_name(r['chien_dich']))}</i>" if "chien_dich" in r.keys() and r["chien_dich"] else "")
            + f"   📞 {copy(r['sdt'])}{phone_tag(r['sdt'])}\n"
            + (f"🏠 {e(r['dia_chi'])}\n" if r["dia_chi"] else "")
            + f"📦 <b>{e(r['ten_sp'])}</b>{qty_text(r)}  <i>({e(r['nguon_don'])})</i>\n"
            f"💸 Đặt <b>{money(r['gia_dat'])}</b> + công <b>{money(r['cong_dat'])}</b>\n"
            f"{sold}\n"
            f"🚚 {e(r['don_vi_vc'])}   🔖 {tracking_html(r)}"
            + pay_line(r))


def order_qty(r) -> int:
    get = r.get if isinstance(r, dict) else (lambda k: r[k])
    return get("so_luong") or 1


def price_total(r, unit_price: int) -> tuple[int, str]:
    """Giá shop thu nhập theo 1 cái → tổng cả đơn = giá × số lượng. Trả về (tổng, chữ xác nhận)."""
    qty = order_qty(r)
    total = unit_price * qty
    return total, (f"{money(unit_price)} × {qty} = {money(total)}" if qty > 1 and unit_price else money(total))


def suggested_price(r) -> tuple[tuple | None, str]:
    """Giá thu gợi ý 1 cái của sản phẩm (/sanpham). Trả về ((thấp, cao), chữ hiển thị) hoặc (None, "")."""
    get = r.get if isinstance(r, dict) else (lambda k: r[k])
    lo, hi = db.product_price(get("ten_sp"))
    if not lo:
        return None, ""
    return (lo, hi), range_short(lo, hi) + (" / 1 cái" if order_qty(r) > 1 else "")


def unit_price_hint(r) -> str:
    """Nhắc nhập giá 1 cái khi đơn có nhiều cái (+ bán 1 phần: thêm số cái, vd 2tr65 1c)."""
    qty = order_qty(r)
    return (f"\n🔢 Đơn {qty} cái: gõ giá <b>1 cái</b>, bot tự nhân × {qty}. Chỉ bán vài cái thì thêm số cái, "
            f"vd <code>2tr65 1c</code>: phần còn lại tách thành đơn mới." if qty > 1 else "")


# số cái bán ở cuối giá: "2tr65 1c", "2tr65 1 cái", "2tr65 x1", "2tr65 sl1" (phải có c / cái / x / sl, để khỏi lẫn với giá)
SELL_QTY_RE = re.compile(r"\s+(?:(?:x|sl)\s*(\d+)|(\d+)\s*(?:c|cái|cai))\s*$", re.I)


def split_sell_qty(text: str) -> tuple[str, int | None]:
    """"2tr65 1c" → ("2tr65", 1); không ghi số cái → (text, None)."""
    m = SELL_QTY_RE.search(" " + text.strip())
    if not m:
        return text, None
    return (" " + text.strip())[:m.start()].strip(), int(m.group(1) or m.group(2))


def phone_tag(raw) -> str:
    """Sau SĐT của đơn: số của tôi thì ghi lắp ở máy nào (theo danh bạ /sdt). Danh sách số của tôi lấy 1 lần mỗi
    lượt (db.my_phones, đệm theo lượt) thay vì tra từng số qua API."""
    mine = db.my_phones()
    norm = db.norm_phone(raw)
    if not norm or norm not in mine:
        return ""
    return f" · {PHONE_MINE} <i>{html.escape(mine[norm]) if mine[norm] else 'số của tôi'}</i>"


def qty_text(r) -> str:
    """Số lượng sau tên sản phẩm: " x2" (không ghi thì bỏ trống)."""
    return f" x{r['so_luong']}" if r["so_luong"] else ""


def pay_line(r) -> str:
    """Đơn Đã đặt / Đã nhận: link "Thanh toán" (t.me/<bot>?start=pay_<id>) mở QR + nút Đã trả công;
    Đã đặt thêm link "Đã nhận hàng" (start=nhan_<id>) → Đã nhận (nhận hàng rồi, chưa trả công)."""
    if r["trang_thai"] in db.UNPAID_STATUSES and BOT_USERNAME:
        qr = " 📷" if r["qr_file_id"] else " <i>(chưa có QR)</i>"
        nhan = (f'\n📦 <a href="https://t.me/{BOT_USERNAME}?start=nhan_{r["id"]}"><b>ĐÃ NHẬN HÀNG</b></a> (chưa trả công)'
                if r["trang_thai"] == db.STATUS_ORDERED else "")
        return (f'{nhan}\n💳 <a href="https://t.me/{BOT_USERNAME}?start=pay_{r["id"]}"><b>THANH TOÁN CÔNG '
                f'{money(r["cong_dat"])}</b></a>{qr}')
    if r["trang_thai"] == db.STATUS_PAID and BOT_USERNAME:  # đã trả công → bán xong thì Hoàn thành
        return f'\n✅ <a href="https://t.me/{BOT_USERNAME}?start=done_{r["id"]}"><b>HOÀN THÀNH (ĐÃ BÁN)</b></a>'
    return f"\n📷 QR trả công: /qr {r['id']}" if r["qr_file_id"] else ""


def fmt_sale_table(rows, show_status: bool) -> list[str]:
    """Bảng quản lý săn sale (có id). Trả về list dòng."""
    lines = []
    for r in rows:
        status = ("✅ " if r["active"] else "⛔ ") if show_status else ""
        cong = f" · công {range_short(r['cong'], r['cong_max'], 'cong')}" if r["cong"] or r["cong_max"] else ""
        sl = f" · SL <b>{r['so_luong']}</b>" if r["so_luong"] is not None else ""
        link = f" · {link_html(r['link'])}" if r["link"] else ""
        shop = f" · 🏪 {html.escape(r['shop'])}" if r["shop"] else ""
        lines.append(f"{status}<code>#{r['id']}</code> {html.escape(r['ten_sp'])}{shop} — "
                     f"<b>{range_short(r['gia_sale'], r['gia_sale_max'], 'gia_sale')}</b>{cong}{sl}{link}")
    return lines


async def reply_long(message, lines: list[str]):
    """Gửi nhiều dòng, tự cắt nếu vượt giới hạn 4096 ký tự của Telegram."""
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 4000:
            await message.reply_html(chunk, disable_web_page_preview=True)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await message.reply_html(chunk, disable_web_page_preview=True)


def parse_id(context) -> int | None:
    return int(context.args[0]) if context.args and context.args[0].isdigit() else None


# ---------- phân quyền ----------

async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # chỉ admin trong .env (ALLOWED_USER_IDS) dùng được bot này; người nhận alert chỉ nhận tin qua bot alert
    remote.begin_request()  # đệm hàm db chỉ đọc cho lượt này (web ở xa → đỡ gọi lại qua mạng)
    user = update.effective_user
    if user is not None:  # ghi vào nhật ký đổi trạng thái đơn (db.order_status_log) ai đã đổi
        db.ACTOR.set(user.full_name or user.username or str(user.id))
    if user is None or user.id not in ALLOWED_USER_IDS:
        if update.effective_message:
            await update.effective_message.reply_text(
                f"⛔ Bạn không có quyền dùng bot. User ID của bạn: {user.id if user else '?'}")
        raise ApplicationHandlerStop


# ---------- nhập liệu từng bước (dùng chung cho đơn hàng và săn sale) ----------

def current_steps(context):
    return FLOWS[context.user_data["table"]]["steps"]


async def ask_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    steps = current_steps(context)
    idx = context.user_data["step"]
    field, kind, prompt, options, required = steps[idx]
    progress = f"<i>({idx + 1}/{len(steps)})</i> "
    msg = update.effective_message
    options = step_options(options)
    # người đặt đã có: nguồn khách lấy luôn theo đơn gần nhất; SĐT / địa chỉ gần nhất thành nút gợi ý đầu tiên
    known = db.person_defaults(context.user_data["data"].get("ten_nguoi_dat")) \
        if context.user_data["table"] == "orders" else None
    if known and field == "nguon_khach" and known["nguon_khach"] and context.user_data.get("back_to") != field:
        context.user_data["data"][field] = known["nguon_khach"]
        await msg.reply_html(f"{progress}📲 Khách từ <b>{html.escape(known['nguon_khach'])}</b> "
                             f"(theo người đặt {html.escape(known['ten'])}).", reply_markup=ReplyKeyboardRemove())
        return await next_step(update, context)
    if known and field == "sdt" and known["phones"]:
        # mọi số người này từng dùng (gần nhất trước) kèm kết quả, rồi các số hay dùng khác trong danh bạ
        values = [x["sdt"] for x in known["phones"]]
        values += [o for o in options if db.norm_phone(o) not in {db.norm_phone(v) for v in values}]
        values = values[:9]
        rows = [values[i:i + 2] for i in range(0, len(values), 2)]
        lines = [person_phone_line(x) for x in known["phones"][:9]]
        await msg.reply_html(f"{progress}{prompt}\n👤 Các số {html.escape(known['ten'])} từng dùng "
                             f"({len(known['phones'])}) – bấm nút để dùng lại:\n" + "\n".join(lines),
                             reply_markup=ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True))
        return ASKING
    if known and field in ("ten_tren_don", "sdt", "dia_chi") and known[field + "s"]:
        values = known[field + "s"]
        if field == "sdt":  # thêm các số hay dùng phía sau
            values += [o for o in options if db.norm_phone(o) not in {db.norm_phone(v) for v in values}]
        rows = [[v] for v in values[:3]] + ([values[3:6]] if len(values) > 3 else [])
        await msg.reply_html(f"{progress}{prompt}\n👤 Lần trước {html.escape(known['ten'])} dùng: "
                             f"<b>{html.escape(known[field])}</b> – bấm nút để dùng lại",
                             reply_markup=ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True))
        return ASKING
    if field == "qr_file_id":  # người đặt đã có QR lưu sẵn → dùng luôn, khỏi gửi lại
        name = context.user_data["data"].get("ten_nguoi_dat")
        if qr := db.person_qr(name):
            context.user_data["data"][field] = qr
            await msg.reply_html(f"{progress}📷 Dùng ảnh QR đã lưu của <b>{html.escape(name)}</b> "
                                 "(đổi bằng /nguoidat).", reply_markup=ReplyKeyboardRemove())
            return await next_step(update, context)
    back = context.user_data.get("back_to") == field  # /lai về bước này → hỏi lại, không tự điền
    if field == "gia_thu" and not back and (hint := suggested_price(context.user_data["data"]))[0] \
            and hint[0][1] in (None, hint[0][0]):
        # sản phẩm có 1 giá thu gợi ý trong /sanpham → điền luôn (giá 1 cái × số lượng), sửa bằng /lai hoặc /sua
        value, shown = price_total(context.user_data["data"], hint[0][0])
        context.user_data["data"][field] = value
        await msg.reply_html(f"{progress}💰 Giá shop thu: <b>{shown}</b> (theo giá gợi ý của sản phẩm, "
                             "gõ /lai để nhập giá khác).", reply_markup=ReplyKeyboardRemove())
        return await next_step(update, context)
    if field == "gia_thu" and (hint := suggested_price(context.user_data["data"]))[0]:
        # sản phẩm có giá thu gợi ý trong /sanpham → nút chọn nhanh giá 1 cái (vẫn gõ giá khác được)
        price, text = hint
        kb = ReplyKeyboardMarkup([[money_short(p) for p in price if p] + ["0"]],
                                 one_time_keyboard=True, resize_keyboard=True)
        await msg.reply_html(f"{progress}{prompt}\n📈 Giá thu gợi ý: <b>{text}</b>"
                             + unit_price_hint(context.user_data["data"]), reply_markup=kb)
        return ASKING
    if field == "gia_thu":
        prompt += unit_price_hint(context.user_data["data"])
    if kind == "choice":
        buttons = [InlineKeyboardButton(o, callback_data=f"pick:{o}") for o in options]
        rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
        if not required:
            rows.append([InlineKeyboardButton("⏭ Bỏ qua", callback_data="pick:-")])
        await msg.reply_html(progress + prompt, reply_markup=InlineKeyboardMarkup(rows))
    elif kind == "pick_id":
        if not options:  # chưa có gì để chọn → bỏ qua (đang /lai lùi về thì lùi tiếp)
            context.user_data["data"][field] = None
            if context.user_data.get("back_to") == field and idx > 0:
                context.user_data["step"] -= 1
                context.user_data["back_to"] = steps[idx - 1][0]
                return await ask_step(update, context)
            return await next_step(update, context)
        context.user_data["pick_opts"] = [v for v, _ in options]
        context.user_data["pick_labels"] = [label for _, label in options]
        rows = [[InlineKeyboardButton(f"🎁 {label}", callback_data=f"pick:#{i}")] for i, (_, label) in enumerate(options)]
        rows.append([InlineKeyboardButton("⏭ Không thuộc chiến dịch", callback_data="pick:-")])
        await msg.reply_html(progress + prompt, reply_markup=InlineKeyboardMarkup(rows))
    elif kind == "pick_text":
        # callback_data tối đa 64 byte → nút mang số thứ tự, tên lưu trong user_data
        context.user_data["pick_opts"] = [name for name, _ in options]
        buttons = [InlineKeyboardButton(("🔥 " if hot else "") + name, callback_data=f"pick:#{i}")
                   for i, (name, hot) in enumerate(options)]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        await msg.reply_html(progress + prompt, reply_markup=InlineKeyboardMarkup(rows) if rows else ReplyKeyboardRemove())
    elif kind == "suggest":
        rows = [options[i:i + 3] for i in range(0, len(options), 3)]
        kb = ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True) if rows else ReplyKeyboardRemove()
        await msg.reply_html(progress + prompt, reply_markup=kb)
    elif kind == "photo":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Bỏ qua", callback_data="pick:-")]])
        await msg.reply_html(progress + prompt, reply_markup=kb)
    elif kind == "links":
        context.user_data["links"] = []
        await msg.reply_html(progress + prompt, reply_markup=LINKS_DONE_KB)
    else:
        await msg.reply_html(progress + prompt, reply_markup=ReplyKeyboardRemove())
    return ASKING


def step_options(options) -> list:
    """Lựa chọn của 1 bước: list cố định, hoặc hàm đọc từ db (vd: db.carrier_names)."""
    return options() if callable(options) else (options or [])


LINKS_DONE_KB = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Xong", callback_data="linksdone")]])


async def finish_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = current_steps(context)[context.user_data["step"]][0]
    context.user_data["data"][field] = "\n".join(context.user_data.pop("links", [])) or None
    return await next_step(update, context)


async def on_links_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if current_steps(context)[context.user_data["step"]][1] != "links":
        return ASKING  # nút cũ
    await query.edit_message_reply_markup(None)
    return await finish_links(update, context)


async def next_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    steps = current_steps(context)
    context.user_data["step"] += 1
    if context.user_data["step"] < len(steps):
        return await ask_step(update, context)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Lưu", callback_data="save"),
        InlineKeyboardButton("❌ Hủy", callback_data="cancel"),
    ]])
    if steps[-1][1] == "suggest":
        await update.effective_message.reply_html("👌", reply_markup=ReplyKeyboardRemove())
    table = context.user_data["table"]
    data = context.user_data["data"]
    await update.effective_message.reply_html(fmt_record(table, data), reply_markup=kb,
                                              disable_web_page_preview=True)
    return CONFIRM


async def start_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, table: str):
    context.user_data.update(table=table, data={}, step=0)
    await update.message.reply_html(
        FLOWS[table]["intro"] + "\nGõ /lai để quay lại bước trước, /huy để hủy.")
    return await ask_step(update, context)


async def start_add_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút ➕ Thêm shop khác (/suasale <id>): luồng /themsale với tên sản phẩm điền sẵn, bắt đầu từ bước shop."""
    query = update.callback_query
    await query.answer()
    r = db.get("sales", int(query.data.split(":")[1]))
    if not r:
        await query.edit_message_text("Không tìm thấy món sale.")
        return ConversationHandler.END
    context.user_data.pop("pending_edit", None)
    context.user_data.update(table="sales", data={"ten_sp": r["ten_sp"]},
                             step=[f for f, *_ in SALE_STEPS].index("shop"))
    await query.edit_message_text(
        f"➕ <b>Thêm shop cho {html.escape(r['ten_sp'])}</b>\nGõ /lai để quay lại bước trước, /huy để hủy.",
        parse_mode="HTML")
    return await ask_step(update, context)


async def start_add_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await start_flow(update, context, "orders")


SALE_FORMAT_HELP = (
    "Dán danh sách theo dạng:\n"
    "/themsale\n"
    "- ram viper x1 4tr3 công 200\n"
    "https://link1\n"
    "https://link2\n\n"
    "- vga 3050 4tr-4tr2 công 150-200k\n"
    "https://link\n\n"
    "- i5 14600K 4tr8 công 70 shop Hải Nam\n"
    "https://link\n\n"
    "- i5 14600K 4tr9 công 70 shop Phong Vũ\n"
    "https://link\n\n"
    "(“shop …” đứng cuối dòng, không bắt buộc; 1 sản phẩm nhiều shop → mỗi shop 1 dòng, /sale tự gộp)\n"
    "Cả danh sách vào 1 nhóm: dòng đầu ghi <code>nhóm 1</code> → xem bằng /sale 1\n"
    "Thêm mức giá khác: dòng <code>hoặc dưới 4tr5 công 100</code> ngay dưới món\n"
    "(ghi 1 mức: giá hiện “dưới 4tr8”, công hiện “từ 70 trở lên”)"
)


async def start_add_sale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.split(maxsplit=1)
    body = text[1].strip() if len(text) > 1 else ""
    if not body:
        return await start_flow(update, context, "sales")

    if "|" in body and "\n" not in body:
        # Thêm nhanh 1 dòng: /themsale i5 12400F shop Hải Nam | 2tr5 | 100 | https://...
        parts = [p.strip() for p in body.split("|")]
        parts[0], shop = sale_text.split_shop(parts[0])
        price = parse_range(parts[1]) if len(parts) > 1 else None
        cong = parse_range(parts[2]) if len(parts) > 2 and parts[2] else (0, None)
        if len(parts) < 2 or not parts[0] or not price or not cong:
            await update.message.reply_text(
                "Cú pháp nhanh: /themsale Tên SP [shop Tên shop] | Giá mua được | Công | Link\n"
                "(Công và Link có thể bỏ trống)\n\nHoặc:\n" + SALE_FORMAT_HELP)
            return ConversationHandler.END
        items = [{
            "ten_sp": parts[0], "shop": shop,
            "gia_sale": price[0], "gia_sale_max": price[1], "cong": cong[0], "cong_max": cong[1],
            "link": "\n".join(parts[3].split()) if len(parts) > 3 and parts[3] else None,
        }]
        errors = []
    else:
        # dòng đầu "nhóm 1" → mọi món vào nhóm 1
        nhom, body = sale_text.split_group(body)
        items, errors = sale_text.parse_block(body)
        items = [{**it, "nhom": nhom} for it in items] if nhom else items

    if not items:
        await update.message.reply_text("⚠️ Không đọc được món nào.\n\n" + SALE_FORMAT_HELP)
        return ConversationHandler.END
    ids = db.insert_many("sales", items, update.effective_user.id)
    lines = [f"✅ <b>Đã lưu {len(ids)} món sale</b>\n"]
    for rec_id, it in zip(ids, items):
        n_links = len((it["link"] or "").split())
        lines.append(f"<code>#{rec_id}</code> {html.escape(sale_text.item_line(it)[2:])} · 🔗 {n_links}")
    if errors:
        lines.append("\n⚠️ <b>Dòng không đọc được:</b>")
        lines += [f"• {html.escape(e)}" for e in errors]
    nhom = items[0].get("nhom") if items else None
    lines.append(f"\nXem danh sách: /sale {nhom}" if nhom else "\nXem danh sách: /sale")
    await reply_long(update.message, lines)
    return ConversationHandler.END


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field, kind, _, options, required = current_steps(context)[context.user_data["step"]]
    text = update.message.text.strip()

    if kind == "photo" and text != "-":
        await update.message.reply_text("⚠️ Vui lòng gửi ảnh QR, hoặc bấm ⏭ Bỏ qua.")
        return ASKING

    if kind == "links":
        links = context.user_data.setdefault("links", [])
        if text.lower() in ("xong", "done", "ok", "-"):
            return await finish_links(update, context)
        urls = sale_text.URL_RE.findall(text)
        if not urls:
            await update.message.reply_text("⚠️ Không thấy link nào. Gửi link (https://...) hoặc bấm ✅ Xong.",
                                            reply_markup=LINKS_DONE_KB)
            return ASKING
        links += [u for u in urls if u not in links]
        await update.message.reply_text(
            f"✔️ Đã nhận {len(urls)} link (tổng {len(links)}). Gửi thêm link, hoặc bấm ✅ Xong.",
            reply_markup=LINKS_DONE_KB)
        return ASKING

    if text == "-":
        if required:
            await update.message.reply_text("⚠️ Trường này bắt buộc, vui lòng nhập.")
            return ASKING
        value = 0 if kind in ("money", "range") else None
    elif kind == "int":
        if not text.isdigit():
            await update.message.reply_text("⚠️ Phải là số, ví dụ: 3")
            return ASKING
        value = int(text)
    elif kind == "range" and field == "gia_sale" and (pct := parse_pct(text)):
        goc = context.user_data["data"].get("gia_goc")
        if not goc:
            await update.message.reply_text("⚠️ Chưa có giá gốc nên không tính được theo %. Gõ thẳng giá mua được.")
            return ASKING
        value = price_after_discount(goc, pct)
        context.user_data["data"].update(gia_sale_max=None, giam_pt=pct)
        await update.message.reply_text(f"✔️ {money(goc)} giảm {pct_text(pct)} = {money(value)}")
    elif kind == "range":
        rng = parse_range(text)
        if rng is None:
            await update.message.reply_text("⚠️ Giá không hợp lệ. Ví dụ: 4tr3, 200 (= 200.000), khoảng 4tr2 - 4tr4")
            return ASKING
        value, hi = rng
        context.user_data["data"][db.RANGE_FIELDS[field]] = hi
        await update.message.reply_text(f"✔️ {money_range(value, hi, field)}")
    elif kind == "tiers":
        value, errors = sale_text.parse_tiers(text)
        if errors or not value:
            await update.message.reply_text(TIERS_ERROR + (f"\nKhông đọc được: {'; '.join(errors)}" if errors else ""))
            return ASKING
        await update.message.reply_text("✔️ " + value.replace("\n", " · "))
    elif kind == "money":
        value = parse_money(text)
        if value is None:
            await update.message.reply_text("⚠️ Số tiền không hợp lệ. Ví dụ: 150 (=150.000), 1.2tr")
            return ASKING
        if field == "gia_thu":  # nhập giá 1 cái → lưu tổng cả đơn
            value, shown = price_total(context.user_data["data"], value)
        else:
            shown = money(value)
        await update.message.reply_text(f"✔️ {shown}")
    elif kind == "choice":
        value = match_option(text, step_options(options))
        if not value:
            await update.message.reply_text(f"⚠️ Vui lòng bấm chọn: {', '.join(step_options(options))}")
            return ASKING
    elif kind == "suggest":
        value = match_option(text, step_options(options)) or text
    elif kind == "pick_text":
        value = match_option(text, [name for name, _ in step_options(options)]) or text
    elif kind == "pick_id":
        value = next((v for v, label in step_options(options) if label.lower() == text.lower()), None)
        if value is None:
            await update.message.reply_text("⚠️ Bấm chọn 1 nút ở trên, hoặc gõ - nếu không thuộc chiến dịch nào")
            return ASKING
    else:
        value = text

    context.user_data["data"][field] = value
    return await next_step(update, context)


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    field, kind, prompt, options, required = current_steps(context)[context.user_data["step"]]
    value = query.data.split(":", 1)[1]
    shown = None
    if value == "-" and kind in ("choice", "photo", "pick_id") and not required:
        value = None
    elif kind == "pick_id" and value.startswith("#"):
        opts, labels = context.user_data.get("pick_opts") or [], context.user_data.get("pick_labels") or []
        i = int(value[1:])
        if i >= len(opts):
            return ASKING
        value, shown = opts[i], labels[i]
    elif kind == "pick_text" and value.startswith("#"):
        opts = context.user_data.get("pick_opts") or []
        i = int(value[1:])
        if i >= len(opts):
            return ASKING
        value = opts[i]
    elif kind != "choice" or value not in step_options(options):
        return ASKING  # bấm nút cũ của bước khác
    context.user_data["data"][field] = value
    await query.edit_message_text(f"{prompt} → {shown or value or 'bỏ qua'}")
    return await next_step(update, context)


async def go_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("step", 0) > 0:
        context.user_data["step"] -= 1
    # bước tự điền (nguồn khách, giá shop thu) mà quay lại thì hỏi thật, không tự nhảy qua nữa
    context.user_data["back_to"] = current_steps(context)[context.user_data.get("step", 0)][0]
    return await ask_step(update, context)


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    table, data = context.user_data["table"], context.user_data["data"]
    if query.data == "save":
        rec_id = db.insert(table, data, update.effective_user.id)
        await query.edit_message_text(fmt_record(table, data, rec_id) + "\n\n✅ Đã lưu.",
                                      parse_mode="HTML", disable_web_page_preview=True)
    else:
        await query.edit_message_text(f"❌ Đã hủy {FLOWS[table]['name']}.")
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("❌ Đã hủy.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ---------- xem / sửa / xóa / xuất (dùng chung) ----------

def make_view(table: str, cmd: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        rec_id = parse_id(context)
        if rec_id is None:
            await update.message.reply_text(f"Cú pháp: /{cmd} <id>")
            return
        r = db.get(table, rec_id)
        if not r:
            await update.message.reply_text("Không tìm thấy.")
            return
        text = fmt_record(table, r, r["id"], r["created_at"])
        if table == "orders":
            text += "\n\n" + status_history_text(r)
        await update.message.reply_html(text, disable_web_page_preview=True)
    return handler


def duration_text(a: str, b: str) -> str:
    """Khoảng thời gian giữa 2 mốc 'YYYY-MM-DD HH:MM:SS': "2 ngày 3 giờ", "45 phút"."""
    secs = int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds())
    d, rem = divmod(max(secs, 0), 86400)
    h, m = rem // 3600, rem % 3600 // 60
    parts = [f"{d} ngày"] * bool(d) + [f"{h} giờ"] * bool(h) + [f"{m} phút"] * bool(m and not d)
    return " ".join(parts) or "dưới 1 phút"


def status_history_text(r) -> str:
    """Các mốc đổi trạng thái của đơn (người dùng yêu cầu): tạo đơn (Đã đặt) → từng lần đổi trong
    db.order_status_log, kèm giờ, cách mốc trước bao lâu, đổi ở bot / web, ai đổi."""
    fmt = lambda at: f"{at[11:16]} {at[8:10]}/{at[5:7]}/{at[:4]}"
    lines = ["🕒 <b>Các mốc trạng thái</b>",
             f"{STATUS_ICON[db.STATUS_ORDERED]} {db.STATUS_ORDERED} · {fmt(r['created_at'])} <i>(tạo đơn)</i>"]
    logs = db.status_log(r["id"])
    if logs and logs[0]["tu"] != db.STATUS_ORDERED or not logs and r["trang_thai"] != db.STATUS_ORDERED:
        since = db.get_meta(db.STATUS_LOG_SINCE_KEY) or ""
        lines.append(f"⋯ <i>đổi trạng thái trước {fmt(since)[6:] if since else 'khi có nhật ký'} chưa có mốc giờ</i>")
    prev = r["created_at"]
    for g in logs:
        who = " ".join(x for x in (g["nguon"], g["nguoi"]) if x)
        lines.append(f"{STATUS_ICON.get(g['den'], '•')} {html.escape(g['den'])} · {fmt(g['at'])}"
                     f" · <i>sau {duration_text(prev, g['at'])}</i>" + (f" · {html.escape(who)}" if who else ""))
        prev = g["at"]
    return "\n".join(lines)


def mark_done(rec_id: int):
    """Chuyển Hoàn thành – chỉ khi người dùng bấm/gõ lệnh bán (/ban, nút HOÀN THÀNH), không tự đổi khi sửa giá."""
    db.update("orders", rec_id, "trang_thai", db.STATUS_DONE)


def field_options(table: str, field: str) -> list[tuple] | None:
    """Các giá trị cố định của 1 trường dạng [(giá trị, nhãn nút)], None nếu nhập tự do."""
    if table == "orders" and field == "trang_thai":
        return [(st, status_text(st)) for st in db.STATUSES]
    if table == "orders" and field == "chien_dich":
        return [(cid, f"🎁 {name}") for cid, name in db.campaign_choices()] + [(None, "— Không thuộc chiến dịch")]
    if table == "sales" and field == "active":
        return [(1, "✅ Còn sale"), (0, "⛔ Hết sale")]
    if table == "phones" and field == "da_su_dung":
        return [(1, f"{PHONE_BUSY} Đang dùng"), (0, f"{PHONE_FREE} Free")]
    if table == "phones" and field == "cua_toi":
        return [(1, f"{PHONE_MINE} Của tôi"), (0, "👤 Người khác")]
    for f, kind, _, options, required in FLOWS[table]["steps"]:
        if f == field and kind == "choice":
            opts = [(o, o) for o in step_options(options)]
            return opts if required else opts + [(None, "— Bỏ trống")]
    return None


TIERS_ERROR = ("⚠️ Mức giá không hợp lệ. Ví dụ: dưới 4tr5 công 100 · nhiều mức: "
               "dưới 4tr5 công 100; dưới 4tr3 công 150-200k (gõ - để xóa hết)")


def parse_field_value(table: str, field: str, text: str):
    """Đọc giá trị người dùng gõ cho 1 trường. Trả về (giá trị, lỗi)."""
    text = text.strip()
    if field == "uu_tien":  # 1, 2, 3… ; - hoặc 0 = bỏ thứ tự
        ok = text == "-" or text.isdigit()
        return (0 if text == "-" else int(text), None) if ok else (None, "Gõ số thứ tự: 1 (lên đầu), 2, 3… · gõ - hoặc 0 để bỏ.")
    if field == "nhom":  # 1, 2… ; - hoặc 0 = bỏ nhóm
        ok = text == "-" or (text.isdigit() and int(text) <= MAX_SALE_GROUP)
        return (0 if text == "-" else int(text), None) if ok else (None, f"Gõ số nhóm 1 – {MAX_SALE_GROUP} · gõ - hoặc 0 để bỏ nhóm.")
    if field == "chien_dich":  # gõ tên chiến dịch (hoặc id), - = bỏ
        if text == "-":
            return None, None
        cs = db.campaign_choices()
        value = next((cid for cid, name in cs if name.lower() == text.lower() or str(cid) == text.lstrip("#")), None)
        return (value, None) if value else (None, "Bấm chọn chiến dịch ở trên, hoặc gõ - để bỏ.")
    required = any(f == field and req for f, _, _, _, req in FLOWS[table]["steps"])
    if text == "-":
        if field in db.RANGE_FIELDS:
            return (0, None), None
        if field in db.MONEY_FIELDS:
            return 0, None
        if required or field_options(table, field):
            return None, "Trường này không được để trống."
        return None, None
    if field == "so_luong":
        return (int(text), None) if text.isdigit() else (None, "Số lượng phải là số, ví dụ: 3")
    if field == "giam_pt":
        value = parse_pct(text if "%" in text else text + "%")
        return (value, None) if value else (None, "Phần trăm giảm phải từ 0 đến 100, ví dụ: 10 hoặc 12.5%")
    if field == "ha_noi":
        value = 1 if fold(text) in ("hn", "ha noi") else parse_bool(text)
        return (value, None) if value is not None else (None, "Gõ 1 (ở Hà Nội) hoặc 0 (không ở Hà Nội), - để bỏ trống.")
    if field in ("zalo", "facebook"):
        try:
            return social_link(field, text), None
        except ValueError as e:
            return None, f"{e}. Gửi link (https://zalo.me/…, https://facebook.com/…) hoặc SĐT Zalo."
    if field == "muc_gia":
        value, errors = sale_text.parse_tiers(text)
        return (value, None) if value and not errors else (None, TIERS_ERROR)
    if field in db.RANGE_FIELDS:
        rng = parse_range(text)
        return (rng, None) if rng else (None, "Giá không hợp lệ. Ví dụ: 4tr3, 200 (= 200.000), 150-200, dưới 4tr8, từ 70 trở lên")
    if field in db.MONEY_FIELDS:
        value = parse_money(text)
        return (value, None) if value is not None else (None, "Số tiền không hợp lệ. Ví dụ: 150 (=150.000), 1.2tr")
    if field == "trang_thai":
        value = parse_status(text)
        return (value, None) if value else (None, f"Trạng thái phải là: {', '.join(db.STATUSES)}")
    if field == "active":
        value = parse_bool(text)
        return (value, None) if value is not None else (None, "active phải là 1 (còn sale) hoặc 0 (hết sale).")
    if options := field_options(table, field):
        value = match_option(text, [v for v, _ in options if v is not None])
        return (value, None) if value else (None, f"Chọn một trong: {', '.join(l for _, l in options)}")
    return text, None


def status_change_error(r, new_status: str) -> str | None:
    """Kiểm tra đổi trạng thái có hợp lệ với giá shop thu không."""
    if new_status == db.STATUS_DONE and not r["gia_thu"]:
        return f"Đơn chưa có giá shop thu. Dùng /ban {r['id']} <giá> để hoàn thành."
    return None


async def apply_edit(message, table: str, rec_id: int, field: str, value, edit_message=None):
    """Ghi 1 trường và trả lời bằng chi tiết bản ghi (sửa tin nhắn nút bấm nếu có)."""
    r = db.get(table, rec_id)
    if not r:
        await message.reply_text("Không tìm thấy.")
        return
    if table == "orders" and field == "trang_thai":
        err = status_change_error(r, value)
        if err:
            await message.reply_text(f"⚠️ {err}")
            return
    if field == "ten" and table in ("products", "nguoi_dat", "carriers") and db.name_taken(table, rec_id, value):
        await message.reply_text(f"⚠️ Đã có {FLOWS[table]['name']} tên “{value}”.")
        return
    if field in db.RANGE_FIELDS and isinstance(value, tuple):
        db.update(table, rec_id, db.RANGE_FIELDS[field], value[1])
        value = value[0]
    note = ""
    if table == "orders" and field == "gia_thu" and order_qty(r) > 1 and value:
        value, shown = price_total(r, value)  # nhập giá 1 cái → lưu tổng cả đơn
        note = f"💰 Giá shop thu: {shown}\n"
    db.update(table, rec_id, field, value)
    if table == "orders" and field == "gia_thu" and not value and r["trang_thai"] == db.STATUS_DONE:
        db.update(table, rec_id, "trang_thai", db.STATUS_PAID)  # xóa giá bán → đơn chưa bán nữa
    r = db.get(table, rec_id)
    text = "✏️ Đã sửa.\n" + note + "\n" + fmt_record(table, r, r["id"], r["created_at"])
    if edit_message:
        await edit_message(text, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await message.reply_html(text, disable_web_page_preview=True)


def fmt_value(table: str, field: str, value, r=None) -> str:
    if value in (None, ""):
        return "—"
    if r is not None and field in db.RANGE_FIELDS:
        return money_range(value, r[db.RANGE_FIELDS[field]], field)
    if field in db.MONEY_FIELDS:
        return money(value)
    for v, label in field_options(table, field) or []:
        if v == value:
            return label
    return html.escape(str(value))


def value_keyboard(table: str, rec_id: int, field: str, current) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(("• " if v == current else "") + label,
                                    callback_data=f"edit:{table}:{rec_id}:{field}:{i}")
               for i, (v, label) in enumerate(field_options(table, field))]
    return InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])


def make_edit(table: str, cmd: str, find=None, default_field: str | None = None):
    """
    /sua <id>                    → nút chọn trường cần sửa (bảng chỉ có 1 trường: hỏi luôn giá trị)
    /sua <id> <trường>           → nút chọn giá trị (trường cố định) hoặc hỏi gõ giá trị
    /sua <id> <trường> <giá trị> → sửa luôn
    find: tìm bản ghi theo tham số đầu (mặc định: id); default_field: /cmd <id> <giá trị> (không ghi trường)
    sửa cho trường này – giữ cú pháp cũ, vd /suand <id> <tên mới>.
    """
    labels = dict(db.TABLES[table])

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        r = (find(args[0]) if find else db.get(table, int(args[0])) if args[0].isdigit() else None) if args else None
        if not r:
            await update.message.reply_text(
                ("Không tìm thấy.\n" if args else "")
                + f"Cú pháp: /{cmd} <id>  (rồi bấm chọn trường)\n"
                f"hoặc: /{cmd} <id> <trường> <giá trị>\nTrường: {', '.join(labels)}")
            return
        rec_id = r["id"]
        name = FLOWS[table]["name"].capitalize()
        if len(args) == 1 and len(labels) == 1:
            await ask_field_value(update.message.reply_html, context, table, r, next(iter(labels)))
            return
        if len(args) >= 2 and args[1] not in labels and default_field:
            args = [args[0], default_field, *args[1:]]

        if len(args) == 1:
            buttons = [InlineKeyboardButton(label, callback_data=f"ef:{table}:{rec_id}:{field}")
                       for field, label in db.TABLES[table] if field not in db.RANGE_FIELDS.values()]
            rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
            if table == "orders":  # ảnh QR không nằm trong TABLES nên thêm nút riêng
                qr_row = [InlineKeyboardButton("📷 Đổi ảnh QR" if r["qr_file_id"] else "📷 Thêm ảnh QR",
                                               callback_data=f"qrnew:{rec_id}")]
                if r["qr_file_id"]:
                    qr_row.append(InlineKeyboardButton("🗑 Xóa ảnh QR", callback_data=f"qrdel:{rec_id}"))
                rows.append(qr_row)
            if table == "sales":  # cùng sản phẩm, shop khác → món sale mới (giá / link riêng)
                rows.append([InlineKeyboardButton("➕ Thêm shop khác cho sản phẩm này",
                                                  callback_data=f"saleshop:{rec_id}")])
            if table in ("sales", "products"):  # ảnh lưu theo tên sản phẩm, /sale gửi kèm
                n_img = len(db.product_images(r["ten_sp"] if table == "sales" else r["ten"]))
                rows.append([InlineKeyboardButton(f"🖼 Ảnh sản phẩm ({n_img}) – xem / thêm ảnh",
                                                  callback_data=f"pimg:{table}:{rec_id}")])
            rows.append([InlineKeyboardButton("✖️ Thôi", callback_data="ef:x")])
            await update.message.reply_html(
                f"✏️ {name} <code>#{rec_id}</code> – chọn trường cần sửa:",
                reply_markup=InlineKeyboardMarkup(rows))
            return

        field, text = args[1], " ".join(args[2:])
        if table == "orders" and field.lower() in ("qr", "qr_file_id", "anhqr"):
            await ask_qr_photo(update.message, context, rec_id)
            return
        if field not in labels:
            await update.message.reply_text(f"Trường không hợp lệ. Dùng một trong: {', '.join(labels)}")
            return
        if not text:
            await ask_field_value(update.message.reply_html, context, table, r, field)
            return
        if table == "sales" and field == "link":
            await on_sale_links_text(update, context, rec_id, text)
            return
        value, err = parse_field_value(table, field, text)
        if err:
            await update.message.reply_text(f"⚠️ {err}")
            return
        await apply_edit(update.message, table, rec_id, field, value)
    return handler


async def ask_field_value(send, context, table: str, r, field: str):
    """Hiện nút chọn giá trị, hoặc hỏi người dùng gõ giá trị mới."""
    context.user_data.pop("done_after_price", None)  # sửa thường: không tự Hoàn thành
    label = dict(db.TABLES[table])[field]
    head = (f"✏️ {FLOWS[table]['name'].capitalize()} <code>#{r['id']}</code> – {label.lower()} "
            f"(hiện tại: <b>{fmt_value(table, field, r[field], r)}</b>)")
    if field_options(table, field):
        await send(head + "\nChọn giá trị mới:", reply_markup=value_keyboard(table, r["id"], field, r[field]))
        return
    context.user_data["pending_edit"] = (table, r["id"], field)
    if table == "sales" and field == "link":
        text, kb = sale_links_panel(r)
        await send(text, reply_markup=kb)
        return
    hint = (" (vd: 4tr3, 200 = 200.000, khoảng 150-200)" if field in db.RANGE_FIELDS
            else " (tính theo nghìn: 150 = 150.000)" if field in db.MONEY_FIELDS else "")
    extra = unit_price_hint(r) if table == "orders" and field == "gia_thu" else ""
    await send(head + f"\nGõ giá trị mới{hint}. Gõ - để xóa, /huy để thôi.{extra}")


def sale_links_panel(r) -> tuple[str, InlineKeyboardMarkup]:
    """Sửa link món sale: liệt kê link, gửi link để thêm, nút xóa từng link / xóa hết / xong."""
    urls = (r["link"] or "").split()
    lines = [f"🔗 Món sale <code>#{r['id']}</code> {html.escape(r['ten_sp'])} – {len(urls)} link:"]
    lines += [f"{i}. {html.escape(u)}" for i, u in enumerate(urls, 1)] or ["<i>(chưa có link)</i>"]
    lines.append("\nGửi link để <b>thêm</b> (gửi nhiều lần, nhiều link 1 lần đều được). Bấm 🗑 để xóa, ✅ Xong khi đã đủ.")
    buttons = [InlineKeyboardButton(f"🗑 {i}", callback_data=f"sl:{r['id']}:del:{i - 1}")
               for i in range(1, len(urls) + 1)]
    rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    last = [InlineKeyboardButton("✅ Xong", callback_data=f"sl:{r['id']}:ok")]
    if urls:
        last.insert(0, InlineKeyboardButton("🗑 Xóa hết", callback_data=f"sl:{r['id']}:clr"))
    rows.append(last)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def on_sale_links_text(update: Update, context: ContextTypes.DEFAULT_TYPE, rec_id: int, text: str | None = None):
    """Link gửi khi đang sửa link món sale → thêm vào (bỏ trùng), vẫn chờ thêm link tiếp."""
    text = update.message.text if text is None else text
    r = db.get("sales", rec_id)
    if not r:
        context.user_data.pop("pending_edit", None)
        await update.message.reply_text("Không tìm thấy món sale.")
        return
    urls = (r["link"] or "").split()
    if text.strip() == "-":
        urls = []
    else:
        new = sale_text.URL_RE.findall(text)
        if not new:
            await update.message.reply_text("⚠️ Không thấy link nào (link phải bắt đầu bằng http). "
                                            "Gửi lại, bấm ✅ Xong, hoặc /huy để thôi.")
            return
        urls += [u for u in dict.fromkeys(new) if u not in urls]
    db.update("sales", rec_id, "link", "\n".join(urls) or None)
    context.user_data["pending_edit"] = ("sales", rec_id, "link")
    text, kb = sale_links_panel(db.get("sales", rec_id))
    await update.message.reply_html("✔️ Đã lưu.\n\n" + text, reply_markup=kb, disable_web_page_preview=True)


async def on_sale_links_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút trong bảng sửa link: sl:<id>:del:<i> | sl:<id>:clr | sl:<id>:ok"""
    query = update.callback_query
    await query.answer()
    _, rec_id, action, *rest = query.data.split(":")
    r = db.get("sales", int(rec_id))
    if not r:
        await query.edit_message_text("Không tìm thấy món sale.")
        return
    if action == "ok":
        context.user_data.pop("pending_edit", None)
        await query.edit_message_text("✏️ Đã sửa.\n\n" + fmt_record("sales", r, r["id"], r["created_at"]),
                                      parse_mode="HTML", disable_web_page_preview=True)
        return
    urls = (r["link"] or "").split()
    if action == "clr":
        urls = []
    elif action == "del" and rest and rest[0].isdigit() and int(rest[0]) < len(urls):
        urls.pop(int(rest[0]))
    db.update("sales", r["id"], "link", "\n".join(urls) or None)
    context.user_data["pending_edit"] = ("sales", r["id"], "link")
    text, kb = sale_links_panel(db.get("sales", r["id"]))
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


async def on_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bấm nút chọn trường từ /sua <id>."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_edit", None)
    parts = query.data.split(":")
    if parts[1] == "x":
        await query.edit_message_text("Đã thôi sửa.")
        return
    table, rec_id, field = parts[1], int(parts[2]), parts[3]
    r = db.get(table, rec_id) if table in db.TABLES else None
    if not r or field not in dict(db.TABLES[table]):
        await query.edit_message_text("Không còn hợp lệ.")
        return

    async def send(text, reply_markup=None):
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
    await ask_field_value(send, context, table, r, field)


async def on_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nhận giá trị gõ tay sau khi chọn trường cần sửa."""
    if await on_traship_text(update, context):
        return
    if await on_sale_toggle_text(update, context):
        return
    if await on_sale_show_text(update, context):
        return
    if await on_sale_link_text(update, context):
        return
    if context.user_data.pop("pending_address", None):  # /themdiachi không kèm nội dung → tin này là địa chỉ
        await save_address(update, update.message.text)
        return
    pending = context.user_data.get("pending_edit")
    if not pending:
        return
    table, rec_id, field = pending
    if table == "sales" and field == "link":
        await on_sale_links_text(update, context, rec_id)
        return
    if table == "sales" and field == "gia_sale" and parse_pct(update.message.text):
        field = "giam_pt"  # gõ "10%" ở giá mua được = giảm 10% so với giá gốc
    if table == "sales" and field == "giam_pt" and update.message.text.strip() != "-" and not db.get("sales", rec_id)["gia_goc"]:
        await update.message.reply_text("⚠️ Món này chưa có giá gốc. Sửa Giá gốc trước, hoặc gõ thẳng giá mua được.")
        return
    text, sell_qty = update.message.text, None
    if table == "orders" and field == "gia_thu" and context.user_data.get("done_after_price") == rec_id:
        text, sell_qty = split_sell_qty(text)  # nút HOÀN THÀNH: "2tr65 1c" = bán 1 cái
    value, err = parse_field_value(table, field, text)
    if err:
        await update.message.reply_text(f"⚠️ {err} Gõ lại, hoặc /huy để thôi.")
        return
    context.user_data.pop("pending_edit", None)
    if context.user_data.pop("done_after_price", None) == rec_id and field == "gia_thu" and value:
        await sell_order(update.message, db.get("orders", rec_id), value, sell_qty)  # từ nút HOÀN THÀNH
        return
    await apply_edit(update.message, table, rec_id, field, value)


async def on_edit_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bấm nút chọn giá trị."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_edit", None)
    _, table, rec_id, field, idx = query.data.split(":")
    options = field_options(table, field) if table in db.TABLES else None
    if not options or not idx.isdigit() or int(idx) >= len(options):
        await query.edit_message_text("Lựa chọn không còn hợp lệ.")
        return
    await apply_edit(query.message, table, int(rec_id), field, options[int(idx)][0],
                     edit_message=query.edit_message_text)


def make_delete(table: str, cmd: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        rec_id = parse_id(context)
        if rec_id is None:
            await update.message.reply_text(f"Cú pháp: /{cmd} <id>")
            return
        if not db.get(table, rec_id):
            await update.message.reply_text("Không tìm thấy.")
            return
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Xóa", callback_data=f"del:{table}:{rec_id}"),
            InlineKeyboardButton("Không", callback_data="del:no"),
        ]])
        await update.message.reply_text(f"Xóa {FLOWS[table]['name']} #{rec_id}?", reply_markup=kb)
    return handler


async def on_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if parts[1] == "no":
        await query.edit_message_text("Đã giữ lại.")
        return
    table, rec_id = parts[1], int(parts[2])
    if table in db.TABLES and db.delete(table, rec_id):
        await query.edit_message_text(f"🗑 Đã xóa {FLOWS[table]['name']} #{rec_id}.")
    else:
        await query.edit_message_text("Không còn tồn tại.")


def make_export(table: str, filename: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        rows = db.all_rows(table)
        if not rows:
            await update.message.reply_text("Chưa có dữ liệu.")
            return
        fields = db.TABLES[table]
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ID", *[l for _, l in fields], "Ngày tạo"])
        for r in rows:
            writer.writerow([r["id"], *[r[f] for f, _ in fields], r["created_at"]])
        data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # BOM để Excel đọc đúng tiếng Việt
        await update.message.reply_document(data, filename=f"{filename}_{datetime.now():%Y%m%d_%H%M}.csv")
    return handler


# ---------- lệnh riêng: đơn hàng ----------

HELP = """🤖 <b>Bot quản lý đơn hàng</b>

<b>Đơn hàng</b> (⏳ Đã đặt → 📦 Đã nhận → 💵 Thanh toán → ✅ Hoàn thành)
/them – thêm đơn · /ds – danh sách đơn (bấm link dưới mỗi đơn để trả công / hoàn thành)
/xem · /sua · /xoa &lt;id&gt; – xem / sửa (bấm chọn trường) / xóa đơn
/danhan &lt;id&gt; – đã nhận hàng, chưa trả công · /dsdanhan – các đơn 📦 Đã nhận
/dsdiachi [từ khóa] – đơn Đã đặt gom theo địa chỉ giống nhau
/ban &lt;id&gt; &lt;giá 1 cái&gt; [số cái] – bán xong → Hoàn thành (vd /ban 129 2tr65 1c: bán 1 cái, phần còn lại tách đơn mới)
/tach &lt;id&gt; [số cái] – tách đơn nhiều cái (không bán): /tach 129 = thành từng đơn 1 cái
/tim &lt;từ khóa&gt; · /khach &lt;tên|SĐT&gt; – tìm đơn
/thongke – thống kê tháng · /traship – đơn Đã đặt theo người đặt trong khoảng ngày (bot hỏi ngày bắt đầu / kết thúc)
/thuongsale – thưởng săn sale của chiến dịch đang bật (/thuongsale all, /thuongsale &lt;id&gt;; tạo chiến dịch trên web, trang Đơn hàng, mục 🎁 Thưởng săn sale)

<b>Danh mục</b> (sửa đều bấm chọn trường: /suasp, /suand, /suasdt, /suadvvc &lt;id&gt;)
/sanpham – sản phẩm + giá thu gợi ý · /themsp &lt;tên&gt; | &lt;giá&gt; · /anhsp &lt;id&gt; – ảnh sản phẩm (/sale gửi kèm)
/nguoidat – người đặt + QR trả công · /sdt – danh bạ SĐT (/sdt toi – số của tôi) · /dvvc – đơn vị vận chuyển
/diachi – sổ địa chỉ đặt hàng · /themdiachi &lt;địa chỉ&gt; · /suadiachi · /xoadiachi &lt;id&gt;

<b>Săn sale</b>
/themsale – thêm món (hoặc dán danh sách), 1 sản phẩm nhiều shop → mỗi shop 1 món · /sale – xem · /sale 1 – nhóm 1 · /nhomsale – chia nhóm · /saleall – cả món hết sale, thêm link, bật/tắt · /dssale – quản lý

<b>Vận chuyển</b>
Bấm mã vận đơn có 🔍 để xem hành trình · /tracuu &lt;mã&gt; · /kiemtravc – kiểm tra ngay · /danggiao – đơn đang giao đến

/ghep – ghép 2+ ảnh thành 1 ảnh (gửi ảnh sau lệnh)
/huy – hủy thao tác · /clear – xóa lịch sử chat · /help full – xem đủ mọi lệnh
💡 Tiền tính theo nghìn: 150 = 150.000, 4tr3 = 4.300.000"""

HELP_FULL = """🤖 <b>Bot quản lý đơn hàng – đầy đủ</b>

<b>Đơn hàng</b>
/them – thêm đơn mới
/ds – xem toàn bộ đơn kèm trạng thái, không tính đơn hủy (/ds 10 – chỉ 10 đơn mới nhất)
/dsdadat [n] – đơn ⏳ Đã đặt; bấm 📦 ĐÃ NHẬN HÀNG (nhận rồi, chưa trả công → Đã nhận) hoặc 💳 THANH TOÁN CÔNG dưới mỗi đơn: xem QR → ✅ Đã trả công (chuyển Thanh toán)
/dsdiachi [từ khóa] – đơn ⏳ Đã đặt gom theo địa chỉ giống nhau (Ngõ 34 láng hạ = 12B 34 láng hạ, 10 lh = 10 láng hạ; tên gọi khác của địa chỉ đặt trong ADDR_ALIASES ở .env của web), mỗi nhóm: số đơn, tổng giá đặt, từng đơn kèm link 📦 Đã nhận / 💳 Trả công; /dsdiachi lang ha = chỉ nhóm khớp
/dsdanhan [n] – đơn 📦 Đã nhận (đã nhận hàng, chưa trả công); bấm 💳 THANH TOÁN CÔNG để trả công
/dsthanhtoan [n] – đơn 💵 Thanh toán; bấm ✅ HOÀN THÀNH (ĐÃ BÁN) dưới mỗi đơn để ghi giá bán → Hoàn thành
/dshoanthanh [n] – đơn ✅ Hoàn thành
/dshuy [n] – đơn ❌ Đã hủy
/xem &lt;id&gt; – xem chi tiết đơn

<b>Trạng thái đơn:</b> ⏳ Đã đặt → 📦 Đã nhận → 💵 Thanh toán → ✅ Hoàn thành, hoặc ❌ Đã hủy
/dangdat – đơn đã đặt, chưa nhận hàng (thêm <code>full</code> để xem chi tiết)
/chuaban – đơn chưa bán (Đã đặt + Đã nhận + Thanh toán) và tổng vốn
/danhan &lt;id&gt; [id ...] – đã nhận hàng, chưa trả công người đặt
/thanhtoan &lt;id&gt; [id ...] – đã nhận hàng + trả công người đặt
/ban &lt;id&gt; &lt;giá shop thu 1 cái&gt; [số cái] – bán được → ghi giá, chuyển Hoàn thành; đơn nhiều cái mà chỉ bán vài cái thì ghi số cái (1c / x1): phần còn lại tách thành đơn mới ✂️ (giữ trạng thái, chia giá đặt / công theo số cái, không tính thêm mốc thưởng)
/tach &lt;id&gt; [số cái] – tách đơn nhiều cái mà không bán: không ghi số cái = tách hết thành từng đơn 1 cái (vd đơn 2 main → 2 đơn 1 cái); ghi số cái = tách chừng đó cái ra 1 đơn mới. Giá đặt / công / giá shop thu chia theo số cái, giữ trạng thái, không tính thêm mốc thưởng
/huydon &lt;id&gt; [id ...] – chuyển đơn sang ❌ Đã hủy
/datlai &lt;id&gt; – chuyển đơn về Đã đặt

/khach &lt;tên hoặc SĐT&gt; – xem tất cả đơn của 1 khách + tổng tiền
/tim &lt;từ khóa&gt; – tìm theo tên, SĐT, mã đơn, sản phẩm, địa chỉ
/sua &lt;id&gt; – sửa đơn: bấm chọn trường rồi chọn/gõ giá trị mới (có cả nút 📷 thêm/đổi/xóa ảnh QR)
/sua &lt;id&gt; &lt;trường&gt; &lt;giá trị&gt; – sửa nhanh 1 dòng
/xoa &lt;id&gt; – xóa đơn
/thongke – thống kê tháng này (số đơn, vốn, lãi theo sàn) · /thongke 9 hoặc 9/2026 – tháng khác
/thuongsale – thưởng săn sale theo chiến dịch (tạo / sửa trên web, trang Đơn hàng, mục 🎁 Thưởng săn sale): mỗi người đặt cứ N đơn gán vào chiến dịch (bước 🎁 của /them, /sua) ở 💵 Thanh toán / ✅ Hoàn thành → thưởng X. /thuongsale = chiến dịch đang bật · /thuongsale all · /thuongsale &lt;id&gt;
/traship – đơn ⏳ Đã đặt gom theo người đặt: số đơn + tổng giá đặt; bot hỏi ngày bắt đầu rồi ngày kết thúc (ngày/tháng/năm). Gõ nhanh: /traship 1/11/2026 4/11/2026 (1 ngày = từ ngày đó tới nay)
/xuat – xuất đơn ra file CSV (mở bằng Excel)

<b>Tra cứu vận chuyển</b> (hỗ trợ SPX, J&amp;T, GHN – J&amp;T, GHN dùng 4 số cuối SĐT của đơn; BEST có captcha → bấm 🌐 tra web, trang đã điền sẵn mã)
Bấm vào mã vận đơn có 🔍 trong /ds, /xem… – bot trả hành trình
/tracuu &lt;số đơn&gt; hoặc /tracuu &lt;mã vận đơn&gt; [4 số cuối SĐT] – tra cứu thủ công
Tự động: khoảng 12 giờ tra các đơn ⏳ Đã đặt 1 lần, đơn sắp giao khoảng 1 giờ/lần (lệch ngẫu nhiên ±30 phút); khớp điều kiện trong alert_rules.py → bot alert nhắn hành trình
/kiemtravc – chạy kiểm tra tự động ngay
/danggiao – tra lại hành trình các đơn Đã đặt từ hãng rồi gửi bảng tổng hợp đơn đang giao đến (người đặt, tên trên đơn, mã vận đơn, ĐVVC, hàng, giá đặt, địa chỉ, nhân viên giao nếu J&amp;T có ghi); bấm mã vận đơn → hành trình. Bot alert tự gửi bảng này mỗi khi có đơn mới chuyển sang đang giao, và cũng nhận lệnh /danggiao
/alert – quản lý người nhận tin bot alert (duyệt / từ chối / bỏ quyền / xóa)
Ai bấm Start với bot alert → bot báo bạn kèm nút ✅ Duyệt / ⛔ Từ chối
/themalert &lt;chat id&gt; [tên] · /xoaalert &lt;chat id&gt; – cấp quyền / xóa bằng tay

<b>Ảnh QR trả công người đặt</b>
/qr &lt;id&gt; – xem ảnh QR của đơn (chưa có thì bot chờ bạn gửi ảnh)
Gửi ảnh kèm chú thích <code>qr &lt;id&gt;</code> – lưu/đổi ảnh QR cho đơn
/xoaqr &lt;id&gt; – xóa ảnh QR
Ảnh QR lưu theo <b>người đặt</b>: cùng tên người đặt thì mọi đơn dùng chung 1 ảnh, đơn mới tự lấy QR đã lưu

<b>Người đặt hộ</b>
/nguoidat – danh sách (📷 = đã có QR, nợ công), bấm để xem/đổi QR
/nguoidat &lt;id|tên&gt; – chi tiết + ảnh QR + các đơn đang đặt (⏳ Đã đặt / 📦 Đã nhận)
/nguoidat hn – người ở Hà Nội (ngoai – nơi khác, chuaro – chưa rõ; đặt bằng /suand)
/suand &lt;id&gt; – bấm chọn trường cần sửa: tên (cập nhật cả các đơn), SĐT, ghi chú · /xoand &lt;id&gt; – xóa khỏi danh sách

<b>Danh bạ SĐT</b> (số dùng trong đơn tự được thêm vào)
/sdt – danh sách số + số đơn, bấm để xem chi tiết
/sdt &lt;số / vài số cuối / id&gt; – số đó gắn với những đơn nào, địa chỉ nào
/themsdt &lt;số&gt; [số ...] [toi] [ghi chú] [may &lt;tên máy&gt;] – thêm (<code>toi</code> = 📱 số của tôi, <code>may …</code> = sim lắp ở máy nào) · /suasdt &lt;id|số&gt; – bấm chọn sửa chủ số / máy / ghi chú / trạng thái · /xoasdt &lt;id|số&gt;
/sdt free – số 🟢 free · /sdt dung – số 🔴 đang dùng (có đơn chưa hoàn thành; mọi đơn hoàn thành/hủy → tự thành free)
/sdt toi – 📱 số của tôi · /sdt khac – số người khác (ghép được: /sdt toi free)
/dasudung &lt;id|số&gt; – đánh dấu tay đang dùng · /chuasudung &lt;id|số&gt; – đánh dấu tay free (nhiều số cách nhau dấu cách)

<b>Đơn vị vận chuyển</b> (danh sách để chọn khi thêm/sửa đơn)
/dvvc – xem danh sách
/themdvvc &lt;tên&gt; – thêm (nhiều tên cách nhau dấu phẩy)
/suadvvc &lt;id&gt; [tên mới] – đổi tên (cập nhật cả đơn cũ)
/xoadvvc &lt;id&gt; – xóa khỏi danh sách chọn

<b>Sổ địa chỉ đặt hàng</b> (web: tab 📍 Địa chỉ)
/diachi [từ khóa] – xem / tìm, bấm vào địa chỉ để chép
/diachi &lt;id&gt; – chỉ trả nguyên văn địa chỉ đó (giữ tin để chép cho dễ)
/themdiachi &lt;địa chỉ&gt; – lưu (được xuống dòng; gõ /themdiachi trống thì bot hỏi)
/suadiachi &lt;id&gt; [nội dung mới] · /xoadiachi &lt;id&gt;

<b>Sản phẩm</b> (chọn tên khi thêm đơn / săn sale + giá thu gợi ý khi nhập giá shop thu)
/sanpham [từ khóa] – danh mục + giá thu gợi ý
/themsp &lt;tên&gt; | &lt;giá&gt; – thêm, giá là 1 mức hoặc khoảng (4tr7 - 4tr750); mỗi dòng 1 sản phẩm, giá bỏ trống được
/suasp &lt;id&gt; – bấm chọn trường cần sửa (đổi tên thì cập nhật cả đơn/sale cũ) · /giasp &lt;id&gt; &lt;giá&gt; – sửa nhanh giá · /xoasp &lt;id&gt;
/anhsp &lt;id|tên&gt; – xem + thêm ảnh sản phẩm (gửi ảnh, /xong khi xong; tối đa 10 ảnh; hoặc gửi ảnh kèm chú thích <code>anhsp &lt;id|tên&gt;</code>) · /xoaanhsp &lt;id|tên&gt; &lt;số|all&gt; · /anhsp – sản phẩm nào có ảnh. /sale gửi kèm album ảnh từng sản phẩm (/sale text = chỉ chữ)

<b>Săn sale</b>
/themsale – thêm 1 món, hỏi lần lượt: tên SP → giá mua được → công → số lượng → link (gửi nhiều link được)
/themsale + dán danh sách – thêm nhiều món 1 lần, dạng:
<code>- ram viper x1 4tr3 công 200 sl 2
https://link1
https://link2

- vga 3050 4tr-4tr2 công 150-200k
https://link

- i5 14600K 4tr8 công 70 shop Hải Nam
https://link

- i5 14600K 4tr9 shop Phong Vũ
https://link</code>
“shop …” đứng cuối dòng (không bắt buộc). 1 sản phẩm bán ở nhiều shop → mỗi shop 1 dòng cùng tên, /sale tự gộp theo sản phẩm.
Mức giá khác (giá thấp hơn công cao hơn…): thêm dòng <code>hoặc dưới 4tr5 công 100</code> ngay dưới món, hoặc /suasale &lt;id&gt; → Mức giá khác.
Món sale ghi 1 mức: giá hiện “dưới 4tr8”, công hiện “từ 70 trở lên”.
/sale – danh sách món đang sale dễ đọc (tên, giá, công, SL, link đầy đủ) để copy; thêm link / tắt sale: /saleall
/sale copy – xuất đúng dạng tin nhắn ở trên để copy/gửi khách
/sale text – chỉ chữ, không gửi kèm ảnh sản phẩm (mặc định /sale gửi album ảnh của sản phẩm có ảnh, thẻ sale làm chú thích)
/sale &lt;từ khóa&gt; – lọc theo tên, vd: /sale 14600 · /sale all – gồm cả món hết sale
/sale 1 – chỉ món nhóm 1 (số 1–99; dùng kèm được: /sale 1 copy, /saleall 1) · /nhomsale – xem nhóm · /nhomsale 1 2 3 – món #2, #3 vào nhóm 1 (0 = bỏ nhóm)
/saleall [từ khóa] – mọi món kể cả hết sale; dưới mỗi shop có link ➕ Thêm link · ⛔ Tắt / ✅ Bật sale
/dssale – bảng quản lý có id
/hetsale &lt;id&gt; [id ...] – hết sale · /hetsale all – cho tất cả hết sale
/consale &lt;id&gt; [id ...] – còn sale trở lại
Chat “sale bật 14600k dtech” / “sale tắt 14600k dtech” (hoặc id: “sale tắt 3 5”, “sale tắt all”; nhiều món: “sale tắt 14600kf, màn hình, 1660sp”) – bật/tắt theo từ khóa, khớp mọi từ trong tên + shop
Chat “sale màn hình” – như /sale màn hình: chỉ thẻ sale các món khớp từ khóa (không dấu cũng được; thêm all / copy / text / số nhóm như /sale)
Chat “sale link 14600k dtech” – mở bảng paste link mới liên tục; ✅ Xong = xóa hết link cũ, chỉ giữ link mới (➕ Giữ cả link cũ nếu muốn)
/xemsale, /suasale, /xoasale &lt;id&gt; – xem / sửa (bấm chọn) / xóa món sale
/xuatsale – xuất bảng sale ra file CSV

/huy – hủy thao tác đang làm
/clear [n] – xóa lịch sử chat cho gọn (n tin gần nhất, mặc định 1000; Telegram chỉ cho xóa tin trong 48 giờ)

💡 Số tiền tính theo <b>nghìn đồng</b>: gõ 1 = 1.000, 150 = 150.000, 1500 = 1.500.000.
Có thể gõ 150k, 1.2tr, hoặc viết đủ 150.000.

Trường dùng cho /sua: """ + ", ".join(f"<code>{f}</code>" for f, _ in db.ORDER_FIELDS) + """
Trường dùng cho /suasale: """ + ", ".join(f"<code>{f}</code>" for f, _ in db.SALE_FIELDS)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # link bấm từ mã vận đơn: t.me/<bot>?start=tr_<số đơn>
    if context.args and context.args[0].startswith("tr_") and context.args[0][3:].isdigit():
        await send_tracking(update.message, order_id=int(context.args[0][3:]))
        try:  # xóa tin "/start tr_…" Telegram tự gửi khi bấm link cho đỡ rối
            await update.message.delete()
        except Exception:
            pass
        return
    # link "Hoàn thành" trong /ds, /dsthanhtoan: t.me/<bot>?start=done_<số đơn>
    if context.args and context.args[0].startswith("done_") and context.args[0][5:].isdigit():
        await ask_done(update.message, context, int(context.args[0][5:]))
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    # link "Đã nhận hàng" dưới đơn Đã đặt: t.me/<bot>?start=nhan_<số đơn> → Đã nhận
    if context.args and context.args[0].startswith("nhan_") and context.args[0][5:].isdigit():
        await mark_received(update.message, int(context.args[0][5:]))
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    # link "Thanh toán" trong /ds, /dsdadat: t.me/<bot>?start=pay_<số đơn>
    if context.args and context.args[0].startswith("pay_") and context.args[0][4:].isdigit():
        await send_pay(update.message, int(context.args[0][4:]))
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    # link trong /saleall: start=slink_<id> (thêm link), start=stog_<id> (bật/tắt sale)
    if context.args and (m := re.fullmatch(r"(slink|stog)_(\d+)", context.args[0])):
        if m[1] == "slink":
            await send_sale_links(update.message, context, int(m[2]))
        else:
            await toggle_sale_active(update.message, int(m[2]))
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    if context.args and context.args[0].lower() == "full":
        # HELP_FULL dài hơn giới hạn 4096 ký tự → gửi theo từng mục (tách trước tiêu đề <b>…</b>)
        await reply_long(update.message, [sec + "\n" for sec in re.split(r"\n\n(?=<b>)", HELP_FULL)])
        return
    await update.message.reply_html(HELP)


# lệnh xem danh sách riêng cho từng trạng thái
STATUS_LIST_CMDS = {
    "dsdadat": db.STATUS_ORDERED,
    "dsdanhan": db.STATUS_RECEIVED,
    "dsthanhtoan": db.STATUS_PAID,
    "dshoanthanh": db.STATUS_DONE,
    "dshuy": db.STATUS_CANCELED,
}


def make_list(fixed_status: str | None = None):
    """
    /ds [n]              toàn bộ đơn (không tính đơn hủy); có n thì chỉ n đơn mới nhất
    /ds <trạng thái> [n] chỉ đơn ở trạng thái đó, vd: /ds dat, /ds thanh toan 20
    /dsdadat, /dsthanhtoan, /dshoanthanh, /dshuy [n]  – mỗi lệnh 1 trạng thái
    """
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        nums = [a for a in context.args if a.isdigit()]
        words = " ".join(a for a in context.args if not a.isdigit())
        n = int(nums[0]) if nums else None  # không ghi số → toàn bộ
        status = fixed_status
        if words and not fixed_status:
            status = parse_status(words)
            if not status:
                await update.message.reply_text(
                    "⚠️ Trạng thái không hợp lệ. Dùng: dat, thanh toan, hoan thanh, huy\nVí dụ: /ds dat 20\n"
                    "Hoặc: /dsdadat, /dsthanhtoan, /dshoanthanh, /dshuy")
                return
        rows = db.list_orders(n, status=status)
        if not rows:
            await update.message.reply_html(
                f"Không có đơn {status_text(status)} nào." if status else "Chưa có đơn nào.")
            return
        if status:
            title = (f"📋 <b>{len(rows)} đơn {status_text(status)}</b>" + (" gần nhất" if n else ""))
            counts = ""
        else:
            title = (f"📋 <b>{'Toàn bộ ' if not n else ''}{len(rows)} đơn{' gần nhất' if n else ''}</b>"
                     " (không tính đơn hủy, mới nhất trước)")
            counts = " · ".join(f"{status_text(st)}: {sum(r['trang_thai'] == st for r in rows)}"
                                for st in db.STATUSES if st != db.STATUS_CANCELED) + "\n"
        await reply_long(update.message, [title, counts, *(fmt_order_short(r) for r in rows)])
    return handler


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Cú pháp: /tim <từ khóa>")
        return
    rows = db.search_orders(" ".join(context.args))
    if not rows:
        await update.message.reply_text("Không tìm thấy đơn nào.")
        return
    await reply_long(update.message, [fmt_order_short(r) for r in rows])


def fmt_customer_order(r, show_name: bool = False) -> str:
    e = lambda v: html.escape(v) if v else "—"
    name = f"👤 {e(r['ten_nguoi_dat'])} · " if show_name else ""
    lines = [
        f"{STATUS_ICON.get(r['trang_thai'], '')} <code>#{r['id']}</code> "
        f"{r['created_at'][8:10]}/{r['created_at'][5:7]} · {name}"
        f"<b>{e(r['ten_sp'])}</b>{qty_text(r)} ({r['nguon_don'] or '—'})",
        f"   💰 Shop thu {money(r['gia_thu']) if r['gia_thu'] else 'chưa bán'} · Đặt {money(r['gia_dat'])}"
        f" · Công {money(r['cong_dat'])} → <b>{margin_text(r['gia_thu'], r['gia_dat'], r['cong_dat'])}</b>",
        f"   🔖 Mã đơn: {tracking_html(r)} · 🚚 {e(r['don_vi_vc'])}",
        f"   📞 {e(r['sdt'])}{phone_tag(r['sdt'])} · 🏠 {e(r['dia_chi'])}",
    ]
    return "\n".join(lines)


async def cmd_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Cú pháp: /khach <tên hoặc SĐT>")
        return
    keyword = " ".join(context.args)
    rows = db.customer_orders(keyword)
    if not rows:
        await update.message.reply_text(f"Không có đơn nào của khách “{keyword}”.")
        return

    # gom theo tên khách (không phân biệt hoa thường)
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["ten_nguoi_dat"].strip().lower(), []).append(r)

    lines = []
    for orders in groups.values():
        phones = sorted({o["sdt"] for o in orders if o["sdt"]})
        sold = [o for o in orders if o["trang_thai"] == db.STATUS_DONE]
        unsold = [o for o in orders if o["trang_thai"] in db.UNSOLD_STATUSES]
        thu = sum(o["gia_thu"] or 0 for o in sold)
        dat = sum(o["gia_dat"] or 0 for o in sold)
        cong = sum(o["cong_dat"] or 0 for o in sold)
        von = sum((o["gia_dat"] or 0) + (o["cong_dat"] or 0) for o in unsold)
        lines.append(f"👤 <b>{html.escape(orders[0]['ten_nguoi_dat'])}</b>"
                     + (f" · 📞 {html.escape(', '.join(phones))}" if phones else "")
                     + f" · <b>{len(orders)} đơn</b>")
        lines += [fmt_customer_order(o) + "\n" for o in orders]
        lines.append(f"✅ <b>Đã bán {len(sold)} đơn:</b> shop thu <b>{money(thu)}</b> · đặt {money(dat)} · "
                     f"công {money(cong)} · chênh lệch <b>{money(margin(thu, dat, cong))}</b>")
        if unsold:
            lines.append(f"⏳ <b>Chưa bán {len(unsold)} đơn:</b> vốn (đặt + công) {money(von)}")
        lines.append("━━━━━━━━━━━━")
    await reply_long(update.message, lines[:-1])


def make_status_list(statuses: list[str], title: str, cmd: str, footer: str):
    """Danh sách đơn theo trạng thái. /<cmd>: mỗi đơn 1 dòng; /<cmd> full: chi tiết."""
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        rows = db.orders_by_status(statuses)
        if not rows:
            await update.message.reply_text("🎉 Không có đơn nào.")
            return
        full = bool(context.args) and context.args[0].lower() in ("full", "chitiet", "ct")
        today = datetime.now().date()

        def age(r):
            days = (today - datetime.strptime(r["created_at"][:10], "%Y-%m-%d").date()).days
            return "hôm nay" if days == 0 else f"{days} ngày"

        von = sum((r["gia_dat"] or 0) + (r["cong_dat"] or 0) for r in rows)
        lines = [f"{title} – {len(rows)} đơn (cũ nhất trước)\n"]
        if full:
            for r in rows:
                lines.append(fmt_customer_order(r, show_name=True) + f"\n   🕒 Đặt {age(r)}\n")
        else:
            e = lambda v: html.escape(v) if v else "—"
            for r in rows:
                lines.append(f"{STATUS_ICON.get(r['trang_thai'], '')} <code>#{r['id']}</code> "
                             f"{e(r['ten_nguoi_dat'])} · {e(r['ten_sp'])} · "
                             f"vốn {money((r['gia_dat'] or 0) + (r['cong_dat'] or 0))} · "
                             f"🔖 {tracking_html(r)} · 🚚 {e(r['don_vi_vc'])} · 🕒 {age(r)}")
        if db.STATUS_CANCELED not in statuses:  # đơn hủy không còn là vốn
            lines.append(f"\n💸 Tổng vốn (đặt + công): <b>{money(von)}</b>")
        if not full:
            lines.append(f"Xem chi tiết: /{cmd} full")
        lines.append(footer)
        await reply_long(update.message, lines)
    return handler


async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ban <id> <giá shop thu 1 cái> [số cái]: ghi giá bán và chuyển đơn sang Hoàn thành; số cái ít hơn số lượng
    của đơn (vd /ban 129 2tr65 1c) = bán 1 phần, phần còn lại tách thành đơn mới."""
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text("Cú pháp: /ban <id> <giá shop thu 1 cái> [số cái]\n"
                                        "Ví dụ: /ban 5 250 (= 250.000; đơn x2 → 500.000)\n"
                                        "Bán 1 phần đơn nhiều cái: /ban 129 2tr65 1c (bán 1 cái, phần còn lại tách đơn mới)")
        return
    price_text, sell_qty = split_sell_qty(" ".join(context.args[1:]))
    rec_id, price = int(context.args[0]), parse_money(price_text)
    if not price:
        await update.message.reply_text("⚠️ Giá không hợp lệ. Ví dụ: 250 (= 250.000), 1.2tr")
        return
    r = db.get("orders", rec_id)
    if not r:
        await update.message.reply_text("Không tìm thấy đơn.")
        return
    if r["trang_thai"] == db.STATUS_CANCELED:
        await update.message.reply_text(f"Đơn #{rec_id} đã hủy. Gõ /datlai {rec_id} trước nếu muốn bán.")
        return
    await sell_order(update.message, r, price, sell_qty)


async def sell_order(message, r, unit_price: int, qty: int | None = None):
    """Bán: qty bỏ trống / = số lượng → cả đơn (giá 1 cái × số lượng, Hoàn thành); qty nhỏ hơn → bán 1 phần, phần còn
    lại tách thành đơn mới (db.sell_part, người dùng yêu cầu 11/2026)."""
    n = order_qty(r)
    if qty is not None and not 1 <= qty <= n:
        await message.reply_text(f"⚠️ Đơn #{r['id']} có {n} cái – số cái bán phải từ 1 tới {n}.")
        return
    if qty is not None and qty < n:
        try:
            new_id = db.sell_part(r["id"], qty, unit_price)
        except ValueError as e:
            await message.reply_text(f"⚠️ {e}")
            return
        rest = db.get("orders", new_id)
        await message.reply_html(
            sold_text(db.get("orders", r["id"]))
            + f"\n✂️ {n - qty} cái còn lại → đơn mới <code>#{new_id}</code> · {status_text(rest['trang_thai'])}"
              f" · đặt {money(rest['gia_dat'])} · công {money(rest['cong_dat'])} (/xem {new_id})")
        return
    db.update("orders", r["id"], "gia_thu", price_total(r, unit_price)[0])  # giá 1 cái → tổng cả đơn
    mark_done(r["id"])
    await message.reply_html(sold_text(db.get("orders", r["id"])))


async def cmd_split(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tach <id> [số cái]: tách đơn nhiều cái (không bán) – không ghi số cái = tách hết thành từng đơn 1 cái; có số cái
    = tách chừng đó cái ra 1 đơn mới. Giá đặt / công / giá shop thu chia theo số cái (db.split_order)."""
    args = context.args
    if not args or not args[0].isdigit() or (len(args) > 1 and not args[1].isdigit()):
        await update.message.reply_text("Cú pháp: /tach <id> [số cái]\nVí dụ: /tach 129 (đơn 2 cái → 2 đơn 1 cái), "
                                        "/tach 50 2 (tách 2 cái ra 1 đơn mới)")
        return
    rec_id = int(args[0])
    try:
        new_ids = db.split_order(rec_id, int(args[1])) if len(args) > 1 else db.split_all(rec_id)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    new_ids = new_ids if isinstance(new_ids, list) else [new_ids]
    lines = [f"✂️ <b>Đã tách đơn #{rec_id}</b> ({len(new_ids) + 1} đơn, mỗi đơn giữ trạng thái cũ):"]
    for i in [rec_id, *new_ids]:
        o = db.get("orders", i)
        lines.append(f"• <code>#{i}</code> {o['so_luong'] or 1} cái · đặt {money(o['gia_dat'])} · công {money(o['cong_dat'])}"
                     f" · thu {money(o['gia_thu'])}" + (f" · tách từ #{o['tach_tu']}" if o["tach_tu"] else ""))
    await update.message.reply_html("\n".join(lines))


def sold_text(r) -> str:
    return (f"✅ Đơn <code>#{r['id']}</code> {html.escape(r['ten_sp'] or '')} → <b>Hoàn thành</b>\n"
            f"Shop thu {money(r['gia_thu'])}{f' ({order_qty(r)} cái)' if order_qty(r) > 1 else ''} − đặt {money(r['gia_dat'])} − công {money(r['cong_dat'])} = "
            f"<b>{money(margin(r['gia_thu'], r['gia_dat'], r['cong_dat']))}</b>")


async def ask_done(message, context, order_id: int):
    """Link HOÀN THÀNH: có giá shop thu thì hỏi xác nhận, chưa có thì hỏi giá bán (gõ → Hoàn thành)."""
    r = db.get("orders", order_id)
    if not r:
        await message.reply_text("Không tìm thấy đơn.")
        return
    if r["trang_thai"] == db.STATUS_CANCELED:
        await message.reply_text(f"Đơn #{order_id} đã hủy. Gõ /datlai {order_id} trước nếu muốn bán.")
        return
    head = (f"✅ <b>Hoàn thành đơn #{r['id']}</b> · {status_text(r['trang_thai'])}\n"
            f"👤 {html.escape(r['ten_nguoi_dat'] or '—')} · 📦 {html.escape(r['ten_sp'] or '—')}\n"
            f"💸 Vốn {money((r['gia_dat'] or 0) + (r['cong_dat'] or 0))} "
            f"(đặt {money(r['gia_dat'])} + công {money(r['cong_dat'])})")
    if r["gia_thu"]:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Bán {money(r['gia_thu'])} → Hoàn thành", callback_data=f"done:ok:{r['id']}")],
            [InlineKeyboardButton("✏️ Nhập giá bán khác", callback_data=f"done:price:{r['id']}")]])
        await message.reply_html(head + f"\n💰 Giá shop thu đang ghi: <b>{money(r['gia_thu'])}</b>", reply_markup=kb)
    else:
        context.user_data["pending_edit"] = ("orders", r["id"], "gia_thu")
        context.user_data["done_after_price"] = r["id"]
        price, text = suggested_price(r)
        await message.reply_html(head + (f"\n📈 Giá thu gợi ý: {text}" if price else "")
                                 + "\n\n💰 <b>Bán được bao nhiêu?</b> Gõ giá shop thu "
                                 "(vd: 4tr5, 250 = 250.000) – hoặc /huy để thôi." + unit_price_hint(r))


async def on_done_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, action, order_id = query.data.split(":")
    r = db.get("orders", int(order_id))
    if not r:
        await query.edit_message_text("Không tìm thấy đơn.")
        return
    if action == "price":
        context.user_data["pending_edit"] = ("orders", r["id"], "gia_thu")
        context.user_data["done_after_price"] = r["id"]
        await query.edit_message_text(f"💰 Gõ giá shop thu cho đơn #{r['id']} (vd: 4tr5) – hoặc /huy để thôi."
                                      + unit_price_hint(r), parse_mode="HTML")
        return
    mark_done(r["id"])
    await query.edit_message_text(sold_text(db.get("orders", r["id"])), parse_mode="HTML")


def make_set_status(status: str, cmd: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        ids = [int(a) for a in context.args if a.isdigit()]
        if not ids:
            await update.message.reply_text(f"Cú pháp: /{cmd} <id> [id ...]")
            return
        done, missing, skipped = [], [], []
        for rec_id in ids:
            r = db.get("orders", rec_id)
            if not r:
                missing.append(f"#{rec_id}")
            elif r["trang_thai"] == db.STATUS_DONE and status != db.STATUS_CANCELED:
                skipped.append(f"#{rec_id}")  # đã bán; muốn lùi thì /sua <id> gia_thu 0
            else:
                db.update("orders", rec_id, "trang_thai", status)
                done.append(f"#{rec_id}")
        msg = f"{status_text(status)}: {', '.join(done)}" if done else ""
        if skipped:
            msg += f"\nĐã hoàn thành (bỏ qua): {', '.join(skipped)}"
        if missing:
            msg += f"\nKhông tìm thấy: {', '.join(missing)}"
        await update.message.reply_text(msg.strip())
    return handler


def short_signed(n: int) -> str:
    """Tiền viết tắt có dấu: -14947000 → -14tr947 · 450000 → +450."""
    return ("-" if n < 0 else "+" if n > 0 else "") + money_short(abs(n)) if n else "0"


def stats_table(fc: dict) -> list[str]:
    """Bảng chữ đều (thẻ <pre>) theo trạng thái: Đơn | Chi | Thu | Lãi, có dòng Tổng (thu/lãi dự kiến)."""
    w = (10, 3, 9, 9, 9)
    fmt = lambda cells: "".join(c.ljust(w[0]) if i == 0 else c.rjust(w[i]) for i, c in enumerate(cells))
    out = [fmt(["", "Đơn", "Chi", "Thu", "Lãi"]), "─" * sum(w)]
    tot = dict.fromkeys(("so_don", "chi", "thu", "lai"), 0)
    for st, r in fc.items():
        for k in tot:
            tot[k] += r[k]
        out.append(fmt([st, str(r["so_don"]), money_short(r["chi"]),
                        money_short(r["thu"]) if r["thu"] else "–",
                        short_signed(r["lai"]) if r["thu"] or r.get("thuong") else "–"]))
    out += ["─" * sum(w), fmt(["Tổng", str(tot["so_don"]), money_short(tot["chi"]), money_short(tot["thu"]),
                               short_signed(tot["lai"])])]
    return [html.escape(line) for line in out]


def pct(part: int, whole: int) -> str:
    return f"{round(part * 100 / whole)}%" if whole else "0%"


def fmt_month_stats(month: str) -> str:
    """month: 'YYYY-MM'. Cùng số liệu với trang Tổng quan trên web (db.month_forecast)."""
    y, m = month.split("-")
    fc = db.month_forecast(month)
    lines = [f"📊 <b>THỐNG KÊ THÁNG {m}/{y}</b>", ""]
    so_don = sum(r["so_don"] for r in fc.values())
    if not so_don:
        return "\n".join(lines + ["Không có đơn nào trong tháng."])
    done = fc[db.STATUS_DONE]
    tong = {k: sum(r[k] for r in fc.values()) for k in ("gia_dat", "cong", "thuong", "chi")}
    lines += [
        f"🧾 Số đơn: <b>{so_don}</b> <i>(không tính hủy)</i>",
        "   " + " · ".join(f"{STATUS_ICON[st]} {n} {st}" for st, n in db.month_status_counts(month).items()),
        "",
        f"💸 <b>Đã chi trong tháng: {money_short(tong['chi'])}</b>",
        f"   🛒 Tiền hàng: {money_short(tong['gia_dat'])} ({pct(tong['gia_dat'], tong['chi'])})",
        f"   🛠 Tiền công: {money_short(tong['cong'])} ({pct(tong['cong'], tong['chi'])})",
    ]
    if tong["thuong"]:
        lines.append(f"   🎁 Thưởng chiến dịch: {money_short(tong['thuong'])} ({pct(tong['thuong'], tong['chi'])})")
    # vốn đã bỏ ra mà chưa bán: mọi tháng
    allm = db.month_forecast()
    out = [(st, allm[st]) for st in db.UNSOLD_STATUSES]
    out_chi = sum(r["chi"] for _, r in out)
    out_thu = sum(r["thu"] for _, r in out)
    out_thieu = sum(r["thieu_gia"] for _, r in out)
    out_n = sum(r["so_don"] for _, r in out)
    lines += ["", f"⏳ <b>Vốn chưa thu về: {money_short(out_chi)}</b> <i>(mọi tháng)</i>"]
    lines += [f"   {STATUS_ICON[st]} {st} · {r['so_don']} đơn: {money_short(r['chi'])}" for st, r in out]
    lines.append(f"   <i>chưa đơn nào có giá shop thu</i>" if out_thieu == out_n else
                 f"   giá shop thu đã nhập: {money_short(out_thu)}"
                 + (f" <i>({out_thieu}/{out_n} đơn chưa có giá shop thu)</i>" if out_thieu else ""))
    # dự kiến = đơn chưa bán của tháng (không gồm Hoàn thành)
    unsold = {st: fc[st] for st in db.UNSOLD_STATUSES}
    du = {k: sum(r[k] for r in unsold.values())
          for k in ("so_don", "chi", "thu", "chi_co_gia", "thuong", "so_moc", "lai", "thieu_gia")}
    tn = lambda r: f" − {money_short(r['thuong'])} 🎁" if r["thuong"] else ""
    lines += [
        "",
        "✅ <b>THỰC TẾ</b> · đơn Hoàn thành",
        f"   💰 Thu: <b>{money_short(done['thu'])}</b>",
        f"      <i>= tổng giá shop thu của {done['so_don']} đơn Hoàn thành</i>",
        f"   📈 Lãi: <b>{short_signed(done['lai'])}</b>",
        "      <i>= thu − (giá đặt + công) − thưởng chiến dịch của đơn Hoàn thành</i>",
        f"      <i>= {money_short(done['thu'])} − ({money_short(done['gia_dat'])} + {money_short(done['cong'])}){tn(done)}</i>",
        *([f"      <i>🎁 thưởng {done['so_moc']} lần đạt mốc (tính vào đơn mốc)</i>"] if done["thuong"] else []),
        "",
        "🔮 <b>DỰ KIẾN</b> · đơn chưa bán (Đã đặt + Đã nhận + Thanh toán)",
        f"   💰 Thu: <b>{money_short(du['thu'])}</b>",
        f"      <i>= tổng giá shop thu đã nhập của {du['so_don'] - du['thieu_gia']}/{du['so_don']} đơn chưa bán</i>",
    ]
    if du["thieu_gia"]:
        lines.append(f"      ⚠️ <i>{du['thieu_gia']} đơn chưa có giá shop thu → tính 0</i>")
    lines += [
        f"   🎯 Lãi: <b>{short_signed(du['lai'])}</b>",
        "      <i>= thu − (giá đặt + công) của đơn đã có giá shop thu − thưởng chiến dịch</i>",
    ]
    if du["thu"] or du["thuong"]:
        lines.append(f"      <i>= {money_short(du['thu'])} − {money_short(du['chi_co_gia'])}{tn(du)}</i>")
    if du["thuong"]:
        lines.append(f"      <i>🎁 thưởng {du['so_moc']} lần đạt mốc (đơn mốc đã trả công, chưa bán)</i>")
    if du["thieu_gia"]:
        lines.append(f"      ⚠️ <i>{du['thieu_gia']} đơn chưa có giá shop thu "
                     f"(chi {money_short(du['chi'] - du['chi_co_gia'] - du['thuong'])}) không tính</i>")
    lines.append("<pre>" + "\n".join(stats_table(unsold)) + "</pre>")
    lines.append("<i>Chi = giá đặt + công + 🎁 thưởng chiến dịch (tính vào đơn mốc) · "
                 "Thu = tổng giá shop thu đã nhập (chưa nhập = 0) · "
                 "Lãi = thu − (giá đặt + công) của đơn đã có giá shop thu − thưởng</i>")
    return "\n".join(lines)


def shift_month(month: str, delta: int) -> str:
    y, m = map(int, month.split("-"))
    m += delta
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def stats_keyboard(month: str):
    now = datetime.now().strftime("%Y-%m")
    rows = []
    if n := len(db.month_orders_not_done(month)):
        rows.append([InlineKeyboardButton(f"📋 Xem {n} đơn chưa hoàn thành", callback_data=f"statsopen:{month}")])
    nav = [InlineKeyboardButton("◀ Tháng trước", callback_data=f"stats:{shift_month(month, -1)}")]
    if month < now:
        nav.append(InlineKeyboardButton("Tháng sau ▶", callback_data=f"stats:{shift_month(month, 1)}"))
    rows.append(nav)
    return InlineKeyboardMarkup(rows)


async def on_stats_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút dưới /thongke: liệt kê đơn chưa hoàn thành của tháng."""
    query = update.callback_query
    await query.answer()
    month = query.data.split(":", 1)[1]
    rows = db.month_orders_not_done(month)
    y, m = month.split("-")
    if not rows:
        await query.message.reply_text(f"🎉 Tháng {m}/{y} không còn đơn chưa hoàn thành.")
        return
    e = lambda v: html.escape(v) if v else "—"
    von = sum((r["gia_dat"] or 0) + (r["cong_dat"] or 0) for r in rows)
    lines = [f"📋 <b>Đơn chưa hoàn thành tháng {m}/{y}</b> – {len(rows)} đơn\n"]
    for r in rows:
        lines.append(f"{STATUS_ICON.get(r['trang_thai'], '')} <code>#{r['id']}</code> {e(r['ten_nguoi_dat'])}"
                     f" · {e(r['ten_sp'])} · 🛒 {e(r['nguon_don'])} · vốn <b>{money((r['gia_dat'] or 0) + (r['cong_dat'] or 0))}</b>"
                     f" · {r['created_at'][8:10]}/{r['created_at'][5:7]}")
    lines.append(f"\n💸 Vốn đang nằm: <b>{money(von)}</b>")
    lines.append("<i>Bán xong: /ban &lt;id&gt; &lt;giá&gt; · Đã nhận hàng + trả công: /thanhtoan &lt;id&gt;</i>")
    await reply_long(query.message, lines)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/thongke – tháng này · /thongke 9 hoặc 9/2026 – tháng khác."""
    now = datetime.now()
    month = now.strftime("%Y-%m")
    if context.args:
        m = re.fullmatch(r"(\d{1,2})(?:[/-](\d{4}))?", context.args[0])
        if not m or not 1 <= int(m[1]) <= 12:
            await update.message.reply_text("Cú pháp: /thongke (tháng này) hoặc /thongke 9 hoặc /thongke 9/2026")
            return
        month = f"{int(m[2] or now.year):04d}-{int(m[1]):02d}"
    await update.message.reply_html(fmt_month_stats(month), reply_markup=stats_keyboard(month))


def parse_day(text: str) -> str | None:
    """'5/11', '05/11/26', '5-11-2026', liền số '0511' / '051126' / '05112026' → 'YYYY-MM-DD'
    (thiếu năm = năm nay). Sai → None."""
    m = (re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2}|\d{4}))?", text.strip())
         or re.fullmatch(r"(\d{2})(\d{2})(\d{2}|\d{4})?", text.strip()))
    if not m:
        return None
    y = int(m[3]) + 2000 if m[3] and len(m[3]) == 2 else int(m[3] or datetime.now().year)
    try:
        return datetime(y, int(m[2]), int(m[1])).strftime("%Y-%m-%d")
    except ValueError:
        return None


def fmt_by_person(tu: str | None, den: str | None) -> list[str]:
    """Đơn ⏳ Đã đặt tạo trong tu..den (YYYY-MM-DD, tính cả 2 đầu) gom theo người đặt: số đơn, tổng giá đặt
    (như bảng 👤 Theo người đặt trên web)."""
    rs = [r for r in db.list_orders(limit=None, status=db.STATUS_ORDERED)
          if (not tu or r["created_at"][:10] >= tu) and (not den or r["created_at"][:10] <= den)]
    when = f"{dmy(tu) if tu else 'đầu'} → {dmy(den) if den else 'nay'}" if tu or den else "mọi lúc"
    head = [f"👤 <b>ĐƠN ĐÃ ĐẶT THEO NGƯỜI ĐẶT</b>", f"📅 {when}", ""]
    if not rs:
        return head + ["Không có đơn ⏳ Đã đặt nào trong khoảng này."]
    people: dict[str, dict] = {}
    for r in rs:
        p = people.setdefault(db.name_key(r["ten_nguoi_dat"] or ""), {"ten": r["ten_nguoi_dat"] or "(chưa ghi)", "don": 0, "gia_dat": 0})
        p["don"] += 1
        p["gia_dat"] += r["gia_dat"] or 0
    ps = sorted(people.values(), key=lambda p: (-p["gia_dat"], -p["don"]))
    w = (max(len("Người đặt"), *(len(p["ten"]) for p in ps)) + 1, 4, 10)  # tên đầy đủ, không cắt
    fmt = lambda a, b, c: a.ljust(w[0]) + b.rjust(w[1]) + c.rjust(w[2])
    rows = [fmt(p["ten"], str(p["don"]), money_short(p["gia_dat"])) for p in ps]
    total = fmt("Tổng", str(len(rs)), money_short(sum(p["gia_dat"] for p in ps)))
    out = head + [f"{len(ps)} người · {len(rs)} đơn"]
    # mỗi khối <pre> tối đa 60 dòng để reply_long không cắt giữa thẻ
    for i in range(0, len(rows), 60):
        block = ([fmt("Người đặt", "Đơn", "Giá đặt"), "─" * sum(w)] if i == 0 else []) + rows[i:i + 60]
        if i + 60 >= len(rows):
            block += ["─" * sum(w), total]
        out.append("<pre>" + "\n".join(html.escape(line) for line in block) + "</pre>")
    return out


def fmt_campaign(c) -> list[str]:
    """1 chiến dịch thưởng săn sale (tạo / sửa trên web, trang Đơn hàng, mục 🎁 Thưởng săn sale): thưởng từng người đặt trong <pre>."""
    st = db.campaign_stats(c)
    rule = f"Cứ {c['moc_don']} đơn thưởng {money_short(c['thuong'])}"
    out = [f"🎁 <b>{html.escape(c['ten'])}</b>  <code>#{c['id']}</code>  " + ("✅ đang bật" if c["active"] else "⏸ đã tắt"),
           f"📅 {dmy(c['tu'])} → {dmy(c['den']) if c['den'] else 'nay'} · {rule}",
           f"💰 Tổng thưởng <b>{money(st['tong_thuong'])}</b> · {st['tong_dat']} đơn đạt"
           + (f" · {st['tong_cho']} đơn chưa trả công (chưa tính)" if st["tong_cho"] else "")
           + (f"\n🧾 Tổng công <b>{money(st['tong_cong'])}</b>"
              + (f" (chưa trả {money(st['tong_cong_chua_tra'])})" if st["tong_cong_chua_tra"] else "") if st["tong_cong"] else "")]
    ps = st["people"]
    if not ps:
        return out + ["Chưa có đơn nào gán vào chiến dịch này (chọn ở bước 🎁 của /them, /sua hoặc trên web)."]
    w = (max(len("Người đặt"), *(len(p["ten"]) for p in ps)) + 1, 4, 7, 8, 8)
    fmt = lambda a, b, cg, c_, d: a.ljust(w[0]) + b.rjust(w[1]) + cg.rjust(w[2]) + c_.rjust(w[3]) + d.rjust(w[4])
    rows = [fmt(p["ten"], str(p["dat"]), money_short(p["cong"]) if p["cong"] else "-",
                money_short(p["thuong"]) if p["thuong"] else "-", f"còn {p['con']}") for p in ps]
    for i in range(0, len(rows), 60):
        block = ([fmt("Người đặt", "Đơn", "Công", "Thưởng", "Tới lần"), "─" * sum(w)] if i == 0 else []) + rows[i:i + 60]
        out.append("<pre>" + "\n".join(html.escape(line) for line in block) + "</pre>")
    return out


async def cmd_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/thuongsale: chiến dịch đang bật (không có thì chiến dịch gần nhất); /thuongsale <id>; /thuongsale all = mọi chiến dịch."""
    cs = db.list_campaigns()
    arg = " ".join(context.args).strip().lower().lstrip("#")
    if not cs:
        await update.message.reply_text("Chưa có chiến dịch thưởng nào. Tạo trên web, trang Đơn hàng, mục 🎁 Thưởng săn sale.")
        return
    if arg == "all":
        pick = cs
    elif arg.isdigit():
        pick = [c for c in cs if c["id"] == int(arg)]
        if not pick:
            await update.message.reply_text(f"Không có chiến dịch #{arg}. Xem hết: /thuongsale all")
            return
    else:
        pick = [c for c in cs if c["active"]] or cs[:1]
    lines = []
    for c in pick:
        lines += fmt_campaign(c) + [""]
    others = [c for c in cs if c not in pick]
    if others:
        lines.append("Chiến dịch khác: " + " · ".join(f"/thuongsale {c['id']} ({html.escape(c['ten'])})" for c in others[:10]))
    await reply_long(update.message, lines)


TRASHIP_ALL = "♾ Mọi lúc"


def traship_buttons(not_before: str | None = None) -> dict[str, str]:
    """Nút chọn nhanh: hôm nay, 1 ngày trước, 2 ngày trước → {nhãn: YYYY-MM-DD}; bỏ ngày trước not_before."""
    today = datetime.now()
    out = {}
    for n, name in ((0, "Hôm nay"), (1, "1 ngày trước"), (2, "2 ngày trước")):
        d = today - timedelta(days=n)
        day = d.strftime("%Y-%m-%d")
        if not not_before or day >= not_before:
            out[f"{name} ({d:%d/%m})"] = day
    return out


def dmy(day: str) -> str:
    return f"{day[8:10]}/{day[5:7]}/{day[:4]}"


async def cmd_traship(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/traship [từ ngày] [đến ngày]: đơn Đã đặt theo người đặt (1 ngày = từ ngày đó tới nay).
    Không ghi ngày → bot hỏi ngày bắt đầu rồi ngày kết thúc (pending_traship, on_traship_text)."""
    if not context.args:
        context.user_data.pop("pending_edit", None)
        buttons = traship_buttons()
        context.user_data["pending_traship"] = {"buttons": buttons}
        kb = ReplyKeyboardMarkup([list(buttons), [TRASHIP_ALL]], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_html("📅 Chọn <b>ngày bắt đầu</b>, hoặc gõ ngày/tháng/năm (vd 1/11/2026):", reply_markup=kb)
        return
    days = [parse_day(a) for a in context.args[:2]]
    if None in days:
        await update.message.reply_text("Cú pháp: /traship [từ ngày] [đến ngày], vd /traship 1/11/2026 4/11/2026 – hoặc chỉ /traship để bot hỏi ngày")
        return
    tu, den = (days + [None])[:2]
    if den and den < tu:
        await update.message.reply_text(f"⚠️ Ngày kết thúc {dmy(den)} trước ngày bắt đầu {dmy(tu)}. Gõ lại cho đúng thứ tự.")
        return
    await send_traship(update.message, tu, den)


async def send_traship(message, tu, den):
    lines = fmt_by_person(tu, den)
    if len(text := "\n".join(lines)) < 4000:
        await message.reply_html(text, reply_markup=ReplyKeyboardRemove())
    else:  # rất nhiều người: reply_long cắt theo từng khối <pre>
        await reply_long(message, lines)


async def on_traship_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Ngày chọn / gõ sau /traship: bước 1 ngày bắt đầu, bước 2 ngày kết thúc (không được trước ngày bắt đầu).
    True = đã xử lý tin này."""
    pending = context.user_data.get("pending_traship")
    if pending is None:
        return False
    text = update.message.text.strip()
    if "tu" not in pending and text == TRASHIP_ALL:
        context.user_data.pop("pending_traship", None)
        await send_traship(update.message, None, None)
        return True
    day = pending["buttons"].get(text) or parse_day(text)
    if not day:
        await update.message.reply_text("⚠️ Ngày không đúng: bấm nút hoặc gõ ngày/tháng/năm (vd 1/11/2026), /huy để thôi.")
        return True
    if "tu" not in pending:
        if day > datetime.now().strftime("%Y-%m-%d"):
            await update.message.reply_text(f"⚠️ Ngày bắt đầu {dmy(day)} là ngày trong tương lai, chưa có đơn nào. Chọn lại.")
            return True
        buttons = traship_buttons(not_before=day)
        pending.update(tu=day, buttons=buttons)
        kb = ReplyKeyboardMarkup([list(buttons)], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_html(f"📅 Từ <b>{dmy(day)}</b>. Chọn <b>ngày kết thúc</b>, hoặc gõ ngày/tháng/năm:", reply_markup=kb)
        return True
    if day < pending["tu"]:
        kb = ReplyKeyboardMarkup([list(pending["buttons"])], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_html(f"⚠️ Ngày kết thúc {dmy(day)} trước ngày bắt đầu {dmy(pending['tu'])}. Chọn lại ngày kết thúc:",
                                        reply_markup=kb)
        return True
    context.user_data.pop("pending_traship", None)
    await send_traship(update.message, pending["tu"], day)
    return True


async def on_stats_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    month = query.data.split(":", 1)[1]
    await query.edit_message_text(fmt_month_stats(month), parse_mode="HTML", reply_markup=stats_keyboard(month))


# ---------- tra cứu hành trình vận chuyển ----------

def fmt_tracking(res: "tracking.TrackResult", order=None) -> str:
    lines = [f"🚚 <b>{html.escape(res.carrier)}</b> · <code>{html.escape(res.tracking_no)}</code>"]
    if order:
        lines.append(f"🧾 Đơn #{order['id']} · 👤 {html.escape(order['ten_nguoi_dat'] or '—')}"
                     f" · 📦 {html.escape(order['ten_sp'] or '—')}")
    if not res.ok:
        lines.append(f"\n⚠️ {html.escape(res.error)}")
        if res.web_url:
            lines.append(f'🌐 <a href="{html.escape(res.web_url)}">Mở trang tra cứu {html.escape(res.carrier)}</a>')
        return "\n".join(lines)
    if not res.events:
        lines.append("\nChưa có thông tin hành trình.")
        return "\n".join(lines)
    latest = res.events[0]
    lines.append(f"\n📍 <b>{html.escape(res.milestone.upper() or latest.description.upper())}</b>")
    lines.append(f"<i>{html.escape(latest.description)} — {latest.time:%H:%M %d/%m}</i>")
    if res.eta:
        lines.append(f"📅 Dự kiến giao: <b>{res.eta:%d/%m/%Y}</b>")
    lines.append("\n<b>Hành trình:</b>")
    for ev in res.events[:10]:
        bad = "không thành công" in ev.description.lower() or "thất bại" in ev.description.lower()
        desc = f"⚠️ <b>{html.escape(ev.description)}</b>" if bad else html.escape(ev.description)
        lines.append(f"▸ {ev.time:%d/%m %H:%M} · {desc}")
    if len(res.events) > 10:
        lines.append(f"<i>… và {len(res.events) - 10} mốc cũ hơn</i>")
    if res.stale:
        t = datetime.fromisoformat(res.saved_at)
        lines.append(f"\n⚠️ Tra lại lỗi: {html.escape(res.error)}")
        lines.append(f"<i>💾 Đang xem bản lưu lúc {t:%H:%M %d/%m} – có thể đã cũ</i>")
        if res.web_url:
            lines.append(f'🌐 <a href="{html.escape(res.web_url)}">Mở trang tra cứu {html.escape(res.carrier)}</a>')
    elif res.saved_at:
        t = datetime.fromisoformat(res.saved_at)
        lines.append(f"\n<i>💾 Hành trình đã kết thúc, xem bản lưu lúc {t:%H:%M %d/%m} (không tra lại hãng)</i>")
    else:
        lines.append(f"\n<i>Cập nhật lúc {datetime.now():%H:%M %d/%m}</i>")
    return "\n".join(lines)


async def build_tracking(order_id: int | None = None, code: str | None = None, phone: str | None = None):
    """Trả về (nội dung HTML, bàn phím) cho 1 đơn hoặc 1 mã vận đơn (J&T, GHN cần SĐT / 4 số cuối)."""
    order = db.get("orders", order_id) if order_id else None
    if order_id and not order:
        return "Không tìm thấy đơn.", None
    if order:
        code = order["ma_don"]
        if not code:
            return f"Đơn #{order_id} chưa có mã vận đơn. Thêm: /sua {order_id} ma_don &lt;mã&gt;", None
    if order:
        res = await tracking.track_saved(code, order["don_vi_vc"], order["sdt"])
    else:
        res = await tracking.track_saved(code, None, phone)
    res = tracking.with_saved_fallback(res)
    ref = f"o{order_id}" if order else f"c{code}:{tracking.phone_last4(phone)}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Cập nhật", callback_data=f"trk:{ref}"[:64])]])
    return fmt_tracking(res, order), kb


async def send_tracking(message, order_id: int | None = None, code: str | None = None, phone: str | None = None):
    text, kb = await build_tracking(order_id, code, phone)
    await message.reply_html(text, reply_markup=kb, disable_web_page_preview=True)


async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tracuu <số đơn> | /tracuu <mã vận đơn> [4 số cuối SĐT – cần cho J&T, GHN]"""
    if not context.args:
        await update.message.reply_text("Cú pháp: /tracuu <số đơn> hoặc /tracuu <mã vận đơn> [4 số cuối SĐT]\n"
                                        "Hoặc bấm vào mã vận đơn (có 🔍) trong /ds, /xem.")
        return
    arg = context.args[0].strip()
    if arg.lstrip("#").isdigit() and len(arg) < 8:
        await send_tracking(update.message, order_id=int(arg.lstrip("#")))
    else:
        phone = context.args[1] if len(context.args) > 1 else None
        await send_tracking(update.message, code=arg, phone=phone)


async def on_track_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Đang tra cứu…")
    ref = query.data.split(":", 1)[1]
    if ref.startswith("o"):
        text, kb = await build_tracking(order_id=int(ref[1:]))
    else:
        code, _, phone = ref[1:].partition(":")
        text, kb = await build_tracking(code=code, phone=phone or None)
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    except BadRequest:  # nội dung không đổi
        pass


# ---------- tự động theo dõi vận chuyển + alert ----------

fold, _match, is_delivered = tracking.fold, tracking.match_keywords, tracking.is_delivered


def alert_match(res: "tracking.TrackResult") -> str | None:
    return _match(res, alert_rules.keywords_for(res.carrier))


def cancel_match(res: "tracking.TrackResult") -> str | None:
    """Cụm từ cho biết đơn bị hủy / hoàn về (alert_rules.CANCEL_KEYWORDS), None nếu không."""
    return _match(res, alert_rules.cancel_keywords_for(res.carrier))


# orders.vc_near: mức tra hành trình (tính trong tracking.py, web dùng chung)
VC_NORMAL, VC_NEAR, VC_HUB, VC_HUB_SORTED = tracking.VC_NORMAL, tracking.VC_NEAR, tracking.VC_HUB, tracking.VC_HUB_SORTED
hub_sorted, delivery_level = tracking.hub_sorted, tracking.delivery_level


def alert_title(res: "tracking.TrackResult", order_id: int) -> str:
    """Tiêu đề alert theo trạng thái hiện tại: 🚚 đang giao / ✅ đã giao / ❌ bị hủy, hoàn hàng / ⚠️ giao lỗi."""
    status = res.milestone or res.events[0].description
    text = fold(f"{status} {res.events[0].description}")
    if cancel_match(res):
        return f"🔔 <b>Đơn #{order_id} · ❌ BỊ HỦY / HOÀN HÀNG</b> ({html.escape(status)})"
    icon = ("✅" if is_delivered(res) else
            "⚠️" if any(fold(k) in text for k in alert_rules.FAIL_KEYWORDS) else "🚚")
    return f"🔔 <b>Đơn #{order_id} · {icon} {html.escape(status)}</b>"


ALERT_STATUS_LABEL = {db.ALERT_APPROVED: "✅ Đang nhận", db.ALERT_PENDING: "⏳ Chờ duyệt",
                      db.ALERT_REJECTED: "⛔ Từ chối"}


def admin_ids() -> list[int]:
    """Admin trong .env luôn nhận alert (ALERT_CHAT_IDS, trống thì ALLOWED_USER_IDS)."""
    return list(ALERT_CHAT_IDS or sorted(ALLOWED_USER_IDS))


def alert_recipients() -> dict[int, str]:
    """Admin + người đã được duyệt trong bảng alert_users, {chat_id: tên}."""
    chats = {cid: "admin" for cid in admin_ids()}
    for u in db.list_alert_users(db.ALERT_APPROVED):
        chats.setdefault(u["chat_id"], u["ten"] or "")
    return chats


def alert_user_label(u) -> str:
    name = html.escape(u["ten"] or "")
    if u["username"]:
        name += f" @{html.escape(u['username'])}"
    return f"<code>{u['chat_id']}</code> {name}".strip()


def alert_user_buttons(u) -> list[InlineKeyboardButton]:
    cid, status = u["chat_id"], u["trang_thai"]
    if status == db.ALERT_PENDING:
        return [InlineKeyboardButton("✅ Duyệt", callback_data=f"alert:ok:{cid}"),
                InlineKeyboardButton("⛔ Từ chối", callback_data=f"alert:no:{cid}")]
    if status == db.ALERT_APPROVED:
        return [InlineKeyboardButton("❌ Bỏ quyền", callback_data=f"alert:no:{cid}"),
                InlineKeyboardButton("🗑 Xóa", callback_data=f"alert:del:{cid}")]
    return [InlineKeyboardButton("✅ Cấp quyền", callback_data=f"alert:ok:{cid}"),
            InlineKeyboardButton("🗑 Xóa", callback_data=f"alert:del:{cid}")]


def build_alert_users():
    """Nội dung + bàn phím cho /alert: quản lý người nhận tin bot alert theo trạng thái."""
    users = db.list_alert_users()
    lines = ["🔔 <b>Người nhận tin từ bot alert</b>", "",
             "<b>Admin</b> (trong .env, luôn nhận):"]
    lines += [f"• <code>{cid}</code>" for cid in admin_ids()] or ["<i>(chưa có)</i>"]
    buttons = []
    for status in (db.ALERT_PENDING, db.ALERT_APPROVED, db.ALERT_REJECTED):
        group = [u for u in users if u["trang_thai"] == status]
        if not group:
            continue
        lines.append(f"\n<b>{ALERT_STATUS_LABEL[status]}</b> ({len(group)}):")
        for u in group:
            lines.append(f"• {alert_user_label(u)} <i>– {(u['updated_at'] or u['created_at'])[:16]}</i>")
            name = (u["ten"] or str(u["chat_id"]))[:20]
            buttons.append([InlineKeyboardButton(f"👤 {name}", callback_data="noop"),
                            *alert_user_buttons(u)])
    if not users:
        lines.append("\n<i>Chưa có ai. Người muốn nhận tin chỉ cần mở bot alert và bấm Start – "
                     "bot sẽ báo để bạn duyệt. Hoặc thêm tay: /themalert &lt;chat id&gt; [tên]</i>")
    if not ALERT_BOT_TOKEN:
        lines.append("\n⚠️ Chưa cấu hình ALERT_BOT_TOKEN – alert đang gửi bằng bot chính.")
    return "\n".join(lines), InlineKeyboardMarkup(buttons) if buttons else None


async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/alert: quản lý người được nhận tin từ bot alert."""
    text, kb = build_alert_users()
    await update.message.reply_html(text, reply_markup=kb)


async def send_alert_to(chat_id: int, text: str):
    """Nhắn 1 người qua bot alert (không lỗi nếu không gửi được)."""
    if not ALERT_BOT_TOKEN:
        return
    try:
        async with Bot(ALERT_BOT_TOKEN, **tg_net.tg_bot_kwargs()) as alert_bot:
            await alert_bot.send_message(chat_id, text)
    except Exception as e:
        log.warning("Nhắn %s qua bot alert lỗi: %s", chat_id, e)


async def notify_admins(app, text: str, kb=None):
    for admin in sorted(ALLOWED_USER_IDS):
        try:
            await app.bot.send_message(admin, text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            log.warning("Báo admin %s lỗi: %s", admin, e)


async def set_alert_status(chat_id: int, action: str, by: int | None) -> str:
    """action: ok (duyệt/cấp quyền) · no (từ chối/bỏ quyền) · del (xóa khỏi bảng)."""
    if chat_id in admin_ids():
        return "Id này là admin trong .env – luôn nhận alert, muốn đổi thì sửa .env."
    u = db.get_alert_user(chat_id)
    if action == "del":
        if not u:
            return "Không có người này trong danh sách."
        db.remove_alert_user(chat_id)
        if u["trang_thai"] == db.ALERT_APPROVED:
            await send_alert_to(chat_id, "🔕 Bạn đã bị bỏ quyền nhận thông báo vận chuyển.")
        return f"🗑 Đã xóa {alert_user_label(u)} khỏi danh sách."
    new = db.ALERT_APPROVED if action == "ok" else db.ALERT_REJECTED
    if u and u["trang_thai"] == new:
        return f"{alert_user_label(u)} đã ở trạng thái {ALERT_STATUS_LABEL[new]}."
    db.upsert_alert_user(chat_id, None, status=new, by=by)
    u = db.get_alert_user(chat_id)
    if new == db.ALERT_APPROVED:
        await send_alert_to(chat_id, "✅ Admin đã duyệt – bạn sẽ nhận thông báo vận chuyển từ bot này. "
                                     "Gửi /stop nếu muốn ngừng nhận.")
        return f"✅ Đã cấp quyền nhận alert cho {alert_user_label(u)}."
    await send_alert_to(chat_id, "⛔ Bạn không được cấp quyền nhận thông báo từ bot này.")
    return f"⛔ {alert_user_label(u)}: không nhận alert."


async def on_alert_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, action, cid = query.data.split(":")
    msg = await set_alert_status(int(cid), action, update.effective_user.id)
    if query.message.text and query.message.text.startswith("🔔 Người nhận"):  # bảng /alert → vẽ lại
        text, kb = build_alert_users()
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except BadRequest:
            pass
        await query.message.reply_html(msg)
    else:  # tin báo có người xin nhận alert → ghi kết quả vào luôn
        await query.edit_message_text(query.message.text_html + "\n\n➡️ " + msg, parse_mode="HTML")


async def cmd_add_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/themalert <chat id> [tên]"""
    if not context.args or not re.fullmatch(r"-?\d+", context.args[0]):
        await update.message.reply_html("Cú pháp: /themalert &lt;chat id&gt; [tên]\n"
                                        "Dễ hơn: nhờ người đó mở bot alert và bấm Start, bot sẽ báo để bạn duyệt.")
        return
    cid = int(context.args[0])
    if name := " ".join(context.args[1:]):
        db.upsert_alert_user(cid, name, status=(db.get_alert_user(cid) or {"trang_thai": db.ALERT_PENDING})["trang_thai"])
    await update.message.reply_html(await set_alert_status(cid, "ok", update.effective_user.id))


async def cmd_remove_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/xoaalert <chat id>"""
    if not context.args or not re.fullmatch(r"-?\d+", context.args[0]):
        await update.message.reply_html("Cú pháp: /xoaalert &lt;chat id&gt; (xem id bằng /alert)")
        return
    await update.message.reply_html(await set_alert_status(int(context.args[0]), "del", update.effective_user.id))


async def on_alert_bot_message(app, alert_bot, message):
    """Tin nhắn gửi tới bot alert: Start → xin quyền (báo admin duyệt), /stop → ngừng nhận."""
    chat = message.chat
    text = (message.text or "").strip().lower()
    name = chat.title or " ".join(filter(None, [chat.first_name, chat.last_name])) or str(chat.id)

    async def reply(t):
        await alert_bot.send_message(chat.id, t)

    async def reply_html(t):
        await alert_bot.send_message(chat.id, t, parse_mode="HTML", disable_web_page_preview=True)

    u = db.get_alert_user(chat.id)
    status = u["trang_thai"] if u else None
    if chat.id in admin_ids() or status == db.ALERT_APPROVED:
        if m := re.fullmatch(r"/start\s+tr_(\d+)", text):  # bấm mã vận đơn trong bảng tổng hợp
            body, _ = await build_tracking(order_id=int(m[1]))
            link = alert_track_link(int(m[1]))
            await reply_html(body + (f'\n\n🔄 <a href="{html.escape(link)}">Cập nhật</a>' if link else ""))
            try:
                await alert_bot.delete_message(chat.id, message.message_id)
            except Exception:
                pass
            return
        if text.startswith("/danggiao"):  # tra lại API ở task riêng để bot alert vẫn nhận tin khác trong lúc chờ
            async def progress(t):
                return await alert_bot.send_message(chat.id, t)

            async def run():
                try:
                    await refreshed_delivering(app, alert_track_link, reply_html, progress)
                except Exception:
                    log.exception("Lỗi /danggiao ở bot alert")
            asyncio.create_task(run())
            return
    if chat.id in admin_ids():
        await reply("👋 Bạn là admin – luôn nhận thông báo vận chuyển từ bot này.\n/danggiao – các đơn đang giao đến")
        return
    if text.startswith("/stop"):
        if status == db.ALERT_APPROVED:
            db.remove_alert_user(chat.id)
            await reply("🔕 Đã ngừng nhận thông báo. Bấm /start nếu muốn nhận lại.")
            await notify_admins(app, f"🔕 {alert_user_label(u)} đã tự ngừng nhận alert.")
        else:
            await reply("Bạn đang không nhận thông báo.")
        return
    if status == db.ALERT_APPROVED:
        await reply("✅ Bạn đang nhận thông báo vận chuyển. /danggiao – các đơn đang giao đến · /stop – ngừng nhận.")
    elif status == db.ALERT_PENDING:
        await reply("⏳ Yêu cầu của bạn đang chờ admin duyệt.")
    elif status == db.ALERT_REJECTED:
        await reply("⛔ Bạn không được cấp quyền nhận thông báo từ bot này.")
    else:
        db.upsert_alert_user(chat.id, name, chat.username, status=db.ALERT_PENDING)
        await reply("📨 Đã gửi yêu cầu nhận thông báo vận chuyển tới admin, vui lòng chờ duyệt.")
        u = db.get_alert_user(chat.id)
        await notify_admins(
            app, f"🔔 <b>Yêu cầu nhận tin bot alert</b>\n{alert_user_label(u)}",
            InlineKeyboardMarkup([alert_user_buttons(u)]))


async def alert_bot_loop(app):
    """Đọc tin nhắn gửi tới bot alert (long polling) – bot alert chỉ chạy trong tiến trình này."""
    offset = None
    while True:
        # lỗi mạng (timeout qua proxy) có thể làm kẹt kết nối duy nhất của get_updates → mọi lần sau
        # đều "Pool timeout". Nên lỗi là đóng Bot (bỏ kết nối cũ) rồi tạo lại.
        # lỗi lúc tạo Bot (get_me khi mạng chập chờn) cũng phải bắt, không thì task chết im lặng
        # và bot alert không trả lời lệnh nào nữa cho tới lần khởi động lại
        try:
            async with Bot(ALERT_BOT_TOKEN, **tg_net.tg_bot_kwargs()) as alert_bot:
                global ALERT_BOT_USERNAME
                ALERT_BOT_USERNAME = alert_bot.username or ALERT_BOT_USERNAME
                try:
                    await alert_bot.set_my_commands([BotCommand("danggiao", "Các đơn đang giao đến"),
                                                     BotCommand("stop", "Ngừng nhận thông báo")])
                except Exception as e:
                    log.warning("Đặt menu lệnh bot alert lỗi: %s", e)
                while True:
                    try:
                        updates = await alert_bot.get_updates(offset=offset, timeout=30, read_timeout=40,
                                                              allowed_updates=["message"])
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("Đọc tin bot alert lỗi: %s – kết nối lại", e)
                        break
                    for u in updates:
                        offset = u.update_id + 1
                        if u.message:
                            try:
                                await on_alert_bot_message(app, alert_bot, u.message)
                            except Exception:
                                log.exception("Xử lý tin bot alert lỗi")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Kết nối bot alert lỗi: %s – thử lại sau 15s", e)
        await asyncio.sleep(15)


async def send_alert(app, text: str) -> int:
    """Gửi bằng bot alert (ALERT_BOT_TOKEN); chưa cấu hình thì dùng bot chính. Trả về số tin gửi được."""
    chat_ids = list(alert_recipients())
    if not chat_ids:
        log.warning("Không có ALERT_CHAT_IDS / ALLOWED_USER_IDS để gửi alert")
        return 0
    sent = 0

    async def send_all(bot):
        nonlocal sent
        for chat_id in chat_ids:
            try:
                await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
                sent += 1
            except Exception as e:  # vd: người nhận chưa bấm Start với bot alert
                log.warning("Gửi alert tới %s lỗi: %s", chat_id, e)

    if ALERT_BOT_TOKEN:
        async with Bot(ALERT_BOT_TOKEN, **tg_net.tg_bot_kwargs()) as alert_bot:
            await send_all(alert_bot)
    else:
        await send_all(app.bot)
    return sent


# ---------- sổ địa chỉ đặt hàng ----------

def fmt_address(r) -> str:
    # <code> → bấm vào là chép cả địa chỉ
    return f"<b>#{r['id']}</b>\n<code>{html.escape(r['noi_dung'])}</code>"


async def cmd_addresses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/diachi [từ khóa] – sổ địa chỉ đặt hàng (bấm vào địa chỉ để chép); /diachi <id> – chỉ trả nội dung để chép."""
    if len(context.args or []) == 1 and context.args[0].lstrip("#").isdigit():
        r = db.get("addresses", int(context.args[0].lstrip("#")))
        if r:  # chỉ gửi nguyên văn địa chỉ, không định dạng → giữ tin / chép ngay
            await update.message.reply_text(r["noi_dung"])
            return
    kw = " ".join(context.args) if context.args else None
    rows = db.list_addresses(kw)
    if not rows:
        await update.message.reply_text(("Không có địa chỉ nào khớp." if kw else "Chưa lưu địa chỉ nào.")
                                        + "\nThêm: /themdiachi <địa chỉ> (được xuống dòng)")
        return
    head = f"🏠 <b>Sổ địa chỉ</b> ({len(rows)}{' khớp “' + html.escape(kw) + '”' if kw else ''}) · bấm vào địa chỉ để chép"
    blocks = [head, *(fmt_address(r) for r in rows),
              "Chỉ lấy chữ để chép: /diachi &lt;id&gt; · Thêm: /themdiachi &lt;địa chỉ&gt; · Sửa: /suadiachi &lt;id&gt; · Xóa: /xoadiachi &lt;id&gt;"]
    for msg in join_blocks(blocks):
        await update.message.reply_html(msg)


async def save_address(update: Update, text: str):
    text = (text or "").strip()
    if not text or text == "-":
        await update.message.reply_text("Chưa lưu (địa chỉ trống).")
        return
    rec_id = db.add_address(text, update.effective_user.id)
    await update.message.reply_html(f"✅ Đã lưu địa chỉ\n{fmt_address(db.get('addresses', rec_id))}\n\nXem tất cả: /diachi")


async def cmd_add_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/themdiachi <địa chỉ> – giữ nguyên xuống dòng; không kèm nội dung thì bot hỏi."""
    parts = (update.message.text or "").split(None, 1)
    if len(parts) > 1 and parts[1].strip():
        await save_address(update, parts[1])
        return
    context.user_data["pending_address"] = True
    await update.message.reply_text("🏠 Gửi địa chỉ cần lưu (được xuống dòng). /huy để thôi.")


# ---------- tổng hợp đơn đang giao đến (bot alert /danggiao + tự gửi khi có đơn mới vào danh sách) ----------

ALERT_BOT_USERNAME = ""  # gán khi bot alert kết nối (alert_bot_loop)
DELIVERING_META = "delivering_ids"  # meta: id các đơn đã có trong bảng tổng hợp gửi gần nhất


def alert_track_link(order_id: int) -> str:
    """Link mã vận đơn trong tin bot alert → bot alert trả hành trình đơn đó (không có bot alert: bot chính)."""
    user = ALERT_BOT_USERNAME if ALERT_BOT_TOKEN else BOT_USERNAME
    return f"https://t.me/{user}?start=tr_{order_id}" if user else ""


def main_track_link(order_id: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=tr_{order_id}" if BOT_USERNAME else ""


def delivering_orders() -> list[tuple]:
    """Đơn ⏳ Đã đặt đang sắp giao / đang giao (mức VC_NEAR tính từ hành trình đã lưu) kèm hành trình (không gọi API hãng)."""
    out = []
    for r in trackable_orders():
        lg = db.get_track_log(tracking.log_key(r["ma_don"]))
        res = tracking.result_from_dict(json.loads(lg["data"])) if lg else None
        # theo hành trình đã lưu (có thể vừa tra trên web, bot chưa tra lại); chưa có bản lưu thì theo cờ vc_near
        level = delivery_level(res) if res and res.events else r["vc_near"]
        if level != VC_NEAR or (res and (lg["final"] or is_delivered(res))):
            continue
        out.append((r, res))
    # mốc hành trình mới nhất trước (đơn vừa chuyển sang đang giao lên đầu)
    return sorted(out, key=lambda x: x[1].events[0].time if x[1] and x[1].events else datetime.min, reverse=True)


out_for_delivery = tracking.out_for_delivery


def is_fail_event(ev) -> bool:
    text = fold(ev.description)
    return any(fold(k) in text for k in alert_rules.FAIL_KEYWORDS)


def delivering_state(res) -> str:
    """'giao' = đang giao (có nhân viên đang đi giao), 'sap' = đã tới bưu cục giao cuối, chưa đi giao."""
    return "giao" if out_for_delivery(res) else "sap"


def fmt_delivering_order(i: int, r, res, link_fn, new: bool, state: str = "") -> str:
    esc = html.escape
    link = link_fn(r["id"])
    code = f'<a href="{esc(link)}"><b>{esc(r["ma_don"])}</b></a>' if link else f"<code>{esc(r['ma_don'])}</code>"
    carrier = r["don_vi_vc"] or (res.carrier if res else "")
    qty = r["so_luong"] or 1
    # người đặt nằm ở dòng tiêu đề nhóm (fmt_delivering) → ở đây chỉ còn trạng thái giao + tên trên đơn
    tag = {"giao": " · 🏍 <b>Đang giao</b>", "sap": " · 📮 Sắp giao"}.get(state, "")
    lines = [f"{'🆕 ' if new else ''}<b>{i}. Đơn #{r['id']}</b>{tag}"
             + (f" · 🪪 {esc(r['ten_tren_don'])}" if r["ten_tren_don"] else ""),
             f"🚚 {esc(carrier or '—')} · {code}",
             f"📦 {esc(r['ten_sp'] or '—')}{f' × {qty}' if qty > 1 else ''} · 💰 {money(r['gia_dat'] or 0)}"]
    if r["sdt"]:  # SĐT trên đơn (shipper gọi số này) + số của tôi thì lắp ở máy nào
        lines.append(f"📞 <code>{esc(r['sdt'])}</code>{phone_tag(r['sdt'])}")
    if r["dia_chi"]:
        lines.append(f"🏠 {esc(r['dia_chi'])}")
    courier = tracking.courier_of(res) if res else None
    latest_is_courier = bool(courier and res.events and courier["event"] == res.events[0])  # == (không phải is): mốc qua API là bản sao
    if res and res.events and not latest_is_courier:
        ev = res.events[0]
        lines.append(f"{'⚠️' if is_fail_event(ev) else '📍'} {ev.time:%H:%M %d/%m} · {esc(ev.description)}")
    if courier:
        lines.append(f"🧑‍✈️ {'' if latest_is_courier else 'Lần giao trước · '}{courier['time']:%H:%M %d/%m} · "
                     f"<b>{esc(courier['name'])}</b>" + (f" · {courier['phone']}" if courier["phone"] else "")
                     + f" · {esc(courier['post'])}")
    return "\n".join(lines)


def fmt_delivering(items: list[tuple], link_fn, new_ids=frozenset()) -> list[str]:
    """Các khối HTML: tiêu đề, rồi gom theo người đặt (người dùng yêu cầu): mỗi người 1 dòng tiêu đề số đơn + tổng giá
    đặt, người có đơn 🏍 Đang giao (đã có nhân viên đi giao) lên trước, rồi tới 📮 Sắp giao (đã tới bưu cục cuối);
    mỗi đơn: trạng thái giao, tên trên đơn, mã vận đơn bấm được, ĐVVC, hàng, giá đặt, địa chỉ, mốc mới nhất,
    nhân viên giao nếu hãng có ghi (J&T)."""
    head = f"🛵 <b>ĐƠN ĐANG GIAO ĐẾN ({len(items)})</b> · {datetime.now():%H:%M %d/%m}"
    if not items:
        return [head + "\n\nKhông có đơn nào đang giao."]
    states = {r["id"]: delivering_state(res) for r, res in items}
    n_giao = sum(st == "giao" for st in states.values())
    total = sum(r["gia_dat"] or 0 for r, _ in items)
    # gom theo người đặt, giữ thứ tự mốc mới nhất trước; trong mỗi người: đang giao trước sắp giao
    people: dict[str, list] = {}
    for r, res in items:
        people.setdefault(" ".join((r["ten_nguoi_dat"] or "").lower().split()), []).append((r, res))
    groups = sorted(people.values(), key=lambda g: not any(states[r["id"]] == "giao" for r, _ in g))
    blocks = [head + f"\n🏍 Đang giao: <b>{n_giao}</b> · 📮 Sắp giao: <b>{len(items) - n_giao}</b>"
              + f" · 👤 {len(groups)} người · 💰 tổng <b>{money(total)}</b>"
              + (f"\n🆕 = mới vào danh sách / vừa đi giao ({len(new_ids)} đơn)" if new_ids else "")
              + "\n<i>🏍 Đang giao = nhân viên đã nhận hàng đi giao · 📮 Sắp giao = đã tới bưu cục giao cuối</i>"
              + "\n<i>Bấm mã vận đơn để xem hành trình từng đơn</i>"]
    i = 0
    for g in groups:
        g.sort(key=lambda x: states[x[0]["id"]] != "giao")
        n = {st: sum(states[r["id"]] == st for r, _ in g) for st in ("giao", "sap")}
        detail = " · ".join(f"{icon} {n[st]}" for st, icon in (("giao", "🏍"), ("sap", "📮")) if n[st])
        blocks.append(f"━━━━━━━━━━\n👤 <b>{html.escape(g[0][0]['ten_nguoi_dat'] or '—')}</b> · {len(g)} đơn ({detail})"
                      f" · 💰 <b>{money(sum(r['gia_dat'] or 0 for r, _ in g))}</b>")
        for r, res in g:
            i += 1
            blocks.append(fmt_delivering_order(i, r, res, link_fn, r["id"] in new_ids, states[r["id"]]))
    return blocks


def join_blocks(blocks: list[str], limit: int = 4000) -> list[str]:
    """Ghép các khối thành tin ≤ limit ký tự (giới hạn 4096 của Telegram), không cắt giữa 1 khối."""
    msgs, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > limit:
            msgs.append(cur)
            cur = ""
        cur = f"{cur}\n\n{b}" if cur else b
    return msgs + [cur] if cur else msgs


async def notify_delivering(app):
    """Có đơn mới vào danh sách, hoặc đơn chuyển từ sắp giao sang đang giao (so với lần trước, lưu ở meta
    {id: 'giao'|'sap'}) → bot alert gửi lại cả bảng tổng hợp."""
    items = delivering_orders()
    cur = {str(r["id"]): delivering_state(res) for r, res in items}
    try:
        prev = json.loads(db.get_meta(DELIVERING_META) or "{}")
        prev = prev if isinstance(prev, dict) else {str(i): "sap" for i in prev}  # bản cũ: list id
    except ValueError:
        prev = {}
    # đơn mới vào danh sách, hoặc từ 📮 sắp giao chuyển sang 🏍 đang giao → gửi lại bảng
    if new := {int(i) for i, st in cur.items() if i not in prev or (st == "giao" and prev[i] != "giao")}:
        sent = 0
        for msg in join_blocks(fmt_delivering(items, alert_track_link, new)):
            sent += await send_alert(app, msg)
        if not sent:
            return  # gửi lỗi → lần sau thử lại
    if cur != prev:
        db.set_meta(DELIVERING_META, json.dumps(cur))


async def refreshed_delivering(app, link_fn, send, progress=None) -> None:
    """/danggiao (bot chính + bot alert): tra lại API hãng mọi đơn Đã đặt (người dùng yêu cầu: không chỉ đọc hành
    trình đã lưu), rồi gửi bảng tổng hợp. send(html) gửi 1 tin; progress(text) báo đang tra (tin sẽ bị xóa sau)."""
    n = len(trackable_orders())
    note = await progress(f"🔄 Đang tra lại hành trình {n} đơn Đã đặt từ hãng vận chuyển…") if progress and n else None
    try:
        s = await check_shipments(app)
    except Exception:
        log.exception("Lỗi khi tra lại đơn Đã đặt cho /danggiao")
        s = None
    if note:
        try:
            await note.delete()
        except Exception:
            pass
    blocks = fmt_delivering(delivering_orders(), link_fn)
    if s is None or s["errors"]:
        blocks[0] += f"\n⚠️ Tra lại lỗi {s['errors'] if s else 'cả lượt'} đơn – đơn đó dùng hành trình đã lưu"
    for msg in join_blocks(blocks):
        await send(msg)


async def cmd_delivering(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/danggiao: tra lại các đơn Đã đặt rồi gửi bảng tổng hợp đơn đang giao đến (link mã vận đơn mở ở bot chính)."""
    msg = update.message
    await refreshed_delivering(context.application, main_track_link,
                               lambda t: msg.reply_html(t, disable_web_page_preview=True), msg.reply_text)


def trackable_orders() -> list:
    """Đơn Đã đặt có mã vận đơn tra được."""
    return [r for r in db.orders_by_status([db.STATUS_ORDERED])
            if r["ma_don"] and tracking.is_supported(r["ma_don"], r["don_vi_vc"])]


def check_interval(level: int) -> tuple[float, float]:
    """(chu kỳ, độ lệch ngẫu nhiên tối đa) tính bằng giây cho đơn thường / sắp giao / đã về kho cuối."""
    if level == VC_NEAR:
        iv, jitter = alert_rules.NEAR_CHECK_HOURS * 3600, alert_rules.NEAR_CHECK_JITTER_MINUTES * 60
    elif level == VC_HUB:
        iv, jitter = alert_rules.HUB_CHECK_HOURS * 3600, alert_rules.HUB_CHECK_JITTER_MINUTES * 60
    elif level == VC_HUB_SORTED:
        iv, jitter = alert_rules.HUB_SORTED_CHECK_HOURS * 3600, alert_rules.HUB_SORTED_CHECK_JITTER_MINUTES * 60
    else:
        iv, jitter = TRACK_INTERVAL_HOURS * 3600, alert_rules.CHECK_JITTER_MINUTES * 60
    return iv, min(jitter, iv / 2)


def schedule_next_check(order_id: int, level: int):
    iv, jitter = check_interval(level)
    db.set_vc_next(order_id, datetime.now().timestamp() + iv + random.uniform(-jitter, jitter))


async def check_order(app, r, stats: dict):
    """Tra 1 đơn, cập nhật cờ sắp giao + hẹn giờ tra lần tới; khớp alert_rules và có mốc mới thì gửi alert."""
    res = await tracking.track_saved(r["ma_don"], r["don_vi_vc"], r["sdt"])
    stats["checked"] += 1
    if not res.ok:
        # tra hãng lỗi (vd GHN bắt captcha) → dùng hành trình đã lưu (tiện ích Chrome / web gửi về) để vẫn tính mức
        # + gửi alert; chữ ký mốc (vc_alert_last) chặn báo trùng
        stats["errors"] += 1
        res = tracking.with_saved_fallback(res)
        if not res.ok:
            schedule_next_check(r["id"], r["vc_near"])
            return
    level = r["vc_near"]
    if res.ok and res.events:
        level = delivery_level(res)
        if level != r["vc_near"]:
            db.set_vc_near(r["id"], level)
    schedule_next_check(r["id"], level)
    if not res.events:
        return
    stats["near"] += level == VC_NEAR
    stats["hub"] += level in (VC_HUB, VC_HUB_SORTED)
    canceled = cancel_match(res)
    if not canceled and not alert_match(res):
        return
    stats["matched"] += 1
    stats["canceled"] += bool(canceled)
    latest = res.events[0]
    signature = f"{int(latest.time.timestamp())}|{latest.code}|{latest.description}"
    if r["vc_alert_last"] == signature:
        return  # đã báo mốc này rồi
    text = (alert_title(res, r["id"]) + "\n\n" + fmt_tracking(res, r)
            + (f"\n\nXem lại / cập nhật: https://t.me/{BOT_USERNAME}?start=tr_{r['id']}" if BOT_USERNAME else ""))
    if await send_alert(app, text):
        db.set_vc_alert(r["id"], signature)
        stats["alerted"] += 1
    if canceled:
        await notify_admins(app, cancel_prompt_text(r, res), cancel_keyboard(r["id"]))


def cancel_prompt_text(r, res: "tracking.TrackResult") -> str:
    """Tin bot chính gửi khi hành trình báo đơn bị hủy / hoàn: thông tin đơn + mốc mới nhất."""
    ev = res.events[0]
    return (f"❌ <b>Đơn #{r['id']} bị hủy / hoàn hàng</b> theo {html.escape(res.carrier)}:\n"
            f"<i>{ev.time:%d/%m %H:%M} · {html.escape(ev.description)}</i>\n\n"
            + fmt_record("orders", r, r["id"], r["created_at"])
            + "\n\nChuyển đơn sang <b>Đã hủy</b>?")


def cancel_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Chuyển sang Đã hủy", callback_data=f"vccancel:ok:{order_id}"),
        InlineKeyboardButton("Để nguyên", callback_data=f"vccancel:keep:{order_id}")]])


async def on_cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút dưới tin báo đơn bị hủy: vccancel:ok|keep:<id>."""
    query = update.callback_query
    await query.answer()
    _, action, order_id = query.data.split(":")
    r = db.get("orders", int(order_id))
    if not r:
        await query.edit_message_text("Không tìm thấy đơn.")
        return
    if action == "ok" and r["trang_thai"] != db.STATUS_CANCELED:
        db.update("orders", r["id"], "trang_thai", db.STATUS_CANCELED)
        note = f"✅ Đã chuyển đơn #{r['id']} sang <b>Đã hủy</b>."
    elif action == "ok":
        note = f"Đơn #{r['id']} đã ở trạng thái Đã hủy."
    else:
        note = f"Giữ nguyên trạng thái <b>{html.escape(r['trang_thai'])}</b> cho đơn #{r['id']}."
    await query.edit_message_text(fmt_record("orders", db.get("orders", r["id"]), r["id"], r["created_at"])
                                  + "\n\n" + note, parse_mode="HTML", disable_web_page_preview=True)


_shipments_lock = asyncio.Lock()  # watcher, /kiemtravc, /danggiao không tra chồng lên nhau


async def check_shipments(app, orders=None) -> dict:
    """Tra các đơn (mặc định: mọi đơn Đã đặt tra được), cách nhau 2s cho đỡ bị chặn (GHN 429, SPX)."""
    async with _shipments_lock:
        return await _check_shipments(app, orders)


async def _check_shipments(app, orders=None) -> dict:
    stats = {"checked": 0, "near": 0, "hub": 0, "matched": 0, "canceled": 0, "alerted": 0, "errors": 0}
    for i, r in enumerate(trackable_orders() if orders is None else orders):
        if i:
            await asyncio.sleep(2)
        try:
            await check_order(app, r, stats)
        except Exception:
            log.exception("Lỗi khi tra vận đơn đơn #%s", r["id"])
            stats["errors"] += 1
            schedule_next_check(r["id"], r["vc_near"])  # không để đơn lỗi bị tra lại liên tục
    log.info("Kiểm tra vận chuyển: %s", stats)
    try:
        await notify_delivering(app)
    except Exception:
        log.exception("Lỗi khi gửi tổng hợp đơn đang giao")
    return stats


async def refresh_unfinished_logs():
    """Đơn đã rời Đã đặt nhưng hành trình lưu chưa kết thúc: tra lại (chỉ cập nhật track_logs, không alert)."""
    for r in db.unfinished_logs(alert_rules.UNFINISHED_LOG_HOURS):
        try:
            res = await tracking.track_saved(r["ma_don"], r["don_vi_vc"], r["sdt"])
            log.info("Cập nhật hành trình đơn #%s (%s): %s", r["id"], r["trang_thai"], res.milestone or res.error)
        except Exception:
            log.exception("Lỗi khi cập nhật hành trình đơn #%s", r["id"])
        await asyncio.sleep(2)


WATCH_MAX_SLEEP = 600  # thức dậy ít nhất 10 phút/lần để thấy đơn mới / đơn đổi trạng thái


def relevel_from_logs() -> int:
    """Tính lại mức (vc_near) của đơn Đã đặt từ hành trình đã lưu, không gọi API hãng: chạy lúc khởi động để sửa
    alert_rules (từ khóa sắp giao / kho cuối) là nhãn trên web đúng ngay. Lên mức tra dày hơn thì dời giờ tra kế tiếp
    về trong 1 chu kỳ mới. Trả về số đơn đổi mức."""
    changed = 0
    now = datetime.now().timestamp()
    for r in trackable_orders():
        lg = db.get_track_log(tracking.log_key(r["ma_don"]))
        if not lg:
            continue
        level = delivery_level(tracking.result_from_dict(json.loads(lg["data"])))
        if level == r["vc_near"]:
            continue
        db.set_vc_near(r["id"], level)
        iv, _ = check_interval(level)
        if r["vc_next_check"] and r["vc_next_check"] > now + iv:
            db.set_vc_next(r["id"], now + random.uniform(0, iv))
        changed += 1
    return changed


async def shipment_watch_loop(app):
    """Mỗi đơn Đã đặt có giờ tra riêng (orders.vc_next_check): đơn thường mỗi TRACK_INTERVAL_HOURS
    ± alert_rules.CHECK_JITTER_MINUTES, đơn sắp giao mỗi NEAR_CHECK_HOURS ± NEAR_CHECK_JITTER_MINUTES,
    đơn đã về kho cuối mỗi HUB_CHECK_HOURS ± HUB_CHECK_JITTER_MINUTES.

    Đơn chưa có giờ hẹn (đơn mới có mã) được tra lần đầu ngẫu nhiên trong alert_rules.FIRST_CHECK_MINUTES phút. Giờ hẹn lưu trong db nên khởi động lại không chạy dồn.
    """
    logged_next = None
    try:
        if n := relevel_from_logs():
            log.info("Tính lại mức sắp giao / kho cuối theo hành trình đã lưu: %d đơn đổi mức", n)
    except Exception:
        log.exception("Lỗi khi tính lại mức theo hành trình đã lưu")
    while True:
        now = datetime.now().timestamp()
        due = []
        upcoming = []
        for r in trackable_orders():
            nxt = r["vc_next_check"]
            if nxt is None:  # đơn mới có mã: tra lần đầu sớm (trong FIRST_CHECK_MINUTES), không đợi cả chu kỳ
                iv, _ = check_interval(r["vc_near"])
                nxt = now + random.uniform(0, min(iv, alert_rules.FIRST_CHECK_MINUTES * 60))
                db.set_vc_next(r["id"], nxt)
            (due if nxt <= now else upcoming).append((nxt, r))
        if due:
            try:
                await check_shipments(app, [r for _, r in sorted(due, key=lambda x: x[0])])
            except Exception:
                log.exception("Lỗi khi kiểm tra vận chuyển")
            continue
        await refresh_unfinished_logs()
        nxt = min((t for t, _ in upcoming), default=None)
        if nxt != logged_next:
            logged_next = nxt
            if nxt is None:
                log.info("Kiểm tra vận chuyển: không có đơn nào cần tra")
            else:
                ids = [r["id"] for t, r in upcoming if t == nxt]
                log.info("Kiểm tra vận chuyển tiếp theo lúc %s (đơn #%s)",
                         datetime.fromtimestamp(nxt).strftime("%d/%m %H:%M"), ids[0])
        await asyncio.sleep(max(1.0, min(WATCH_MAX_SLEEP, (nxt or now + WATCH_MAX_SLEEP) - now)))


async def cmd_check_shipments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/kiemtravc: chạy kiểm tra vận chuyển ngay."""
    await update.message.reply_text("⏳ Đang kiểm tra vận chuyển các đơn Đã đặt…")
    s = await check_shipments(context.application)
    await update.message.reply_text(
        f"✅ Đã tra {s['checked']} đơn · {s['near']} đơn sắp giao · {s['hub']} đơn đã về kho cuối · {s['matched']} đơn khớp điều kiện báo · "
        f"gửi {s['alerted']} alert mới" + (f" · ❌ {s['canceled']} đơn bị hủy/hoàn" if s["canceled"] else "")
        + (f" · {s['errors']} lỗi" if s["errors"] else ""))


# ---------- dọn chat ----------

CLEAR_DEFAULT = 1000


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/clear [n]: xóa n tin gần nhất trong chat (cả tin của bot lẫn người dùng).

    Bot API không có lệnh đọc lịch sử, nhưng message_id trong 1 chat riêng tăng dần → xóa lùi từ tin /clear.
    Telegram chỉ cho xóa tin gửi trong 48 giờ; id không tồn tại / quá hạn được bỏ qua.
    """
    n = min(int(context.args[0]), 10000) if context.args and context.args[0].isdigit() else CLEAR_DEFAULT
    chat_id, last = update.effective_chat.id, update.message.message_id
    ids = list(range(last, max(0, last - n), -1))
    for i in range(0, len(ids), 100):  # deleteMessages nhận tối đa 100 id/lần
        try:
            await context.bot.delete_messages(chat_id, ids[i:i + 100])
        except TelegramError as e:
            log.info("/clear: bỏ qua %s–%s (%s)", ids[i], ids[min(i + 99, len(ids) - 1)], e)


# ---------- ảnh QR trả công ----------

async def _cache_qr(photo):
    try:
        db.save_image(photo.file_id, bytes(await (await photo.get_file()).download_as_bytearray()))
        await asyncio.sleep(1)  # chờ handler lưu file_id cho người đặt xong rồi mới chép ảnh theo tên
        db.sync_qr_images()
    except Exception as e:  # web tự tải lại sau
        log.warning("Không lưu được ảnh vào database: %s", e)


def cache_qr(photo):
    """Lưu ảnh vừa nhận (QR, ảnh sản phẩm) vào database của web (chạy nền): web hiện ảnh không cần Telegram,
    file_id hỏng (đổi bot…) vẫn gửi lại được (reply_saved_photo)."""
    asyncio.create_task(_cache_qr(photo))


def _bad_file_id(e: Exception) -> bool:
    """Telegram không nhận file_id (bot khác / file_id hỏng): "Wrong file identifier/http url specified",
    "Wrong remote file identifier specified: …"."""
    return isinstance(e, BadRequest) and "file identifier" in str(e).lower()


async def reply_saved_photo(message, file_id: str, **kwargs):
    """Gửi ảnh đã lưu (QR, ảnh sản phẩm) bằng file_id; file_id hỏng → gửi ảnh lưu trong database rồi đổi mọi chỗ
    dùng sang file_id mới (db.replace_file_id)."""
    try:
        return await message.reply_photo(file_id, **kwargs)
    except BadRequest as e:
        if not _bad_file_id(e) or (data := db.get_image(file_id)) is None:
            raise
    log.warning("file_id ảnh hỏng, gửi lại bằng ảnh trong database: %s…", file_id[:16])
    sent = await message.reply_photo(data, **kwargs)
    db.replace_file_id(file_id, sent.photo[-1].file_id)
    return sent


async def reply_saved_album(message, file_ids: list[str], captions: list, parse_mode: str | None = None):
    """Như reply_saved_photo cho album 2–10 ảnh (captions: chú thích từng ảnh, None = không có)."""
    def media(srcs):
        return [InputMediaPhoto(src, caption=c, parse_mode=parse_mode) for src, c in zip(srcs, captions)]
    try:
        return await message.reply_media_group(media(file_ids))
    except BadRequest as e:
        if not _bad_file_id(e):
            raise
    datas = [db.get_image(f) for f in file_ids]
    log.warning("file_id ảnh hỏng, gửi lại album bằng ảnh trong database (%d ảnh)", len(file_ids))
    sent = await message.reply_media_group(media([f if d is None else d for f, d in zip(file_ids, datas)]))
    for f, d, m in zip(file_ids, datas, sent):
        if d is not None and m.photo:
            db.replace_file_id(f, m.photo[-1].file_id)
    return sent


async def on_step_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nhận ảnh ở bước QR của /them."""
    field, kind, prompt, _, _ = current_steps(context)[context.user_data["step"]]
    if kind != "photo":
        await update.message.reply_text("⚠️ Bước này chưa cần ảnh.")
        return ASKING
    context.user_data["data"][field] = update.message.photo[-1].file_id
    cache_qr(update.message.photo[-1])
    await update.message.reply_text("✔️ Đã nhận ảnh QR")
    return await next_step(update, context)


def qr_caption(r) -> str:
    e = lambda v: html.escape(v) if v else "—"
    return (f"💳 <b>Trả công đơn #{r['id']}</b> · {status_text(r['trang_thai'])}\n"
            f"👤 <b>{e(r['ten_nguoi_dat'])}</b>   📞 {e(r['sdt'])}\n"
            f"📦 {e(r['ten_sp'])}\n"
            f"🛠 Công cần trả: <b><code>{r['cong_dat'] or 0}</code></b> ({money(r['cong_dat'])})")


def qr_keyboard(r) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton("🔄 Đổi ảnh QR" if r["qr_file_id"] else "📷 Thêm ảnh QR",
                                    callback_data=f"qrnew:{r['id']}")]
    if r["trang_thai"] in db.UNPAID_STATUSES:
        buttons.insert(0, InlineKeyboardButton("✅ Đã trả công", callback_data=f"qrpaid:{r['id']}"))
    return InlineKeyboardMarkup([buttons])


async def ask_qr_photo(message, context, order_id: int):
    context.user_data["pending_qr"] = order_id
    context.user_data.pop("pending_merge", None)
    await message.reply_html(f"📷 Gửi ảnh QR cho đơn <code>#{order_id}</code> (gõ /huy để thôi).")


async def cmd_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/qr <id>: xem ảnh QR trả công; đơn chưa có ảnh thì chờ bạn gửi ảnh."""
    order_id = parse_id(context)
    if order_id is None:
        await update.message.reply_text("Cú pháp: /qr <số đơn>\nHoặc gửi ảnh kèm chú thích: qr <số đơn>")
        return
    r = db.get("orders", order_id)
    if not r:
        await update.message.reply_text("Không tìm thấy đơn.")
        return
    if not r["qr_file_id"]:
        await ask_qr_photo(update.message, context, order_id)
        return
    await reply_saved_photo(update.message, r["qr_file_id"], caption=qr_caption(r), parse_mode="HTML",
                            reply_markup=qr_keyboard(r))


async def on_qr_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ảnh gửi ngoài /them: sau lệnh /qr <id>, hoặc ảnh có chú thích "qr <id>"."""
    if await on_merge_photo(update, context) or await on_product_photo(update, context):
        return
    m = re.match(r"^/?qr\s*#?(\d+)", (update.message.caption or "").strip(), re.IGNORECASE)
    if not m and (pid := context.user_data.pop("pending_qr_person", None)):  # từ /nguoidat → Đổi ảnh QR
        db.set_person_qr(pid, update.message.photo[-1].file_id)
        cache_qr(update.message.photo[-1])
        p = db.find_person(str(pid))
        await update.message.reply_html("✅ Đã lưu ảnh QR (dùng chung cho mọi đơn).\n\n" + person_caption(p),
                                        reply_markup=person_keyboard(p))
        return
    order_id = int(m[1]) if m else context.user_data.get("pending_qr")
    if not order_id:
        await update.message.reply_text(
            "Ảnh này lưu cho đơn nào? Gõ /qr <số đơn> rồi gửi lại ảnh, hoặc gửi ảnh kèm chú thích: qr <số đơn>")
        return
    context.user_data.pop("pending_qr", None)
    if not db.set_qr(order_id, update.message.photo[-1].file_id):
        await update.message.reply_text(f"Không tìm thấy đơn #{order_id}.")
        return
    cache_qr(update.message.photo[-1])
    r = db.get("orders", order_id)
    await update.message.reply_html(f"✅ Đã lưu ảnh QR – dùng chung cho mọi đơn của "
                                    f"<b>{html.escape(r['ten_nguoi_dat'] or '')}</b>.\n\n" + qr_caption(r),
                                    reply_markup=qr_keyboard(r))


async def mark_received(message, order_id: int):
    """Link 📦 ĐÃ NHẬN HÀNG: đơn Đã đặt → Đã nhận (nhận hàng rồi, chưa trả công), gửi lại thẻ đơn kèm link trả công."""
    r = db.get("orders", order_id)
    if not r:
        await message.reply_text("Không tìm thấy đơn.")
        return
    if r["trang_thai"] == db.STATUS_ORDERED:
        db.update("orders", order_id, "trang_thai", db.STATUS_RECEIVED)
        head = f"📦 Đơn #{order_id} → <b>Đã nhận</b> (chưa trả công)."
    else:
        head = f"Đơn #{order_id} đang ở {status_text(r['trang_thai'])}, không đổi."
    await message.reply_html(head + "\n\n" + fmt_order_short(db.get("orders", order_id)),
                             disable_web_page_preview=True)


async def send_pay(message, order_id: int):
    """Ảnh QR trả công (hoặc thông tin nếu chưa có ảnh) kèm nút ✅ Đã trả công → Thanh toán."""
    r = db.get("orders", order_id)
    if not r:
        await message.reply_text("Không tìm thấy đơn.")
    elif r["qr_file_id"]:
        await reply_saved_photo(message, r["qr_file_id"], caption=qr_caption(r), parse_mode="HTML",
                                reply_markup=qr_keyboard(r))
    else:
        await message.reply_html(qr_caption(r) + "\n\n<i>Chưa có ảnh QR.</i>", reply_markup=qr_keyboard(r))


async def on_qr_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, order_id = query.data.split(":")
    order_id = int(order_id)
    r = db.get("orders", order_id)
    if not r:
        await query.message.reply_text("Không tìm thấy đơn.")
        return
    if action == "qrshow":
        await send_pay(query.message, order_id)
        return
    if action == "qrnew":
        context.user_data.pop("pending_edit", None)
        await ask_qr_photo(query.message, context, order_id)
        return
    if action == "qrdel":
        db.set_qr(order_id, None)
        await query.edit_message_text(f"🗑 Đã xóa ảnh QR của đơn #{order_id}.")
        return
    if r["trang_thai"] in db.UNPAID_STATUSES:  # qrpaid
        db.update("orders", order_id, "trang_thai", db.STATUS_PAID)
    r = db.get("orders", order_id)
    text = qr_caption(r) + ("\n\n✅ Đã chuyển sang 💵 Thanh toán." if r["trang_thai"] == db.STATUS_PAID else "")
    if query.message.photo:
        await query.edit_message_caption(text, parse_mode="HTML", reply_markup=qr_keyboard(r))
    else:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=qr_keyboard(r))


async def cmd_delete_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order_id = parse_id(context)
    if order_id is None:
        await update.message.reply_text("Cú pháp: /xoaqr <số đơn>")
        return
    ok = db.set_qr(order_id, None)
    await update.message.reply_text(f"🗑 Đã xóa ảnh QR của đơn #{order_id}." if ok else "Không tìm thấy đơn.")


# ---------- người đặt hộ (QR dùng chung) ----------

def person_caption(p) -> str:
    stats = next((x for x in db.list_people() if x["id"] == p["id"]), None)
    lines = [f"👤 <b>{html.escape(p['ten'])}</b>  <i>(người đặt #{p['id']})</i>"]
    if p["sdt"]:
        lines.append(f"📞 <b><code>{html.escape(p['sdt'])}</code></b>")
    if p["ghi_chu"]:
        lines.append(f"📝 {html.escape(p['ghi_chu'])}")
    if stats:
        lines.append(f"📦 {stats['so_don']} đơn · ⏳ {stats['so_dat']} đơn Đã đặt"
                     f" · công chưa trả <b>{money(stats['cong_chua_tra'])}</b>")
    lines.append("📷 QR dùng chung cho mọi đơn của người này" if p["qr_file_id"] else "📷 <i>Chưa có ảnh QR</i>")
    return "\n".join(lines)


def person_keyboard(p) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("🔄 Đổi ảnh QR" if p["qr_file_id"] else "📷 Thêm ảnh QR",
                                callback_data=f"nd:new:{p['id']}")]
    if p["qr_file_id"]:
        row.append(InlineKeyboardButton("🗑 Xóa ảnh QR", callback_data=f"nd:del:{p['id']}"))
    return InlineKeyboardMarkup([row])


async def send_person(message, p):
    if p["qr_file_id"]:
        await reply_saved_photo(message, p["qr_file_id"], caption=person_caption(p), parse_mode="HTML",
                                reply_markup=person_keyboard(p))
    else:
        await message.reply_html(person_caption(p), reply_markup=person_keyboard(p))
    await reply_long(message, person_orders_lines(p))


def open_order_line(r, show_name: bool = False, show_addr: bool = False) -> str:
    """1 đơn đang đặt (⏳ Đã đặt / 📦 Đã nhận): hàng, giá đặt + công, ĐVVC + mã vận đơn, link 📦 Đã nhận / 💳 Trả công."""
    c = r["created_at"]
    links = []
    if BOT_USERNAME:
        if r["trang_thai"] == db.STATUS_ORDERED:
            links.append(f'<a href="https://t.me/{BOT_USERNAME}?start=nhan_{r["id"]}">📦 Đã nhận</a>')
        links.append(f'<a href="https://t.me/{BOT_USERNAME}?start=pay_{r["id"]}">💳 Trả công</a>')
    return (f"{STATUS_ICON.get(r['trang_thai'], '')} <code>#{r['id']}</code> · {c[8:10]}/{c[5:7]} · "
            + (f"👤 {html.escape(r['ten_nguoi_dat'] or '—')} · " if show_name else "")
            + f"<b>{html.escape(r['ten_sp'] or '—')}</b>{qty_text(r)}"
            + (f" · 🪪 {html.escape(r['ten_tren_don'])}" if r["ten_tren_don"] else "")
            + (f"\n   🏠 {html.escape(r['dia_chi'])}" if show_addr and r["dia_chi"] else "")
            + f"\n   💸 {money(r['gia_dat'])} + công {money(r['cong_dat'])}"
            + (f" · 🚚 {html.escape(r['don_vi_vc'] or '')} {tracking_html(r) if r['ma_don'] else '<i>chưa có mã vận đơn</i>'}"
               if r["don_vi_vc"] or r["ma_don"] else " · 📭 <i>chưa có mã vận đơn</i>")
            + (f"\n   {' · '.join(links)}" if links else ""))


async def cmd_orders_by_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dsdiachi [từ khóa]: đơn ⏳ Đã đặt gom theo nhóm địa chỉ giống nhau (utils.addr_groups, như web 🏠 Theo địa chỉ:
    "Ngõ 34 láng hạ" = "12B 34 láng hạ", "10 lh" = "10 láng hạ"); từ khóa = chỉ các nhóm có tên / cách viết khớp mọi từ."""
    groups = addr_groups(r["dia_chi"] for r in db.list_orders(limit=None) + db.list_orders(limit=None, canceled=True))
    by: dict[str, dict] = {}
    for r in reversed(db.list_orders(None, status=db.STATUS_ORDERED)):  # cũ nhất trước
        key, label = groups.get((r["dia_chi"] or "").strip(), ("-", ""))
        g = by.setdefault(key, {"key": key, "ten": label, "rows": [], "viet": set()})
        g["rows"].append(r)
        if (r["dia_chi"] or "").strip():
            g["viet"].add(r["dia_chi"].strip())
    kw = fold(" ".join(context.args)).split()
    gs = [g for g in by.values()
          if all(w in fold(" ".join([g["key"], g["ten"], *g["viet"]])) for w in kw)]
    if not gs:
        await update.message.reply_text("Không có đơn Đã đặt nào" + (f" ở địa chỉ khớp “{' '.join(context.args)}”." if kw else "."))
        return
    gs.sort(key=lambda g: (g["key"] == "-", -len(g["rows"]), -sum(r["gia_dat"] or 0 for r in g["rows"])))
    n = sum(len(g["rows"]) for g in gs)
    dat = sum(r["gia_dat"] or 0 for g in gs for r in g["rows"])
    title = (f"🏠 <b>Đơn ⏳ Đã đặt theo địa chỉ</b>" + (f" – tìm “{html.escape(' '.join(context.args))}”" if kw else "")
             + f"\n<i>{n} đơn · {sum(g['key'] != '-' for g in gs)} nơi · tổng giá đặt {money(dat)}</i>\n")
    blocks = [title]
    for g in gs:
        other = sorted(v for v in g["viet"] if v != g["ten"])
        head = (("📭 <b>Chưa có địa chỉ</b>" if g["key"] == "-" else f"🏠 <b>{html.escape(g['ten'])}</b>")
                + f" – {len(g['rows'])} đơn · 💸 {money(sum(r['gia_dat'] or 0 for r in g['rows']))}"
                + (f"\n<i>cũng viết: {html.escape(' · '.join(other))}</i>" if other else ""))
        # từng dòng riêng để reply_long cắt tin được cả khi 1 nhóm dài hơn 4096 ký tự
        blocks += [head, *(open_order_line(r, show_name=True, show_addr=bool(other)) for r in g["rows"]), ""]
    await reply_long(update.message, blocks)


def person_orders_lines(p) -> list[str]:
    """Các đơn người này đang đặt (⏳ Đã đặt / 📦 Đã nhận, chưa trả công), cũ nhất trước, kèm link đã nhận / trả công."""
    rows = db.person_open_orders(p["ten"])
    name = html.escape(p["ten"])
    if not rows:
        return [f"📭 <b>{name}</b> không có đơn nào đang đặt."]
    lines = [f"🧾 <b>Đơn {name} đang đặt</b> ({len(rows)} đơn, cũ nhất trước)\n"]
    lines += [open_order_line(r) for r in rows]
    dat = sum(r["gia_dat"] or 0 for r in rows)
    cong = sum(r["cong_dat"] or 0 for r in rows)
    by_st = " · ".join(f"{STATUS_ICON[st]} {n} {st}" for st in db.UNPAID_STATUSES
                       if (n := sum(r["trang_thai"] == st for r in rows)))
    lines.append(f"\n{by_st}\n💸 Tổng giá đặt <b>{money(dat)}</b> · công chưa trả <b>{money(cong)}</b>")
    return lines


# /nguoidat hn|ngoai|chuaro: lọc theo nguoi_dat.ha_noi (1 / 0 / NULL)
PEOPLE_AREA = {"hn": (1, "📍 ở Hà Nội"), "hanoi": (1, "📍 ở Hà Nội"), "ngoai": (0, "ở nơi khác"),
               "khac": (0, "ở nơi khác"), "chuaro": (None, "chưa rõ ở đâu")}


async def cmd_people(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/nguoidat: danh sách người đặt hộ · /nguoidat hn|ngoai|chuaro: lọc theo Hà Nội ·
    /nguoidat <id hoặc tên>: chi tiết + ảnh QR."""
    area = PEOPLE_AREA.get(fold("".join(context.args)).replace(" ", "")) if context.args else None
    if context.args and not area:
        p = db.find_person(" ".join(context.args))
        if not p:
            await update.message.reply_text("Không tìm thấy người đặt. Xem danh sách: /nguoidat")
            return
        await send_person(update.message, p)
        return
    people = db.list_people()
    title = "Người đặt hộ"
    if area:
        people = [p for p in people if p["ha_noi"] == area[0]]
        title += " " + area[1]
    if not people:
        await update.message.reply_text("Không có người đặt nào." if area else
                                        "Chưa có người đặt nào (tự thêm khi tạo đơn).")
        return
    lines = [f"👥 <b>{title}</b> ({len(people)}) – 📷 = đã có QR, bấm nút để xem/đổi QR"]
    buttons = []
    for p in people:
        qr = "📷" if p["qr_file_id"] else "▫️"
        owe = f" · nợ công <b>{money(p['cong_chua_tra'])}</b>" if p["cong_chua_tra"] else ""
        lines.append(f"{qr} <code>{p['id']:>3}</code>. <b>{html.escape(p['ten'])}</b>"
                     f"{' · ' + html.escape(p['sdt']) if p['sdt'] else ''} · {p['so_don']} đơn{owe}")
        if len(buttons) < 40:
            buttons.append(InlineKeyboardButton(f"{qr} {p['ten'][:18]}", callback_data=f"nd:show:{p['id']}"))
    lines.append("\n<i>/nguoidat &lt;id|tên&gt; · /nguoidat hn|ngoai|chuaro · /suand &lt;id&gt; · /xoand &lt;id&gt;</i>")
    await update.message.reply_html("\n".join(lines)[:4000],
                                    reply_markup=InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)]))


async def on_person_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, action, pid = query.data.split(":")
    p = db.find_person(pid)
    if not p:
        await query.message.reply_text("Không còn người đặt này.")
        return
    if action == "show":
        await send_person(query.message, p)
    elif action == "new":
        context.user_data.pop("pending_qr", None)
        context.user_data["pending_qr_person"] = p["id"]
        context.user_data.pop("pending_merge", None)
        await query.message.reply_html(f"📷 Gửi ảnh QR cho <b>{html.escape(p['ten'])}</b> "
                                       "(dùng chung cho mọi đơn của người này; gõ /huy để thôi).")
    elif action == "del":
        db.set_person_qr(p["id"], None)
        await query.message.reply_html(f"🗑 Đã xóa ảnh QR của <b>{html.escape(p['ten'])}</b> (và các đơn của người này).")


async def cmd_delete_person(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/xoand <id>"""
    if not context.args or not context.args[0].isdigit() or not db.delete_person(int(context.args[0])):
        await update.message.reply_text("Cú pháp: /xoand <id>  (xem id bằng /nguoidat)")
        return
    await update.message.reply_text("🗑 Đã xóa khỏi danh sách người đặt (các đơn vẫn giữ tên và QR). "
                                    "Tạo đơn mới với tên này thì người đặt sẽ tự được thêm lại.")


# ---------- danh bạ SĐT ----------

def phone_fmt(sdt: str) -> str:
    """0912345678 → 0912 345 678 (dễ đọc)."""
    return f"{sdt[:4]} {sdt[4:7]} {sdt[7:]}" if len(sdt) == 10 else sdt


def count_summary(values) -> str:
    """["Shopee", "TikTok", "Shopee"] → "Shopee: <b>2</b> · TikTok: <b>1</b>" (nhiều nhất trước)."""
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return " · ".join(f"{html.escape(k)}: <b>{n}</b>" for k, n in sorted(counts.items(), key=lambda x: -x[1]))


def person_phone_line(x: dict) -> str:
    """1 dòng gợi ý SĐT người đặt từng dùng: số · số đơn (✅ ⏳ ❌) · số của tôi / máy · đang dùng · lần cuối."""
    res = " ".join(f"{icon}{x[k]}" for k, icon in (("ok", "✅"), ("wait", "⏳"), ("fail", "❌")) if x[k])
    parts = [f"<code>{html.escape(x['sdt'])}</code>", f"{x['so_don']} đơn" + (f" ({res})" if res else "")]
    if x["cua_toi"]:
        parts.append(f"{PHONE_MINE} {html.escape(x['may']) if x['may'] else 'số của tôi'}")
    if x["dang_dung"]:
        parts.append(f"{PHONE_BUSY} đang dùng")
    parts.append(f"lần cuối {x['last'][8:10]}/{x['last'][5:7]}")
    return "• " + " · ".join(parts)


def recent_phones() -> list[str]:
    """Gợi ý ở bước nhập SĐT khi /them: các số dùng nhiều nhất."""
    return [p["sdt"] for p in db.list_phones()[:6]]


def phone_report(raw: str) -> list[str]:
    """Các dòng HTML: SĐT đang gắn với những đơn nào, địa chỉ nào."""
    phone = db.get_phone(raw)
    orders = db.orders_by_phone(phone["sdt"] if phone else raw)
    if not orders and not phone:
        return [f"Không có đơn nào dùng SĐT “{html.escape(raw)}”."]
    numbers = sorted({db.norm_phone(o["sdt"]) for o in orders} | ({phone["sdt"]} if phone else set()))
    head = " · ".join(f"<b><code>{n}</code></b>" for n in numbers)
    lines = [f"📞 {head}"]
    if phone and phone["ghi_chu"]:
        lines.append(f"📝 {html.escape(phone['ghi_chu'])}")
    active = [o for o in orders if o["trang_thai"] != db.STATUS_CANCELED]
    lines.append(f"<b>{len(active)} đơn</b>" + (f" (+{len(orders) - len(active)} đơn hủy)" if len(orders) > len(active) else ""))
    if active:
        lines.append("🛒 " + count_summary(o["nguon_don"] or "Chưa rõ sàn" for o in active))
        lines.append("📌 " + count_summary(status_text(o["trang_thai"]) for o in active))
    if not orders:
        return lines + ["<i>Chưa gắn với đơn nào.</i>"]

    # gom địa chỉ (không phân biệt hoa thường / khoảng trắng)
    addrs: dict[str, list] = {}
    for o in orders:
        key = " ".join((o["dia_chi"] or "").lower().split())
        addrs.setdefault(key, []).append(o)
    lines.append(f"\n🏠 <b>{len([k for k in addrs if k])} địa chỉ:</b>")
    for key, group in addrs.items():
        label = html.escape(group[0]["dia_chi"]) if key else "<i>(chưa có địa chỉ)</i>"
        ids = ", ".join(f"#{o['id']}" for o in group)
        lines.append(f"• {label} — <b>{len(group)} đơn</b> ({ids})")

    lines.append("\n📦 <b>Các đơn:</b>")
    for o in orders:
        lines.append(
            f"<b>#{o['id']}</b> · <b>{status_text(o['trang_thai']).upper()}</b> · 🛒 <b>{html.escape(o['nguon_don'] or '—')}</b>"
            f" · {o['created_at'][8:10]}/{o['created_at'][5:7]}\n"
            f"    👤 {html.escape(o['ten_nguoi_dat'] or '—')} · 📦 {html.escape(o['ten_sp'] or '—')}\n"
            f"    🏠 {html.escape(o['dia_chi'] or '—')}\n"
            f"    🔖 {tracking_html(o)} · 🚚 {html.escape(o['don_vi_vc'] or '—')}")
    return lines


async def cmd_phones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sdt: danh bạ SĐT · /sdt <số hoặc id hoặc vài số cuối>: các đơn + địa chỉ gắn với số đó."""
    # bộ lọc: free / dung (trạng thái), toi / khac (chủ số); ghép được, vd /sdt toi free
    used_words = {"free": 0, "chua": 0, "chưa": 0, "moi": 0, "mới": 0,
                  "dung": 1, "dùng": 1, "dang": 1, "đang": 1, "da": 1, "đã": 1}
    mine_words = {"toi": 1, "tôi": 1, "cuatoi": 1, "minh": 1, "mình": 1, "khac": 0, "khác": 0}
    words = [w.lower() for w in context.args]
    if words and not all(w in used_words or w in mine_words for w in words):
        await send_phone_report(update.message, " ".join(context.args))
        return
    only = next((used_words[w] for w in words if w in used_words), None)
    mine = next((mine_words[w] for w in words if w in mine_words), None)
    phones = db.list_phones()
    if only is not None:
        phones = [p for p in phones if p["da_su_dung"] == only]
    if mine is not None:
        phones = [p for p in phones if p["cua_toi"] == mine]
    if not phones:
        await update.message.reply_text("Không có số nào." if words else
                                        "Danh bạ SĐT trống. Thêm: /themsdt <số> [toi] [ghi chú]")
        return
    phones.sort(key=lambda p: (-p["cua_toi"], p["da_su_dung"]))  # số của tôi trước, rồi số free
    free = sum(not p["da_su_dung"] for p in phones)
    n_mine = sum(p["cua_toi"] for p in phones)
    title = ({None: "SĐT", 1: "SĐT của tôi", 0: "SĐT người khác"}[mine]
             + {None: "", 0: " đang free", 1: " đang dùng"}[only])
    title = "Danh bạ SĐT" if title == "SĐT" else title
    lines = [f"📞 <b>{title}</b> ({len(phones)} số · {PHONE_MINE} {n_mine} của tôi · {PHONE_FREE} {free} free)"
             " – bấm nút để xem chi tiết"]
    buttons = []
    for p in phones:
        note = (f" · 📲 {html.escape(p['may'])}" if p["may"] else "") \
            + (f" · {html.escape(p['ghi_chu'])}" if p["ghi_chu"] else "")
        by_src = " · ".join(f"{html.escape(k)} " + " ".join(f"{icon}{c[kind]}" for kind, icon in SRC_ICONS if c[kind])
                            for k, c in p["theo_san"].items())
        lines.append(f"{phone_icon(p)}{mine_icon(p)} <code>{p['id']:>3}</code>. <b><code>{p['sdt']}</code></b> · {p['so_don']} đơn"
                     + (f" ({by_src})" if by_src else "") + f" · {p['so_dia_chi']} địa chỉ{note}"
                     + phone_orders_text(p))
        if len(buttons) < 40:
            buttons.append(InlineKeyboardButton(f"{phone_icon(p)}{mine_icon(p)} {phone_fmt(p['sdt'])} ({p['so_don']})",
                                                callback_data=f"sdt:{p['id']}"))
    lines.append(f"\n<i>{PHONE_FREE} free (mọi đơn đã hoàn thành/hủy) · {PHONE_BUSY} đang dùng (có đơn chưa xong)"
                 f" · {PHONE_MINE} số của tôi\n"
                 "Theo sàn: ✅ giao thành công (đã nhận hàng) · ❌ thất bại (đơn hủy) · ⏳ đang giao\n"
                 "/sdt free · /sdt dung · /sdt toi · /sdt khac (ghép được: /sdt toi free)\n"
                 "/sdt &lt;số&gt; – chi tiết (gõ vài số cuối cũng được) · /themsdt · /suasdt · /xoasdt</i>")
    kb = InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)]) if buttons else None
    await reply_long(update.message, lines[:-1])
    await update.message.reply_html(lines[-1].strip(), reply_markup=kb)


PHONE_FREE, PHONE_BUSY, PHONE_MINE = "🟢", "🔴", "📱"
# số đơn theo sàn của 1 SĐT: giao thành công (đã nhận hàng) / thất bại (đơn hủy) / đang giao
SRC_ICONS = (("ok", "✅"), ("fail", "❌"), ("wait", "⏳"))


def phone_icon(p) -> str:
    return PHONE_BUSY if p["da_su_dung"] else PHONE_FREE


def mine_icon(p) -> str:
    return PHONE_MINE if p["cua_toi"] else ""


def phone_orders_text(p) -> str:
    """Đơn đang giữ số / các đơn đã dùng số (đã xong), để biết số free từng dùng cho đơn nào."""
    ids = lambda xs: ", ".join(f"#{i}" for i in xs[:8]) + ("…" if len(xs) > 8 else "")
    parts = []
    if p["don_dang_dung"]:
        parts.append(f"đang dùng: {ids(p['don_dang_dung'])}")
    if p["don_da_xong"]:
        parts.append(f"đã dùng: {ids(p['don_da_xong'])}")
    return ("\n      ↳ " + " · ".join(parts)) if parts else ""


def phone_status_view(p):
    """Tin nhắn trạng thái của 1 số + nút đổi tay."""
    text = (f"📞 <b><code>{p['sdt']}</code></b> · "
            + (f"{PHONE_BUSY} <b>ĐANG DÙNG</b>" if p["da_su_dung"] else f"{PHONE_FREE} <b>FREE</b>")
            + (f" · {PHONE_MINE} <b>SỐ CỦA TÔI</b>" if p["cua_toi"] else " · 👤 số người khác")
            + (f"\n📲 Lắp ở máy: <b>{html.escape(p['may'])}</b>" if p["may"] else "")
            + "\n<i>Tự đổi theo đơn: có đơn chưa hoàn thành → đang dùng, mọi đơn hoàn thành/hủy → free.</i>")
    btn = (InlineKeyboardButton(f"{PHONE_FREE} Đánh dấu free", callback_data=f"sdtuse:0:{p['id']}") if p["da_su_dung"]
           else InlineKeyboardButton(f"{PHONE_BUSY} Đánh dấu đang dùng", callback_data=f"sdtuse:1:{p['id']}"))
    owner = (InlineKeyboardButton("👤 Là số người khác", callback_data=f"sdtmine:0:{p['id']}") if p["cua_toi"]
             else InlineKeyboardButton(f"{PHONE_MINE} Là số của tôi", callback_data=f"sdtmine:1:{p['id']}"))
    return text, InlineKeyboardMarkup([[btn, owner]])


async def send_phone_report(message, ref: str):
    await reply_long(message, phone_report(ref))
    if p := db.get_phone(ref):
        text, kb = phone_status_view(p)
        await message.reply_html(text, reply_markup=kb)


async def on_phone_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_phone_report(query.message, query.data.split(":", 1)[1])


async def on_phone_used(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, value, pid = query.data.split(":")
    (db.set_phone_mine if action == "sdtmine" else db.set_phone_used)(int(pid), value == "1")
    p = db.get_phone(pid)
    if p:
        text, kb = phone_status_view(p)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


def make_phone_used(used: bool, cmd: str):
    """/dasudung, /chuasudung <id hoặc số> [...]"""
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(f"Cú pháp: /{cmd} <id hoặc số> [id hoặc số ...]")
            return
        done, missing = [], []
        for ref in context.args:
            p = db.get_phone(ref)
            if p:
                db.set_phone_used(p["id"], used)
                done.append(p["sdt"])
            else:
                missing.append(ref)
        msg = (f"{'🔴 Đang dùng' if used else '🟢 Free'}: " + ", ".join(done)) if done else ""
        if missing:
            msg += f"\n⚠️ Không có trong danh bạ: {', '.join(missing)} (thêm bằng /themsdt)"
        await update.message.reply_text(msg.strip())
    return handler


async def cmd_add_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/themsdt <số> [số ...] [toi] [ghi chú] [may <tên máy>] – "toi" = số của tôi; "may …" (cuối lệnh) = sim
    lắp ở điện thoại nào; nhiều số cách nhau dấu cách / phẩy."""
    words = " ".join(context.args).replace(",", " ").split()
    device = None
    if (i := next((i for i, w in enumerate(words) if w.lower() in ("may", "máy")), None)) is not None:
        words, device = words[:i], " ".join(words[i + 1:]) or None
    numbers = [w for w in words if len(db.norm_phone(w)) >= 9]
    mine = any(w.lower() in ("toi", "tôi", "cuatoi", "minh", "mình") for w in words)
    note = " ".join(w for w in words if w not in numbers
                    and w.lower() not in ("toi", "tôi", "cuatoi", "minh", "mình")) or None
    if not numbers:
        await update.message.reply_text("Cú pháp: /themsdt <số> [số ...] [toi] [ghi chú] [may <tên máy>]\n"
                                        "Vd: /themsdt 0912345678 0987654321 toi may iPhone 12 – 2 số của tôi, "
                                        "lắp ở iPhone 12")
        return
    lines = []
    for raw in numbers:
        sdt = db.norm_phone(raw)
        new = db.add_phone(raw, note, mine=mine, device=device)
        lines.append(f"{'✅ Đã thêm' if new else '✔️ Đã có'} <code>{sdt}</code>"
                     + (f" · {PHONE_MINE} của tôi" if mine else "")
                     + (f" · 📲 {html.escape(device)}" if device else "") + (" – cập nhật ghi chú" if note and not new else ""))
    await update.message.reply_html("\n".join(lines) + "\nXem danh bạ: /sdt" + (" toi" if mine else ""))


async def cmd_delete_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/xoasdt <id hoặc số>"""
    phone = db.get_phone(context.args[0]) if context.args else None
    if not phone:
        await update.message.reply_text("Cú pháp: /xoasdt <id hoặc số> (xem id bằng /sdt)")
        return
    db.delete_phone(phone["id"])
    await update.message.reply_html(f"🗑 Đã xóa <code>{phone['sdt']}</code> khỏi danh bạ (các đơn vẫn giữ SĐT). "
                                    "Lưu ý: thêm/sửa đơn dùng số này thì số sẽ tự vào lại danh bạ.")


# ---------- đơn vị vận chuyển ----------

async def cmd_carriers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.list_carriers()
    if not rows:
        await update.message.reply_text("Chưa có đơn vị vận chuyển nào. Thêm: /themdvvc <tên>")
        return
    lines = ["🚚 <b>ĐƠN VỊ VẬN CHUYỂN</b>\n"]
    lines += [f"<code>#{r['id']}</code> <b>{html.escape(r['ten'])}</b> · {r['so_don']} đơn" for r in rows]
    lines.append("\nThêm: /themdvvc &lt;tên&gt; · Đổi tên: /suadvvc &lt;id&gt; · Xóa: /xoadvvc &lt;id&gt;")
    await reply_long(update.message, lines)


async def cmd_add_carrier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/themdvvc <tên>  (nhiều tên cách nhau dấu phẩy)."""
    names = [n.strip() for n in " ".join(context.args).split(",") if n.strip()]
    if not names:
        await update.message.reply_text("Cú pháp: /themdvvc <tên>  (nhiều tên: /themdvvc Ahamove, Grab)")
        return
    added, existed = [], []
    for name in names:
        if len(name) > 40:
            await update.message.reply_text(f"⚠️ Tên quá dài (tối đa 40 ký tự): {name}")
            return
        (added if db.add_carrier(name) else existed).append(name)
    msg = f"✅ Đã thêm: {', '.join(added)}" if added else ""
    if existed:
        msg += f"\nĐã có sẵn: {', '.join(existed)}"
    await update.message.reply_text(msg.strip() + "\nXem danh sách: /dvvc")


async def cmd_delete_carrier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/xoadvvc <id> – bỏ khỏi danh sách chọn, đơn cũ vẫn giữ tên."""
    carrier_id = parse_id(context)
    if carrier_id is None:
        await update.message.reply_text("Cú pháp: /xoadvvc <id>  (xem id ở /dvvc)")
        return
    c = db.get_carrier(carrier_id)
    if not c or not db.delete_carrier(carrier_id):
        await update.message.reply_text("Không tìm thấy. Xem id ở /dvvc")
        return
    await update.message.reply_text(f"🗑 Đã xóa “{c['ten']}” khỏi danh sách chọn (các đơn cũ vẫn giữ tên).")


# ---------- sản phẩm ----------

PIMG_PENDING_MINUTES = 15  # sau /anhsp, ảnh gửi trong ngần này phút (tính từ ảnh gần nhất) là ảnh sản phẩm


def resolve_product_name(ref: str) -> str | None:
    """id / tên trong danh mục, hoặc tên món sale chưa có trong danh mục."""
    ref = " ".join(ref.split())
    if not ref:
        return None
    if p := db.find_product(ref):
        return p["ten"]
    return db.product_name_known(ref)


def pending_product(context) -> str | None:
    st = context.user_data.get("pending_pimg")
    if st and datetime.now().timestamp() - st["at"] < PIMG_PENDING_MINUTES * 60:
        return st["name"]
    context.user_data.pop("pending_pimg", None)
    return None


async def send_product_images(message, name: str, numbered: bool = False) -> int:
    """Gửi ảnh của sản phẩm thành album (numbered: chú thích số thứ tự để xóa bằng /xoaanhsp). Trả về số ảnh."""
    photos = db.product_images(name)
    if not photos:
        return 0
    if len(photos) == 1:
        await reply_saved_photo(message, photos[0]["file_id"], caption="1" if numbered else None)
    else:
        await reply_saved_album(message, [p["file_id"] for p in photos],
                                [str(j + 1) if numbered else None for j in range(len(photos))])
    return len(photos)


async def cmd_product_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/anhsp <id|tên>: xem ảnh sản phẩm rồi gửi thêm ảnh (/xong khi xong); không tham số = sản phẩm nào có ảnh."""
    ref = " ".join(context.args)
    if not ref:
        counts = {k: len(v) for k, v in db.product_images_map().items()}
        names = {db.name_key(r["ten"]): (r["id"], r["ten"]) for r in db.list_products()}
        lines = ["🖼 <b>ẢNH SẢN PHẨM</b> (/sale gửi kèm ảnh của từng sản phẩm)\n"]
        lines += [f"<code>#{names[k][0]}</code> {html.escape(names[k][1])} · {n} ảnh" if k in names
                  else f"{html.escape(k)} · {n} ảnh (món sale)" for k, n in counts.items()] or ["Chưa có sản phẩm nào có ảnh."]
        lines.append("\n<i>/anhsp &lt;id|tên&gt; – xem + thêm ảnh · /xoaanhsp &lt;id|tên&gt; &lt;số|all&gt; – xóa ảnh"
                     " · hoặc gửi ảnh kèm chú thích: anhsp &lt;id|tên&gt;</i>")
        await reply_long(update.message, lines)
        return
    name = resolve_product_name(ref)
    if not name:
        await update.message.reply_text(f"Không có sản phẩm / món sale “{ref}”. Xem id ở /sanpham.")
        return
    await start_product_images(update.message, context, name)


async def start_product_images(message, context, name: str):
    """Gửi ảnh đang có (đánh số) rồi chờ ảnh mới cho sản phẩm (dùng chung /anhsp và nút ở /suasale, /suasp)."""
    n = await send_product_images(message, name, numbered=True)
    for k in ("pending_qr", "pending_qr_person", "pending_edit"):  # ảnh gửi tiếp theo là ảnh sản phẩm
        context.user_data.pop(k, None)
    context.user_data["pending_pimg"] = {"name": name, "at": datetime.now().timestamp()}
    context.user_data.pop("pending_merge", None)
    await message.reply_html(
        f"🖼 <b>{html.escape(name)}</b> · đang có {n}/{db.MAX_PRODUCT_IMAGES} ảnh (/sale gửi kèm).\n"
        f"📷 Gửi ảnh vào đây (gửi nhiều ảnh 1 lần được) để thêm. Xong bấm /xong."
        + (f"\nXóa ảnh: <code>/xoaanhsp {html.escape(name)} &lt;số thứ tự|all&gt;</code>" if n else ""))


async def on_product_images_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút 🖼 Ảnh sản phẩm trong /suasale, /suasp: pimg:<sales|products>:<id>."""
    query = update.callback_query
    await query.answer()
    _, table, rec_id = query.data.split(":")
    r = db.get(table, int(rec_id))
    if not r:
        await query.edit_message_text("Không tìm thấy.")
        return
    await query.edit_message_reply_markup(None)
    await start_product_images(query.message, context, r["ten_sp"] if table == "sales" else r["ten"])


async def cmd_product_images_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/xong: thôi nhận ảnh sản phẩm."""
    st = context.user_data.pop("pending_pimg", None)
    if not st:
        await update.message.reply_text("Không có gì đang chờ.")
        return
    n = len(db.product_images(st["name"]))
    await update.message.reply_html(f"✅ Xong · <b>{html.escape(st['name'])}</b> có {n} ảnh.")


async def cmd_delete_product_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/xoaanhsp <id|tên> <số…|all>: xóa ảnh theo số thứ tự (xem số ở /anhsp)."""
    args = list(context.args)
    name, picks = None, []
    # tên dài nhất còn khớp (tên có thể tận cùng bằng số, vd "i5 14600"), phần sau toàn số thứ tự / all
    for k in range(len(args) - 1, 0, -1):
        rest = [a.lower() for a in args[k:]]
        if all(a.isdigit() or a == "all" for a in rest) and (name := resolve_product_name(" ".join(args[:k]))):
            picks = rest
            break
    if not name or not picks:
        await update.message.reply_text("Cú pháp: /xoaanhsp <id|tên> <số thứ tự…|all>  (xem số thứ tự ở /anhsp <id|tên>)")
        return
    photos = db.product_images(name)
    if "all" in picks:
        targets = photos
    else:
        targets = [photos[int(i) - 1] for i in picks if 0 < int(i) <= len(photos)]
    for p in targets:
        db.delete_product_image(p["id"])
    await update.message.reply_html(f"🗑 Đã xóa {len(targets)} ảnh · <b>{html.escape(name)}</b> còn "
                                    f"{len(photos) - len(targets)} ảnh.")


async def _ack_product_photos(message, context, key: str):
    """Album nhiều ảnh = nhiều tin → chờ 2s rồi báo 1 lần."""
    await asyncio.sleep(2)
    st = context.user_data.get("pimg_ack", {}).pop(key, None)
    if not st:
        return
    n = len(db.product_images(st["name"]))
    await message.reply_html(f"✅ Đã lưu {st['ok']} ảnh cho <b>{html.escape(st['name'])}</b> "
                             f"(đang có {n}/{db.MAX_PRODUCT_IMAGES})"
                             + (f"\n⚠️ {st['full']} ảnh không lưu vì đã đủ {db.MAX_PRODUCT_IMAGES} ảnh" if st["full"] else "")
                             + "\nGửi tiếp ảnh hoặc /xong.")


async def on_product_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Ảnh sản phẩm: ảnh có chú thích "anhsp <id|tên>", hoặc ảnh gửi sau /anhsp (khi không chờ ảnh QR)."""
    msg = update.message
    m = re.match(r"^/?anhsp\s+(.+)", (msg.caption or "").strip(), re.IGNORECASE | re.DOTALL)
    if m:
        name = resolve_product_name(m[1])
        if not name:
            await msg.reply_text(f"Không có sản phẩm / món sale “{m[1].strip()}”. Xem id ở /sanpham.")
            return True
    elif re.match(r"^/?qr\b", (msg.caption or "").strip(), re.IGNORECASE) or \
            context.user_data.get("pending_qr") or context.user_data.get("pending_qr_person"):
        return False
    elif not (name := pending_product(context)):
        return False
    context.user_data["pending_pimg"] = {"name": name, "at": datetime.now().timestamp()}
    ok = db.add_product_image(name, msg.photo[-1].file_id) is not None
    if ok:
        cache_qr(msg.photo[-1])  # lưu ảnh vào database của web
    key = msg.media_group_id or str(msg.message_id)
    acks = context.user_data.setdefault("pimg_ack", {})
    st = acks.get(key)
    if not st:
        st = acks[key] = {"name": name, "ok": 0, "full": 0}
        asyncio.create_task(_ack_product_photos(msg, context, key))
    st["ok" if ok else "full"] += 1
    return True


# ---------- ghép ảnh ----------

MERGE_PENDING_MINUTES = 10  # sau /ghep, ảnh gửi trong ngần này phút (tính từ ảnh gần nhất) là ảnh để ghép
MERGE_MAX = 10
MERGE_MODES = {"ngang": "ngang", "canh": "ngang", "doc": "doc", "dọc": "doc", "chong": "doc"}
MERGE_CAPTION_RE = re.compile(r"^/?gh[eé]p\b\s*(\S*)", re.IGNORECASE)


def start_merge(context, mode: str | None):
    for k in ("pending_qr", "pending_qr_person", "pending_pimg"):  # ảnh gửi tiếp theo là ảnh để ghép
        context.user_data.pop(k, None)
    context.user_data["pending_merge"] = {"mode": mode, "photos": [], "at": datetime.now().timestamp(), "seq": 0}


async def cmd_merge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ghep [ngang|doc]: gửi 2+ ảnh, bot ghép thành 1 ảnh."""
    arg = (context.args[0].lower() if context.args else "")
    mode = MERGE_MODES.get(arg)
    if arg and not mode:
        await update.message.reply_text("Cú pháp: /ghep [ngang|doc]  (bỏ trống = tự chọn)")
        return
    start_merge(context, mode)
    how = {"ngang": "đặt cạnh nhau (ngang)", "doc": "xếp chồng từ trên xuống (dọc)"}.get(
        mode, "tự chọn: ảnh dọc → đặt cạnh nhau, ảnh ngang → xếp chồng")
    await update.message.reply_html(
        f"🧩 <b>Ghép ảnh</b> · {how}\n"
        f"📷 Gửi 2 ảnh trở lên (chọn nhiều ảnh gửi 1 lần, tối đa {MERGE_MAX}), bot ghép theo thứ tự gửi.\n"
        "<i>/ghep ngang · /ghep doc để chọn kiểu · /huy để thôi · hoặc gửi album kèm chú thích: ghep</i>")


async def send_merged(message, context, photos: list[str], mode: str | None, note: str = ""):
    """Tải ảnh từ Telegram, ghép, gửi lại kèm nút đổi kiểu ghép (ảnh nguồn nhớ ở user_data['last_merge'])."""
    try:
        data = [bytes(await (await context.bot.get_file(f)).download_as_bytearray()) for f in photos]
        jpg, mode, size = await asyncio.to_thread(imgmerge.merge, data, mode)
    except ImportError:
        await message.reply_text("⚠️ Máy chủ chưa cài thư viện ghép ảnh (Pillow) – cần build lại image.")
        return
    except Exception as e:
        log.warning("Ghép ảnh lỗi: %s", e)
        await message.reply_text(f"⚠️ Không ghép được ảnh: {e}")
        return
    context.user_data["last_merge"] = photos
    other = "doc" if mode == "ngang" else "ngang"
    caption = (f"🧩 Đã ghép {len(photos)} ảnh · {'cạnh nhau' if mode == 'ngang' else 'xếp chồng'}{note}"
               f" · {size[0]}×{size[1]}\n💡 Ảnh gốc nét hơn nếu gửi ảnh vào dạng File (📎 → File) thay vì Ảnh")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "🔄 Ghép xếp chồng (dọc)" if other == "doc" else "🔄 Ghép cạnh nhau (ngang)", callback_data=f"ghep:{other}")]])
    # gửi dạng file: gửi dạng ảnh thì Telegram nén lại (tối đa 2560px, JPEG thấp) làm mờ chữ
    await message.reply_document(jpg, filename="anh-ghep.jpg", caption=caption, reply_markup=kb)


async def _finish_merge(message, context, seq: int):
    """Chờ 2s sau ảnh cuối (album = nhiều tin), đủ 2 ảnh thì ghép và gửi lại."""
    await asyncio.sleep(2)
    st = context.user_data.get("pending_merge")
    if not st or st["seq"] != seq:  # còn ảnh mới tới → để task sau xử lý
        return
    if len(st["photos"]) < 2:
        await message.reply_text("📷 Đã nhận 1 ảnh, gửi thêm ảnh nữa để ghép (/huy để thôi).")
        return
    context.user_data.pop("pending_merge", None)
    await send_merged(message, context, st["photos"][:MERGE_MAX], st["mode"],
                      f" (chỉ lấy {MERGE_MAX} ảnh đầu)" if len(st["photos"]) > MERGE_MAX else "")


async def on_merge_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút 🔄 dưới ảnh ghép: ghép lại các ảnh vừa rồi theo kiểu kia (ghep:ngang|doc)."""
    query = update.callback_query
    await query.answer()
    photos = context.user_data.get("last_merge")
    if not photos:
        await query.message.reply_text("Không còn ảnh cũ để ghép lại, gõ /ghep rồi gửi lại ảnh.")
        return
    await query.edit_message_reply_markup(None)
    await send_merged(query.message, context, photos, query.data.split(":")[1])


async def on_merge_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Ảnh để ghép: ảnh / album có chú thích "ghep [ngang|doc]", hoặc ảnh gửi sau /ghep.
    Nhận cả ảnh gửi dạng file (không bị Telegram nén → ảnh ghép nét hơn)."""
    msg = update.message
    if m := MERGE_CAPTION_RE.match((msg.caption or "").strip()):
        start_merge(context, MERGE_MODES.get(m[1].lower()))
    st = context.user_data.get("pending_merge")
    if not st:
        return False
    now = datetime.now().timestamp()
    if now - st["at"] > MERGE_PENDING_MINUTES * 60:
        context.user_data.pop("pending_merge", None)
        return False
    st["photos"].append(msg.photo[-1].file_id if msg.photo else msg.document.file_id)
    st["at"] = now
    st["seq"] += 1
    asyncio.create_task(_finish_merge(msg, context, st["seq"]))
    return True


async def on_merge_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ảnh gửi dạng file: chỉ dùng cho /ghep."""
    if not await on_merge_photo(update, context):
        await update.message.reply_text("Muốn ghép ảnh thì gõ /ghep rồi gửi lại các ảnh.")


async def cmd_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sanpham [từ khóa]: danh mục sản phẩm + giá thu gợi ý."""
    keyword = " ".join(context.args).strip()
    rows = db.list_products(keyword or None)
    if not rows:
        await update.message.reply_text("Không có sản phẩm nào." + (f" (tìm: {keyword})" if keyword else "")
                                        + "\nThêm: /themsp <tên> | <giá thu gợi ý>")
        return
    lines = ["📦 <b>SẢN PHẨM</b> · giá thu gợi ý\n"]
    lines += [f"<code>#{r['id']}</code> <b>{html.escape(r['ten'])}</b> · "
              f"{range_short(r['gia_goi_y'], r['gia_goi_y_max']) if r['gia_goi_y'] else 'chưa có giá'} · "
              f"{r['so_don']} đơn" for r in rows]
    lines.append("\n<i>/themsp &lt;tên&gt; | &lt;giá&gt; · /giasp &lt;id&gt; &lt;giá&gt; · "
                 "/suasp &lt;id&gt; · /xoasp &lt;id&gt;</i>")
    await reply_long(update.message, lines)


async def cmd_add_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/themsp <tên> | <giá thu gợi ý>  (giá hoặc khoảng giá; nhiều sản phẩm: mỗi dòng 1 cái, giá bỏ trống được)."""
    text = update.message.text.split(maxsplit=1)[1] if len(update.message.text.split(maxsplit=1)) > 1 else ""
    items = [line.split("|", 1) for line in text.splitlines() if line.strip()]
    if not items:
        await update.message.reply_text("Cú pháp: /themsp <tên> | <giá thu gợi ý>\n"
                                        "Ví dụ: /themsp RAM Viper 16GB | 4tr7 - 4tr750\n"
                                        "Nhiều sản phẩm: mỗi dòng 1 cái.")
        return
    added, existed = [], []
    for item in items:
        name = " ".join(item[0].split())
        price = parse_range(item[1]) if len(item) > 1 and item[1].strip() else (0, None)
        if not name or price is None:
            await update.message.reply_text(f"⚠️ Dòng không hợp lệ: {'|'.join(item)}")
            return
        (added if db.add_product(name, *price) else existed).append(name)
    msg = f"✅ Đã thêm: {', '.join(added)}" if added else ""
    if existed:
        msg += f"\nĐã có sẵn (đổi giá bằng /giasp): {', '.join(existed)}"
    await update.message.reply_text(msg.strip() + "\nXem danh sách: /sanpham")


async def cmd_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/giasp <id> <giá thu gợi ý>  (1 giá hoặc khoảng: 4tr7 - 4tr750; 0 = xóa giá)"""
    price = parse_range(" ".join(context.args[1:])) if len(context.args) >= 2 else None
    p = db.find_product(context.args[0]) if context.args and context.args[0].isdigit() else None
    if price is None or not p:
        await update.message.reply_text("Cú pháp: /giasp <id> <giá>  (vd: /giasp 3 4tr7 - 4tr750, xem id ở /sanpham)")
        return
    db.set_product_price(p["id"], *price)
    await update.message.reply_text(f"📈 {p['ten']}: giá thu gợi ý "
                                    f"{range_short(*price) if price[0] else 'đã xóa'}")


async def cmd_delete_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/xoasp <id> – bỏ khỏi danh mục, đơn / món sale cũ vẫn giữ tên."""
    product_id = parse_id(context)
    p = db.find_product(str(product_id)) if product_id is not None else None
    if not p or not db.delete_product(p["id"]):
        await update.message.reply_text("Cú pháp: /xoasp <id>  (xem id ở /sanpham)")
        return
    await update.message.reply_text(f"🗑 Đã xóa “{p['ten']}” khỏi danh mục (đơn cũ vẫn giữ tên).")


# ---------- lệnh riêng: săn sale ----------

MAX_SALE_GROUP = 99  # /sale 1…99 = nhóm; số dài hơn (/sale 14600, /sale 3050) vẫn là từ khóa tên


def parse_sale_args(args, flags=("all",), active_only=True):
    """/sale [all] [copy] [nhóm] [từ khóa]: all = gồm cả món hết sale; copy = dạng tin nhắn gốc;
    số 1–99 đứng riêng = nhóm sale; còn lại = từ khóa lọc theo tên + shop (mọi từ, không dấu, như find_sales_by_words)."""
    args = list(args)
    on = {f for f in flags if f in (a.lower() for a in args)}
    rest = [a for a in args if a.lower() not in on]
    i = next((i for i, a in enumerate(rest) if a.isdigit() and 0 < int(a) <= MAX_SALE_GROUP), None)
    nhom = int(rest.pop(i)) if i is not None else None
    keyword = " ".join(rest).strip() or None
    rows = db.list_sales(active_only=active_only and "all" not in on, nhom=nhom)
    if keyword:
        rows = match_sales_words(rows, keyword)
    return on, keyword, nhom, rows


def no_sale_text(keyword, nhom, active_only=True) -> str:
    """Báo không có món nào (kèm các nhóm đang có khi lọc theo nhóm)."""
    text = "Không có món sale nào" + (f" trong nhóm {nhom}" if nhom else "") + (f" (tìm: {keyword})" if keyword else "") + "."
    if nhom and (groups := db.sale_groups(active_only)):
        text += "\nCác nhóm đang có: " + ", ".join(f"/sale {g} ({n} món)" for g, n in groups)
    return text


async def cmd_sale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sale: danh sách dễ đọc; /sale copy: đúng dạng tin nhắn gốc để copy; /sale 1: chỉ nhóm 1."""
    await reply_sales(update.message, context.args)


async def reply_sales(message, args):
    """/sale và chat "sale <từ khóa>": cùng tham số all / copy / text / nhóm / từ khóa."""
    on, keyword, nhom, rows = parse_sale_args(args, flags=("all", "copy", "text"))
    if not rows:
        await message.reply_text(no_sale_text(keyword, nhom, "all" not in on))
        return
    show_all = "all" in on
    if "copy" in on:
        await reply_long(message, sale_text.format_block(rows, show_status=show_all))
        return
    # không kèm link ➕ Thêm link · ⛔ Tắt sale để copy gửi khách (thao tác qua /saleall)
    if "text" in on:  # /sale text: chỉ chữ, không gửi ảnh
        await reply_long(message, sale_text.compact_cards(rows, show_status=show_all))
        return
    await send_sale_cards(message, rows, show_all)


CAPTION_LIMIT = 1024  # chú thích ảnh / album Telegram


async def send_sale_cards(message, rows, show_status: bool):
    """/sale: mỗi sản phẩm có ảnh (/anhsp, web Sản phẩm) → 1 album ảnh, thẻ sale làm chú thích ảnh đầu (thẻ dài quá
    1024 ký tự thì gửi thẻ thành tin riêng ngay sau album); sản phẩm không ảnh gộp chữ như cũ, giữ đúng thứ tự."""
    images = db.product_images_map()
    chunk = ""

    async def flush():
        nonlocal chunk
        if chunk.strip():
            await message.reply_html(chunk, disable_web_page_preview=True)
        chunk = ""

    for g in sale_text.group_by_product(rows):
        card = sale_text.compact_card(g, show_status)  # dạng gọn (người dùng yêu cầu); /saleall giữ dạng cây chi tiết
        photos = images.get(db.name_key(g[0]["ten_sp"]), [])[:db.MAX_PRODUCT_IMAGES]
        if not photos:
            if chunk and len(chunk) + len(card) + 1 > 4000:
                await flush()
            chunk += card + "\n"
            continue
        await flush()
        caption = card if len(card) <= CAPTION_LIMIT else None
        try:
            if len(photos) == 1:
                await reply_saved_photo(message, photos[0]["file_id"], caption=caption, parse_mode="HTML")
            else:
                await reply_saved_album(message, [p["file_id"] for p in photos],
                                        [caption] + [None] * (len(photos) - 1), parse_mode="HTML")
        except Exception:  # ảnh lỗi (file_id hỏng…) → vẫn gửi thẻ chữ
            log.exception("Không gửi được ảnh sản phẩm %s", g[0]["ten_sp"])
            caption = None
        if caption is None:
            await message.reply_html(card, disable_web_page_preview=True)
    await flush()


def sale_actions(r) -> str:
    """Dòng link thao tác dưới mỗi shop trong /saleall (deep link start=slink_ / stog_)."""
    url = f"https://t.me/{BOT_USERNAME}?start="
    toggle = "⛔ Tắt sale" if r["active"] else "✅ Bật sale"
    return (f'<a href="{url}slink_{r["id"]}">➕ Thêm link</a> · '
            f'<a href="{url}stog_{r["id"]}">{toggle}</a>')


async def cmd_sale_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/saleall [nhóm] [từ khóa]: như /sale nhưng gồm cả món hết sale, mỗi shop có link ➕ Thêm link · bật/tắt sale."""
    _, keyword, nhom, rows = parse_sale_args(context.args, flags=(), active_only=False)
    if not rows:
        await update.message.reply_text(no_sale_text(keyword, nhom, False))
        return
    await reply_long(update.message, sale_text.format_cards(rows, show_status=True, actions={r["id"]: sale_actions(r) for r in rows},
                                                             show_goc=True))


async def cmd_sale_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/nhomsale <nhóm> <id…>: gán nhiều món vào 1 nhóm (0 = bỏ nhóm); không tham số = xem các nhóm."""
    args = context.args
    if not args:
        groups = db.sale_groups(active_only=False)
        await update.message.reply_html(
            ("🗂 <b>Nhóm sale</b>\n" + "\n".join(f"• Nhóm {g}: {n} món · /sale {g}" for g, n in groups) if groups
             else "Chưa có nhóm sale nào.")
            + "\n\nGán nhóm: <code>/nhomsale 1 2 3 5</code> (món #2, #3, #5 vào nhóm 1; id xem ở /dssale)"
              "\nBỏ nhóm: <code>/nhomsale 0 2 3</code>")
        return
    nums = [a.lstrip("#") for a in " ".join(args).replace(",", " ").split()]
    if len(nums) < 2 or not all(n.isdigit() for n in nums) or int(nums[0]) > MAX_SALE_GROUP:
        await update.message.reply_html(f"Cú pháp: <code>/nhomsale &lt;nhóm 0–{MAX_SALE_GROUP}&gt; &lt;id món…&gt;</code>, "
                                        "vd <code>/nhomsale 1 2 3 5</code> · 0 = bỏ nhóm")
        return
    nhom, ids = int(nums[0]), [int(n) for n in nums[1:]]
    done = db.set_sale_group(ids, nhom)
    missing = [i for i in ids if i not in done]
    text = (f"✅ {len(done)} món → " + (f"🗂 nhóm {nhom}. Xem: /sale {nhom}" if nhom else "bỏ nhóm.")) if done else "Không đổi món nào."
    if missing:
        text += "\n⚠️ Không có món: " + ", ".join(f"#{i}" for i in missing)
    await update.message.reply_text(text)


async def send_sale_links(message, context, rec_id: int):
    """Link ➕ Thêm link trong /saleall: mở bảng sửa link của món, chờ gửi link."""
    r = db.get("sales", rec_id)
    if not r:
        await message.reply_text("Không tìm thấy món sale.")
        return
    context.user_data["pending_edit"] = ("sales", r["id"], "link")
    text, kb = sale_links_panel(r)
    await message.reply_html(text, reply_markup=kb, disable_web_page_preview=True)


async def toggle_sale_active(message, rec_id: int):
    """Link bật/tắt sale trong /saleall."""
    r = db.get("sales", rec_id)
    if not r:
        await message.reply_text("Không tìm thấy món sale.")
        return
    value = 0 if r["active"] else 1
    db.update("sales", r["id"], "active", value)
    shop = f" – {html.escape(r['shop'])}" if r["shop"] else ""
    await message.reply_html(f"<code>#{r['id']}</code> {html.escape(r['ten_sp'])}{shop} → "
                             + ("✅ còn sale" if value else "⛔ hết sale") + "\nXem lại: /saleall")


async def cmd_sale_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dssale: bảng quản lý có id."""
    on, keyword, _, rows = parse_sale_args(context.args)
    show_all = "all" in on
    if not rows:
        await update.message.reply_text("Không có món sale nào." + (f" (tìm: {keyword})" if keyword else ""))
        return
    title = "🔥 <b>DANH SÁCH SĂN SALE</b>"
    if keyword:
        title += f" – tìm “{html.escape(keyword)}”"
    title += f"\n<i>{len(rows)} món{' (gồm cả hết sale)' if show_all else ' đang sale'}</i>\n"
    await reply_long(update.message, [title, *fmt_sale_table(rows, show_status=show_all)])


SALE_TOGGLE_RE = re.compile(r"^sale\s+(bat|mo|con|tat|het)\s+(.+)$")


def match_sales_words(rows, query: str) -> list:
    """Món sale (trong rows, giữ thứ tự) khớp mọi từ của query (không dấu, không phân biệt hoa/thường) trong tên + shop.
    Ưu tiên khớp nguyên từ ("14600k" không ăn "14600kf"); không có thì khớp một phần."""
    words = tracking.fold(query).split()
    texts = [(r, tracking.fold(f"{r['ten_sp']} {r['shop'] or ''}")) for r in rows]
    exact = [r for r, t in texts if all(w in re.split(r"[^0-9a-z]+", t) for w in words)]
    return exact or [r for r, t in texts if all(w in t for w in words)]


def find_sales_by_words(query: str) -> list:
    return match_sales_words(db.list_sales(active_only=False), query)


SALE_SHOW_RE = re.compile(r"^sale\s+(.+)$", re.S | re.I)


async def on_sale_show_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Chat "sale màn hình" = /sale màn hình: chỉ thẻ sale của các món khớp (cũng nhận all / copy / text / nhóm).
    "sale bật|tắt|link …" do on_sale_toggle_text / on_sale_link_text xử lý trước."""
    m = SALE_SHOW_RE.match(update.message.text.strip())
    if not m or context.user_data.get("pending_edit") or context.user_data.get("pending_address"):
        return False
    words = m.group(1).split()
    if tracking.fold(words[0]) in ("bat", "mo", "con", "tat", "het", "link"):
        return False
    context.user_data.pop("pending_slink", None)  # đang mở bảng paste link mà hỏi sale → bỏ bảng
    await reply_sales(update.message, words)
    return True


async def on_sale_toggle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Chat "sale bật 14600k dtech" / "sale tắt 3 5": bật/tắt món sale theo từ khóa hoặc id.
    Nhiều món trong 1 tin: cách nhau bằng dấu phẩy / chấm phẩy / xuống dòng ("sale tắt 14600kf, màn hình")."""
    m = SALE_TOGGLE_RE.match(tracking.fold(re.sub(r"[\n;]+", ",", update.message.text)))
    if not m:
        return False
    value = 0 if m.group(1) in ("tat", "het") else 1
    query = m.group(2).strip(" ,")
    if query == "all":
        n = db.set_all_sales_active(value)
        await update.message.reply_text(f"Đã chuyển {n} món → " + ("✅ còn sale" if value else "⛔ hết sale"))
        return True
    rows, missing = {}, []
    for part in (p.strip() for p in query.split(",")):
        if not part:
            continue
        ids = part.split()
        found = ([r for r in (db.get("sales", int(a)) for a in ids) if r] if all(a.isdigit() for a in ids)
                 else find_sales_by_words(part))
        if not found:
            missing.append(part)
        rows.update((r["id"], r) for r in found)
    rows = list(rows.values())
    miss = ("\n⚠️ Không tìm thấy: " + ", ".join(f"“{html.escape(x)}”" for x in missing)) if missing else ""
    if not rows:
        await update.message.reply_html(f"Không tìm thấy món sale nào khớp “{html.escape(query)}”.")
        return True
    lines = []
    for r in rows:
        if r["active"] != value:
            db.update("sales", r["id"], "active", value)
        shop = f" – {html.escape(r['shop'])}" if r["shop"] else ""
        lines.append(f"<code>#{r['id']}</code> {html.escape(r['ten_sp'])}{shop}")
    head = ("✅ Đã bật sale" if value else "⛔ Đã tắt sale") + f" {len(rows)} món:"
    await update.message.reply_html("\n".join([head, *lines]) + miss + "\nXem: /sale" + ("" if value else "all"))
    return True


SLINK_PENDING_MINUTES = 30  # bảng paste link mới hết hiệu lực sau 30 phút không gửi gì
SALE_LINK_RE = re.compile(r"^sale\s+link\s+(.+)$")


def new_links_panel(r, urls: list[str]) -> tuple[str, InlineKeyboardMarkup]:
    """Bảng nhập link mới cho món sale: link mới chưa lưu, ✅ Xong = thay hết link cũ bằng link mới."""
    old = (r["link"] or "").split()
    shop = f" – {html.escape(r['shop'])}" if r["shop"] else ""
    lines = [f"🔗 <b>Link mới</b> cho <code>#{r['id']}</code> {html.escape(r['ten_sp'])}{shop}",
             f"<i>Link cũ: {len(old)} link</i>" + (" – sẽ bị xóa khi bấm ✅ Xong" if old else ""), ""]
    lines += [f"{i}. {html.escape(u)}" for i, u in enumerate(urls, 1)] or ["<i>(chưa có link mới)</i>"]
    lines.append("\nPaste link liên tục (mỗi tin 1 hoặc nhiều link). Xong thì bấm ✅ Xong.")
    buttons = [InlineKeyboardButton(f"🗑 {i}", callback_data=f"sln:del:{i - 1}") for i in range(1, len(urls) + 1)]
    rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    last = []
    if urls:
        last.append(InlineKeyboardButton(f"✅ Xong ({len(urls)} link)", callback_data="sln:ok"))
        if old:
            last.append(InlineKeyboardButton("➕ Giữ cả link cũ", callback_data="sln:keep"))
    last.append(InlineKeyboardButton("✖️ Thôi", callback_data="sln:x"))
    rows.append(last)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def start_new_links(message, context, rec_id: int, urls: list[str] | None = None, edit=None):
    r = db.get("sales", rec_id)
    if not r:
        await message.reply_text("Không tìm thấy món sale.")
        return
    context.user_data.pop("pending_edit", None)
    context.user_data["pending_slink"] = {"id": rec_id, "urls": list(dict.fromkeys(urls or [])),
                                          "at": datetime.now()}
    text, kb = new_links_panel(r, context.user_data["pending_slink"]["urls"])
    send = edit or message.reply_html
    await send(text, reply_markup=kb, disable_web_page_preview=True, **({"parse_mode": "HTML"} if edit else {}))


async def on_sale_link_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Chat "sale link 14600k dtech" → bảng paste link mới; khi đang mở bảng, mọi link gửi tới được thêm vào."""
    raw = update.message.text
    m = SALE_LINK_RE.match(tracking.fold(sale_text.URL_RE.sub(" ", raw)))
    if m:
        urls = sale_text.URL_RE.findall(raw)
        query = m.group(1).strip()
        ids = query.split()
        rows = ([r for r in (db.get("sales", int(a)) for a in ids) if r] if all(a.isdigit() for a in ids)
                else find_sales_by_words(query))
        if not rows:
            await update.message.reply_text(f"Không tìm thấy món sale nào khớp “{query}”.")
        elif len(rows) == 1:
            await start_new_links(update.message, context, rows[0]["id"], urls)
        else:
            context.user_data["slink_first"] = urls
            kb = [[InlineKeyboardButton(f"#{r['id']} {r['ten_sp']}" + (f" – {r['shop']}" if r["shop"] else "")
                                        + ("" if r["active"] else " ⛔"), callback_data=f"sln:pick:{r['id']}")]
                  for r in rows[:20]]
            await update.message.reply_text(f"Có {len(rows)} món khớp “{query}”, chọn món:",
                                            reply_markup=InlineKeyboardMarkup(kb))
        return True
    state = context.user_data.get("pending_slink")
    if not state:
        return False
    if (context.user_data.get("pending_edit") or context.user_data.get("pending_address")
            or datetime.now() - state["at"] > timedelta(minutes=SLINK_PENDING_MINUTES)):
        context.user_data.pop("pending_slink", None)  # bảng cũ bỏ dở / đang sửa việc khác
        return False
    state["at"] = datetime.now()
    new = sale_text.URL_RE.findall(raw)
    if not new:
        await update.message.reply_text("⚠️ Không thấy link nào (link phải bắt đầu bằng http). "
                                        "Paste lại, bấm ✅ Xong, hoặc /huy để thôi.")
        return True
    r = db.get("sales", state["id"])
    if not r:
        context.user_data.pop("pending_slink", None)
        await update.message.reply_text("Không tìm thấy món sale.")
        return True
    state["urls"] += [u for u in dict.fromkeys(new) if u not in state["urls"]]
    text, kb = new_links_panel(r, state["urls"])
    await update.message.reply_html(text, reply_markup=kb, disable_web_page_preview=True)
    return True


async def on_new_links_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """sln:pick:<id> | sln:del:<i> | sln:ok | sln:keep | sln:x"""
    query = update.callback_query
    await query.answer()
    _, action, *rest = query.data.split(":")
    if action == "pick":
        await start_new_links(query.message, context, int(rest[0]), context.user_data.pop("slink_first", None),
                              edit=query.edit_message_text)
        return
    state = context.user_data.get("pending_slink")
    if action == "x":
        context.user_data.pop("pending_slink", None)
        await query.edit_message_text("Đã thôi, link giữ nguyên.")
        return
    r = db.get("sales", state["id"]) if state else None
    if not r:
        context.user_data.pop("pending_slink", None)
        await query.edit_message_text("Bảng này đã hết hiệu lực. Chat lại: sale link <tên món>")
        return
    if action == "del" and rest and rest[0].isdigit() and int(rest[0]) < len(state["urls"]):
        state["urls"].pop(int(rest[0]))
    elif action in ("ok", "keep") and state["urls"]:
        old = (r["link"] or "").split()
        urls = state["urls"] if action == "ok" else old + [u for u in state["urls"] if u not in old]
        db.update("sales", r["id"], "link", "\n".join(urls))
        context.user_data.pop("pending_slink", None)
        r = db.get("sales", r["id"])
        note = (f"🗑 Đã xóa {len(old)} link cũ, lưu {len(urls)} link mới." if action == "ok"
                else f"➕ Giữ link cũ, giờ có {len(urls)} link.")
        await query.edit_message_text(note + "\n\n" + fmt_record("sales", r, r["id"], r["created_at"]),
                                      parse_mode="HTML", disable_web_page_preview=True)
        return
    text, kb = new_links_panel(r, state["urls"])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


def make_set_active(value: int, cmd: str):
    """/hetsale <id> [id ...] hoặc /hetsale all (tương tự /consale)."""
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        status = "✅ còn sale" if value else "⛔ hết sale"
        if context.args and context.args[0].lower() == "all":
            n = db.set_all_sales_active(value)
            await update.message.reply_text(f"Đã chuyển {n} món → {status}")
            return
        ids = [int(a) for a in context.args if a.isdigit()]
        if not ids:
            await update.message.reply_text(f"Cú pháp: /{cmd} <id> [id ...]  hoặc  /{cmd} all")
            return
        lines = []
        for rec_id in ids:
            r = db.get("sales", rec_id)
            if not r:
                lines.append(f"#{rec_id}: không tìm thấy")
                continue
            db.update("sales", rec_id, "active", value)
            lines.append(f"#{rec_id} {html.escape(r['ten_sp'])} → {status}")
        await update.message.reply_html("\n".join(lines))
    return handler


# watchdog polling: get_updates lỗi TimedOut liên tục (kết nối duy nhất bị kẹt sau lỗi mạng) thì
# python-telegram-bot chỉ log debug và thử lại mãi → bot im lặng, tin nhắn dồn trên Telegram.
# Quá POLL_STALE_SECONDS không lấy được tin → tạo lại kết nối; quá POLL_EXIT_SECONDS → thoát cho Docker
# khởi động lại (restart: unless-stopped).
POLL_STALE_SECONDS, POLL_EXIT_SECONDS = 120, 900
UPDATES_REQUEST = tg_net.FailoverRequest(1)


CODE_CHECK_SECONDS = 600  # mỗi 10 phút hỏi web đã đổi code module thuần chưa (remote.code_changed)


async def heartbeat_loop(application):
    """Ghi giờ bot còn sống vào meta mỗi phút (khi đang nhận tin được) → /api/health và healthcheck Docker
    đọc. Kèm watchdog polling (xem POLL_STALE_SECONDS)."""
    last_reset = 0.0
    next_code_check = time.monotonic() + CODE_CHECK_SECONDS
    while True:
        try:
            now = time.monotonic()
            if now >= next_code_check:  # web đã đổi code module thuần → chạy lại để nạp code mới
                next_code_check = now + CODE_CHECK_SECONDS
                if remote.code_changed():
                    log.warning("Web đã đổi code (utils / sale_text / tracking …) – thoát để Docker chạy lại với code mới")
                    logging.shutdown()
                    os._exit(1)
            stale = now - UPDATES_REQUEST.last_ok
            if stale > POLL_EXIT_SECONDS:
                log.error("Không lấy được tin Telegram %d phút – thoát để Docker khởi động lại", stale // 60)
                logging.shutdown()
                os._exit(1)
            if stale > POLL_STALE_SECONDS:
                if now - last_reset > POLL_STALE_SECONDS:
                    log.warning("Không lấy được tin Telegram %ds (%s) – tạo lại kết nối",
                                stale, tg_net.route_name())
                    last_reset = now
                    await UPDATES_REQUEST.reset()
            elif application.updater and application.updater.running:
                db.set_meta(db.HEARTBEAT_KEY, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:  # noqa: BLE001 – không để lỗi ghi DB / reset làm chết vòng lặp
            log.warning("Heartbeat / watchdog lỗi: %s", e)
        await asyncio.sleep(db.HEARTBEAT_SECONDS)


def main():
    db.SOURCE = "bot"
    db.init_db()
    if not ALLOWED_USER_IDS:
        log.warning("ALLOWED_USER_IDS trống – không ai dùng được bot. Thêm user ID admin vào .env.")
    async def post_init(application):
        global BOT_USERNAME
        BOT_USERNAME = (await application.bot.get_me()).username or ""
        application.bot_data["watch_task"] = asyncio.create_task(shipment_watch_loop(application))
        application.bot_data["heartbeat_task"] = asyncio.create_task(heartbeat_loop(application))
        if ALERT_BOT_TOKEN:
            application.bot_data["alert_task"] = asyncio.create_task(alert_bot_loop(application))

    async def post_shutdown(application):
        for key in ("watch_task", "alert_task", "heartbeat_task"):
            if task := application.bot_data.get(key):
                task.cancel()

    app = (ApplicationBuilder().token(BOT_TOKEN)
           .request(tg_net.FailoverRequest(256)).get_updates_request(UPDATES_REQUEST)
           .post_init(post_init).post_shutdown(post_shutdown).build())

    app.add_handler(TypeHandler(Update, check_access), group=-1)

    app.add_handler(ConversationHandler(
        entry_points=[
            CommandHandler("them", start_add_order),
            CommandHandler("themsale", start_add_sale),
            CallbackQueryHandler(start_add_shop, pattern=r"^saleshop:\d+$"),
        ],
        states={
            ASKING: [
                CommandHandler("lai", go_back),
                CallbackQueryHandler(on_pick, pattern=r"^pick:"),
                CallbackQueryHandler(on_links_done, pattern=r"^linksdone$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_text),
                MessageHandler(filters.PHOTO, on_step_photo),
            ],
            CONFIRM: [CallbackQueryHandler(on_confirm, pattern=r"^(save|cancel)$")],
        },
        fallbacks=[CommandHandler("huy", cancel)],
    ))
    # giá trị gõ tay cho /sua (chỉ chạy khi không ở trong hội thoại thêm mới)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_edit_value))
    app.add_handler(MessageHandler(filters.PHOTO, on_qr_photo))
    app.add_handler(CommandHandler("anhsp", cmd_product_images))
    app.add_handler(CommandHandler("xoaanhsp", cmd_delete_product_images))
    app.add_handler(CommandHandler("xong", cmd_product_images_done))
    app.add_handler(CommandHandler("ghep", cmd_merge))
    app.add_handler(MessageHandler(filters.Document.IMAGE, on_merge_document))
    app.add_handler(CallbackQueryHandler(on_merge_button, pattern=r"^ghep:(ngang|doc)$"))
    app.add_handler(CommandHandler("qr", cmd_qr))
    app.add_handler(CommandHandler("tracuu", cmd_track))
    app.add_handler(CommandHandler("kiemtravc", cmd_check_shipments))
    app.add_handler(CommandHandler("danggiao", cmd_delivering, block=False))  # tra lại lâu, không chặn lệnh khác
    app.add_handler(CommandHandler("diachi", cmd_addresses))
    app.add_handler(CommandHandler("themdiachi", cmd_add_address))
    app.add_handler(CommandHandler("suadiachi", make_edit("addresses", "suadiachi", default_field="noi_dung")))
    app.add_handler(CommandHandler("xoadiachi", make_delete("addresses", "xoadiachi")))
    app.add_handler(CallbackQueryHandler(on_track_refresh, pattern=r"^trk:"))
    app.add_handler(CommandHandler("alert", cmd_alert))
    app.add_handler(CommandHandler("themalert", cmd_add_alert))
    app.add_handler(CommandHandler("xoaalert", cmd_remove_alert))
    app.add_handler(CallbackQueryHandler(on_alert_button, pattern=r"^alert:(ok|no|del):"))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern=r"^noop$"))
    app.add_handler(CommandHandler("xoaqr", cmd_delete_qr))
    app.add_handler(CallbackQueryHandler(on_qr_button, pattern=r"^qr(new|paid|del|show):"))
    app.add_handler(CommandHandler(["start", "help"], cmd_start))

    # đơn hàng
    app.add_handler(CommandHandler("ds", make_list()))
    for cmd, st in STATUS_LIST_CMDS.items():
        app.add_handler(CommandHandler(cmd, make_list(st)))
    app.add_handler(CommandHandler("xem", make_view("orders", "xem")))
    app.add_handler(CommandHandler("tim", cmd_search))
    app.add_handler(CommandHandler("dsdiachi", cmd_orders_by_address))
    app.add_handler(CommandHandler("khach", cmd_customer))
    app.add_handler(CommandHandler(["dangdat", "dadat"], make_status_list(
        [db.STATUS_ORDERED], "⏳ <b>ĐƠN ĐÃ ĐẶT</b> (chưa nhận hàng)", "dangdat",
        "Nhận hàng chưa trả công: /danhan &lt;id&gt; · nhận hàng + trả công: /thanhtoan &lt;id&gt;")))
    app.add_handler(CommandHandler("chuaban", make_status_list(
        list(db.UNSOLD_STATUSES), "📦 <b>ĐƠN CHƯA BÁN</b>", "chuaban",
        "Bán được: /ban &lt;id&gt; &lt;giá shop thu&gt;")))
    app.add_handler(CommandHandler("danhan", make_set_status(db.STATUS_RECEIVED, "danhan")))
    app.add_handler(CommandHandler("thanhtoan", make_set_status(db.STATUS_PAID, "thanhtoan")))
    app.add_handler(CommandHandler("ban", cmd_sell))
    app.add_handler(CommandHandler("tach", cmd_split))
    app.add_handler(CallbackQueryHandler(on_done_button, pattern=r"^done:(ok|price):\d+$"))
    app.add_handler(CallbackQueryHandler(on_cancel_button, pattern=r"^vccancel:(ok|keep):\d+$"))
    app.add_handler(CommandHandler("huydon", make_set_status(db.STATUS_CANCELED, "huydon")))
    app.add_handler(CommandHandler("datlai", make_set_status(db.STATUS_ORDERED, "datlai")))
    app.add_handler(CommandHandler("sua", make_edit("orders", "sua")))
    app.add_handler(CommandHandler("xoa", make_delete("orders", "xoa")))
    app.add_handler(CommandHandler("thongke", cmd_stats))
    app.add_handler(CommandHandler("traship", cmd_traship))
    app.add_handler(CommandHandler("xuat", make_export("orders", "don_hang")))

    # đơn vị vận chuyển
    app.add_handler(CallbackQueryHandler(on_stats_button, pattern=r"^stats:\d{4}-\d{2}$"))
    app.add_handler(CallbackQueryHandler(on_stats_open, pattern=r"^statsopen:\d{4}-\d{2}$"))
    app.add_handler(CommandHandler("nguoidat", cmd_people))
    app.add_handler(CommandHandler("suand", make_edit("nguoi_dat", "suand", db.find_person, "ten")))
    app.add_handler(CommandHandler("xoand", cmd_delete_person))
    app.add_handler(CallbackQueryHandler(on_person_button, pattern=r"^nd:(show|new|del):\d+$"))
    app.add_handler(CommandHandler("sdt", cmd_phones))
    app.add_handler(CommandHandler("themsdt", cmd_add_phone))
    app.add_handler(CommandHandler("suasdt", make_edit("phones", "suasdt", db.get_phone, "ghi_chu")))
    app.add_handler(CommandHandler("xoasdt", cmd_delete_phone))
    app.add_handler(CallbackQueryHandler(on_phone_button, pattern=r"^sdt:"))
    app.add_handler(CallbackQueryHandler(on_phone_used, pattern=r"^sdt(use|mine):[01]:\d+$"))
    app.add_handler(CommandHandler("dasudung", make_phone_used(True, "dasudung")))
    app.add_handler(CommandHandler("chuasudung", make_phone_used(False, "chuasudung")))
    app.add_handler(CommandHandler("dvvc", cmd_carriers))
    app.add_handler(CommandHandler("sanpham", cmd_products))
    app.add_handler(CommandHandler("themsp", cmd_add_product))
    app.add_handler(CommandHandler("giasp", cmd_product_price))
    app.add_handler(CommandHandler("suasp", make_edit("products", "suasp", default_field="ten")))
    app.add_handler(CommandHandler("xoasp", cmd_delete_product))
    app.add_handler(CommandHandler("themdvvc", cmd_add_carrier))
    app.add_handler(CommandHandler("suadvvc", make_edit("carriers", "suadvvc", default_field="ten")))
    app.add_handler(CommandHandler("xoadvvc", cmd_delete_carrier))

    # săn sale
    app.add_handler(CommandHandler("sale", cmd_sale))
    app.add_handler(CommandHandler("saleall", cmd_sale_all))
    app.add_handler(CommandHandler("nhomsale", cmd_sale_group))
    app.add_handler(CommandHandler("thuongsale", cmd_bonus))
    app.add_handler(CommandHandler("dssale", cmd_sale_admin))
    app.add_handler(CommandHandler("hetsale", make_set_active(0, "hetsale")))
    app.add_handler(CommandHandler("consale", make_set_active(1, "consale")))
    app.add_handler(CommandHandler("xemsale", make_view("sales", "xemsale")))
    app.add_handler(CommandHandler("suasale", make_edit("sales", "suasale")))
    app.add_handler(CommandHandler("xoasale", make_delete("sales", "xoasale")))
    app.add_handler(CommandHandler("xuatsale", make_export("sales", "san_sale")))

    app.add_handler(CallbackQueryHandler(on_delete, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(on_edit_pick, pattern=r"^edit:"))
    app.add_handler(CallbackQueryHandler(on_edit_field, pattern=r"^ef:"))
    app.add_handler(CallbackQueryHandler(on_product_images_button, pattern=r"^pimg:(sales|products):\d+$"))
    app.add_handler(CallbackQueryHandler(on_sale_links_button, pattern=r"^sl:"))
    app.add_handler(CallbackQueryHandler(on_new_links_button, pattern=r"^sln:"))
    app.add_handler(CommandHandler("huy", cancel))
    app.add_handler(CommandHandler("clear", cmd_clear))

    log.info("Bot đang chạy...")
    app.run_polling()


if __name__ == "__main__":
    main()
