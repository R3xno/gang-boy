import io, math
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1280, 720
CX, CY = 420, 360
_bg = None


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def background():
    """'Who's that Pokémon?' style burst: blue/white rays on the left, red on the right."""
    global _bg
    if _bg is not None:
        return _bg.copy()
    img = Image.new("RGB", (W, H), (205, 45, 45))
    d = ImageDraw.Draw(img)
    n = 60
    for i in range(n):
        a1, a2 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
        col = (110, 200, 230) if i % 2 == 0 else (235, 248, 255)
        d.polygon([(CX, CY), (CX + 2500 * math.cos(a1), CY + 2500 * math.sin(a1)),
                   (CX + 2500 * math.cos(a2), CY + 2500 * math.sin(a2))], fill=col)
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).polygon([(860, 0), (W, 0), (W, H), (700, H)], fill=255)
    img.paste(Image.new("RGB", (W, H), (205, 45, 45)), (0, 0), mask.filter(ImageFilter.GaussianBlur(25)))
    glow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(glow).ellipse([CX - 300, CY - 300, CX + 300, CY + 300], fill=255)
    img.paste((255, 255, 255), (0, 0), glow.filter(ImageFilter.GaussianBlur(80)))
    ImageDraw.Draw(img).text((1010, 330), "?", font=_font(260), fill=(255, 205, 0),
                             stroke_width=10, stroke_fill=(40, 90, 170), anchor="mm")
    _bg = img
    return img.copy()


def _jpg(img, q=90):
    b = io.BytesIO()
    img.convert("RGB").save(b, "JPEG", quality=q)
    return b.getvalue()


def _scene(art_bytes, black):
    bg = background()
    art = Image.open(io.BytesIO(art_bytes)).convert("RGBA")
    art.thumbnail((560, 560))
    if black:
        blk = Image.new("RGBA", art.size, (0, 0, 0, 255))
        blk.putalpha(art.getchannel("A"))
        art = blk
    bg.paste(art, (CX - art.width // 2, CY - art.height // 2), art)
    return _jpg(bg)


def silhouette(art_bytes):
    return _scene(art_bytes, True)


def reveal(art_bytes):
    return _scene(art_bytes, False)


def card(art_bytes):
    """Pokémon on a clean white square (like the HeXa catch card)."""
    bg = Image.new("RGB", (800, 800), "white")
    art = Image.open(io.BytesIO(art_bytes)).convert("RGBA")
    art.thumbnail((760, 760))
    bg.paste(art, ((800 - art.width) // 2, (800 - art.height) // 2), art)
    return _jpg(bg)


def collage(items):
    """items: list of (art_bytes, label) -> one 2x2 image."""
    S = 400
    bg = Image.new("RGB", (S * 2, S * 2), "white")
    d = ImageDraw.Draw(bg)
    f = _font(26)
    for i, (b, label) in enumerate(items[:4]):
        x, y = (i % 2) * S, (i // 2) * S
        art = Image.open(io.BytesIO(b)).convert("RGBA")
        art.thumbnail((S - 40, S - 70))
        bg.paste(art, (x + (S - art.width) // 2, y + 10), art)
        d.text((x + S // 2, y + S - 28), label, font=f, fill=(30, 30, 30), anchor="mm")
    d.line([(S, 0), (S, 2 * S)], fill=(200, 200, 200), width=3)
    d.line([(0, S), (2 * S, S)], fill=(200, 200, 200), width=3)
    return _jpg(bg)
