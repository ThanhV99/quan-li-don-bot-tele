# Quản lí đơn – bot Telegram

Bot Telegram ghi đơn đặt hộ Shopee / TikTok: thêm / sửa đơn, trả công người đặt (QR), săn sale, thống kê, tra hành trình
vận đơn và báo khi hàng sắp giao. Bot **không có database và không có logic nghiệp vụ**: mọi dữ liệu và phép tính lấy
qua API của web service ([quan-li-don-webservice](https://github.com/ThanhV99/quan-li-don-webservice)).

Python 3.12 · python-telegram-bot v21 · Docker.

## Cài đặt

Cần web service chạy trước (cùng `SERVICE_TOKEN`).

```bash
cp env.example .env              # điền BOT_TOKEN, ALLOWED_USER_IDS, SERVICE_TOKEN (giống hệt bên web)…
docker network create quan-li-don   # 1 lần: mạng chung với web (bỏ qua nếu đã tạo)
docker compose up -d --build
docker logs -f quan-li-don-bot
```

Mặc định bot gọi web ở `http://quan-li-don-web:8000` (cùng máy, cùng mạng docker). Web ở máy khác: đặt `API_URL` trong `.env`.
Mỗi token bot chỉ được một nơi chạy (long polling).

Chạy trực tiếp (phát triển): `pipenv install && API_URL=http://127.0.0.1:8088 pipenv run python bot.py`.

## Cách gọi web

`remote.py` thay cho các module của web:
- `db`: mỗi lệnh đọc / ghi dữ liệu là một yêu cầu `POST {API_URL}/api/svc/db.<hàm>` kèm header `X-Service-Token`; dữ liệu mã hóa
  theo `rpc.py` (giống hệt bản bên web). Lệnh đọc được nhớ trong một lượt xử lý tin nhắn (ghi là xóa) cho đỡ gọi qua mạng.
- `utils`, `alert_rules`, `net`, `sale_text`, `tracking`: lúc khởi động bot **tải mã nguồn từ web** rồi chạy tại chỗ (định dạng
  tiền, đọc sale…), hằng số lấy theo giá trị bên web; tra vận đơn và các việc cần database vẫn chạy ở web. Code chỉ nằm ở repo
  web; web đổi code thì bot tự khởi động lại (trong ~10 phút) để nạp bản mới.

## File

| File | Vai trò |
|---|---|
| `bot.py` | lệnh bot, định dạng tin nhắn, vòng kiểm tra vận chuyển, gửi thông báo |
| `remote.py` | gọi web API thay cho các module của web |
| `rpc.py` | mã hóa dữ liệu gửi qua lại (bản giống hệt bên web) |
| `tg_net.py` | kết nối Telegram thẳng ↔ proxy (`PROXY_URL`) |
| `imgmerge.py` | ghép ảnh `/ghep` |

## Lệnh hay dùng

`/them` thêm đơn · `/ds`, `/dsdadat` danh sách · `/xem`, `/sua`, `/xoa <id>` · `/ban <id> <giá>` · `/thongke [tháng]` ·
`/sale`, `/saleall` săn sale · `/tracuu <mã>`, `/danggiao` vận chuyển · `/help full` đủ mọi lệnh.
