# -*- coding: utf-8 -*-
"""
生成 6 种场景的测试图，用于验证/回归 cutout.py 的自适应抠图能力。
全部为程序合成的样例，不含任何真实签名。

用法:
    python scripts/gen_cases.py [输出目录]        # 默认 ./examples/generated
"""
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

FONT_CANDIDATES = [
    "C:/Windows/Fonts/STKAITI.TTF", "C:/Windows/Fonts/simkai.ttf",
    "/System/Library/Fonts/Supplemental/Kaiti.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/arphic/ukai.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def load_font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    try:                                    # Pillow >= 10.1 支持给内置字体指定字号
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def paper(w, h, base=(255, 255, 255), vignette=0.12, warm=0):
    """带暗角的纸面，模拟手机拍摄的不均匀打光"""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
    shade = 1 - vignette * np.clip(r - 0.35, 0, 2)
    img = np.dstack([shade * c for c in base])
    if warm:
        img[..., 2] -= warm
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))


def glyph(char="9", size=520):
    """把字符渲染成一张透明底的字形图，当作"手写内容"的替身"""
    font = load_font(size)
    tmp = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(tmp).text((size // 2, size // 2), char, font=font, fill=255)
    bbox = tmp.getbbox()
    box = tmp.crop(bbox).resize((int((bbox[2] - bbox[0]) * 0.9),
                                 int((bbox[3] - bbox[1]) * 0.9)), Image.LANCZOS)
    out = Image.new("RGBA", box.size, (0, 0, 0, 0))
    out.putalpha(box)
    return out


def paste_glyph(bg, g, xy, scale=1.0, rotate=0, color=(20, 20, 25), gauss=0):
    if color:
        arr = np.zeros((g.height, g.width, 4), np.uint8)
        arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3] = *color, np.asarray(g)[..., 3]
        g = Image.fromarray(arr, "RGBA")
    g = g.resize((max(1, int(g.width * scale)), max(1, int(g.height * scale))), Image.LANCZOS)
    if rotate:
        g = g.rotate(rotate, expand=True, resample=Image.BICUBIC)
    if gauss:
        g = g.filter(ImageFilter.GaussianBlur(gauss))
    bg.paste(g, xy, g)
    return bg


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join("examples", "generated")
    os.makedirs(outdir, exist_ok=True)
    g9 = glyph("9")

    # A 米色纸 + 旋转 + 轻微糊（干净扫描件感）
    a = paper(900, 1200, (250, 246, 236), vignette=0.10)
    paste_glyph(a, g9, (330, 380), 1.0, rotate=8, gauss=0.8)
    a.save(f"{outdir}/case_A_cream_rotated.png")

    # B 蓝笔 + 强暗角（手机翻拍感）
    b = paper(900, 1200, (255, 255, 255), vignette=0.30)
    paste_glyph(b, g9, (300, 400), 1.1, rotate=-6, color=(18, 46, 138), gauss=1.6)
    b.save(f"{outdir}/case_B_blue_strong_shadow.png")

    # C 橙色 + 重度模糊 + 小字（难例）
    c = paper(900, 1200, (255, 255, 255), vignette=0.18, warm=14)
    paste_glyph(c, g9, (420, 460), 0.55, rotate=14, color=(230, 110, 20), gauss=2.4)
    c.save(f"{outdir}/case_C_orange_blurry_small.png")

    # D 多个字（多笔画、多连通域、高对比）
    d = paper(1000, 700, (255, 255, 255), vignette=0.12)
    ImageDraw.Draw(d).text((90, 180), "同意", font=load_font(190), fill=(25, 25, 30))
    d.save(f"{outdir}/case_D_multi_char.png")

    # E 深色底 + 浅色字（需要自动反相）
    e = Image.new("RGB", (900, 1200), (26, 28, 34))
    paste_glyph(e, g9, (330, 400), 1.0, rotate=5, color=(246, 246, 240), gauss=1.2)
    e.save(f"{outdir}/case_E_dark_bg_light_ink.png")

    # F 浅铅笔（低对比、细线）
    f_img = paper(900, 1200, (252, 250, 245), vignette=0.14)
    paste_glyph(f_img, g9, (340, 400), 1.0, rotate=-10, color=(120, 118, 112), gauss=1.0)
    f_img.save(f"{outdir}/case_F_pencil_low_contrast.png")

    print("测试图输出目录:", outdir)
    for n in sorted(os.listdir(outdir)):
        print("  -", n)


if __name__ == "__main__":
    main()
