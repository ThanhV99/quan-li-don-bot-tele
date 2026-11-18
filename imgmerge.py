"""Ghép nhiều ảnh thành 1 (lệnh /ghep của bot)."""
import io

GAP = 12             # khoảng trắng giữa các ảnh (px)
MAX_SIDE = 4000      # chiều cao (ngang) / rộng (dọc) chung tối đa
MAX_PIXELS = 40_000_000  # ảnh ghép quá lớn thì thu nhỏ cả ảnh (giữ bộ nhớ)


def auto_mode(sizes: list[tuple[int, int]]) -> str:
    """Ảnh dọc (chụp màn hình điện thoại) → đặt cạnh nhau (ngang); ảnh ngang → xếp chồng (dọc)."""
    portrait = sum(1 for w, h in sizes if h >= w)
    return "ngang" if portrait * 2 >= len(sizes) else "doc"


def merge(images: list[bytes], mode: str | None = None) -> tuple[bytes, str, tuple[int, int]]:
    """Ghép ảnh theo thứ tự gửi. mode: "ngang" (cạnh nhau) / "doc" (chồng lên nhau) / None = tự chọn.
    Trả về (JPEG bytes, mode đã dùng, (rộng, cao))."""
    from PIL import Image, ImageOps  # import muộn: bot vẫn chạy nếu image chưa có Pillow

    imgs = []
    for b in images:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(b)))
        if im.mode != "RGB":
            bg = Image.new("RGB", im.size, "white")
            im = im.convert("RGBA")
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        imgs.append(im)
    mode = mode or auto_mode([im.size for im in imgs])
    # cùng chiều cao / rộng = ảnh LỚN nhất (ảnh nhỏ phóng lên) để ảnh nét không bị thu nhỏ làm mờ chữ
    if mode == "ngang":
        side = min(max(im.height for im in imgs), MAX_SIDE)
        imgs = [im.resize((max(1, round(im.width * side / im.height)), side), Image.LANCZOS)
                if im.height != side else im for im in imgs]
        w, h = sum(im.width for im in imgs) + GAP * (len(imgs) - 1), side
    else:
        side = min(max(im.width for im in imgs), MAX_SIDE)
        imgs = [im.resize((side, max(1, round(im.height * side / im.width))), Image.LANCZOS)
                if im.width != side else im for im in imgs]
        w, h = side, sum(im.height for im in imgs) + GAP * (len(imgs) - 1)
    out = Image.new("RGB", (w, h), "white")
    pos = 0
    for im in imgs:
        out.paste(im, (pos, 0) if mode == "ngang" else (0, pos))
        pos += (im.width if mode == "ngang" else im.height) + GAP
    if w * h > MAX_PIXELS:
        k = (MAX_PIXELS / (w * h)) ** 0.5
        out = out.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
    buf = io.BytesIO()
    # chất lượng cao + không giảm mẫu màu (subsampling=0): chữ màu trên ảnh chuyển khoản không bị nhòe
    out.save(buf, "JPEG", quality=95, subsampling=0, optimize=True)
    return buf.getvalue(), mode, out.size
