# -*- coding: utf-8 -*-
"""
手写内容抠图（自适应版）—— 照片里的手写签名/数字/字迹/印章 → 透明底 PNG

设计原则：**参数不固化**。默认按每张图自身的墨色动态范围自动定阈值，
不同光照、不同笔色、不同清晰度都能自适应；需要微调时用命令行开关，不必改代码。

用法:
    python cutout.py <图片1> [图片2 ...] [-o 输出目录] [--name 名称]
                     [--lo 0.16] [--hi 0.55]      手动指定绝对阈值（覆盖自动）
                     [--ink auto|lum|chroma]      墨色提取方式
                     [--sharpen 140]             锐化强度（默认按模糊程度自适应）
                     [--scurve 2]                边缘 S 曲线次数（默认 2）
                     [--close] [--min-comp 0.02] [--pad 10]
                     [--colors black,blue,ink] [--height 1600]
                     [--no-preview] [--debug]

每张图输出:
    <名>_黑_透明底.png / _蓝_透明底.png / _红_透明底.png / _原墨_透明底.png
    <名>_白底.png（不认透明通道的工具用）
    <名>_预览.png（白纸 + 表单蓝底效果）
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# ---- 相对参数：作用在"已按本图动态范围归一化"之后，因此不依赖具体图片 ----
LO_REL, HI_REL = 0.18, 0.62      # 低于 LO 视为纸/阴影，高于 HI 视为纯墨
PAPER_PCT, INK_PCT = 50.0, 99.5  # 估计纸面 / 墨色上限的百分位
MIN_SPAN = 0.18                  # 最低对比度保护


# --------------------------------------------------------------------------
# 1. 光照归一化：除以自身的大半径模糊，消掉阴影与不均匀打光
# --------------------------------------------------------------------------
def normalize_illumination(ch):
    g = Image.fromarray((np.clip(ch, 0, 1) * 255).astype(np.uint8))
    bg = np.asarray(g.filter(ImageFilter.GaussianBlur(radius=max(g.size) / 12)),
                    dtype=np.float32) / 255.0
    bg = np.clip(bg, 0.05, 1.0)
    return 1.0 - np.clip(ch / bg, 0.0, 1.0)     # 1 = 墨，0 = 纸


def ink_signal(im, mode):
    """返回 (raw, 用的模式)。raw 越大表示内容越深/越饱和。"""
    lum = np.asarray(im.convert("L")).astype(np.float32) / 255.0
    if lum.mean() < 0.5:                        # 深底浅字：反相
        lum = 1.0 - lum
    raw_lum = normalize_illumination(lum)

    if mode == "lum":
        return raw_lum, "lum"

    chans = [normalize_illumination(np.asarray(im.getchannel(c)).astype(np.float32) / 255.0)
             for c in ("R", "G", "B")]
    raw_max = np.maximum(np.maximum(chans[0], chans[1]), chans[2])

    if mode == "chroma":
        return raw_max, "chroma"

    # auto：先看墨迹区是否有明显彩度，有就用"最深的单通道"（对红/蓝/黄笔都稳），否则用亮度
    flat = raw_lum.ravel()
    thr = np.percentile(flat, 99.0)
    sel = raw_lum >= max(thr, 1e-3)
    if sel.sum() > 50:
        rgb = np.asarray(im).astype(np.float32) / 255.0
        mx, mn = rgb.max(2), rgb.min(2)
        sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0)
        sat_ink = float(sat[sel].mean())
    else:
        sat_ink = 0.0
    return (raw_max, "chroma") if sat_ink > 0.25 else (raw_lum, "lum")


# --------------------------------------------------------------------------
# 2. 自动阈值：按本图自身动态范围归一化 + 相对曲线（这就是"不固化"的关键）
# --------------------------------------------------------------------------
def auto_levels(raw):
    v = raw.ravel()
    paper = float(np.percentile(v, PAPER_PCT))
    ink = float(np.percentile(v, INK_PCT))
    if ink - paper < MIN_SPAN:                   # 对比度太低，给个保底跨度
        ink = paper + MIN_SPAN
    u = np.clip((raw - paper) / (ink - paper), 0, 1)
    return u, paper, ink


def blur_score(u):
    """用拉普拉斯方差粗估清晰度：越小越糊。只在有内容的区域统计。"""
    sel = u > 0.3
    if sel.sum() < 100:
        return 999.0
    g = u.astype(np.float32)
    lap = -4 * g + np.roll(g, 1, 0) + np.roll(g, -1, 0) + np.roll(g, 1, 1) + np.roll(g, -1, 1)
    return float(lap[2:-2, 2:-2][sel[2:-2, 2:-2]].var() * 10000)


def build_alpha(path, args):
    im = Image.open(path).convert("RGB")
    raw, mode = ink_signal(im, args.ink)

    if args.lo is not None or args.hi is not None:      # 手动绝对阈值，覆盖自动
        lo = 0.16 if args.lo is None else args.lo
        hi = 0.55 if args.hi is None else args.hi
        u, paper, ink = np.clip(raw, 0, 1), None, None
        src = "manual"
    else:
        u, paper, ink = auto_levels(raw)
        lo, hi = LO_REL, HI_REL
        src = "auto"

    a = np.clip((u - lo) / max(hi - lo, 1e-6), 0, 1)

    pct = args.sharpen
    if pct is None:                                     # 按模糊程度自适应锐化
        bs = blur_score(u)
        pct = 180 if bs < 40 else (140 if bs < 120 else 95)
        bs_note = bs
    else:
        bs_note = None
    a = np.asarray(Image.fromarray((a * 255).astype(np.uint8)).filter(
        ImageFilter.UnsharpMask(radius=2, percent=pct, threshold=2)), dtype=np.float32) / 255.0

    for _ in range(max(0, args.scurve)):                # S 曲线：去灰晕、边缘利落
        a = np.clip(a * a * (3 - 2 * a), 0, 1)

    if args.close:                                      # 桥接断笔（模糊/浅笔迹有用）
        a = np.asarray(Image.fromarray((a * 255).astype(np.uint8))
                       .filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3)),
                       dtype=np.float32) / 255.0

    # 去掉孤立噪点：只保留相对最大块足够大的连通域（多笔画/多数字天然支持）
    mask = (a > 0.30).astype(np.uint8)
    n_comp, dropped = 0, 0
    if mask.any():
        keep = _major_components(mask, args.min_comp)
        n_comp = int(keep[1])
        dropped = int(keep[2])
        km = keep[0]
        if not km.all():
            km = np.asarray(Image.fromarray((km * 255).astype(np.uint8))
                            .filter(ImageFilter.MaxFilter(5)), dtype=np.float32) / 255.0 > 0.5
        a = a * km

    ys, xs = np.where(a > 0.22)
    if len(ys) == 0:
        raise SystemExit("没找到内容：%s（图可能太淡、全空，或需要 --lo/--hi 手动指定）" % path)
    t, b = max(0, ys.min() - args.pad), ys.max() + args.pad
    l, r = max(0, xs.min() - args.pad), xs.max() + args.pad
    alpha = a[t:b, l:r]

    rgb = np.asarray(im).astype(np.float32)
    core = alpha > 0.85
    ink_rgb = tuple(int(v) for v in rgb[t:b, l:r][core].mean(axis=0)) if core.any() else (17, 17, 17)

    info = dict(mode=mode, src=src, lo=lo, hi=hi, paper=paper, ink=ink,
                sharpen=pct, blur=bs_note, comps=n_comp, dropped=dropped,
                coverage=float((alpha > 0.5).mean() * 100), size=alpha.shape[::-1], ink_rgb=ink_rgb)
    return alpha, info, u


def _major_components(mask, min_ratio):
    """保留面积 >= max(40, 最大块*min_ratio) 的连通域。有 scipy 用 scipy，否则手写 BFS。"""
    try:
        from scipy import ndimage
        lab, n = ndimage.label(mask, structure=np.ones((3, 3)))
        if n <= 1:
            return np.ones_like(mask, bool), n, 0
        sizes = ndimage.sum(mask, lab, range(1, n + 1))
        th = max(40.0, sizes.max() * min_ratio)
        ids = [i + 1 for i, s in enumerate(sizes) if s >= th]
        return np.isin(lab, ids), n, n - len(ids)
    except ImportError:
        from collections import deque
        H, W = mask.shape
        lab = np.zeros((H, W), np.int32)
        cur, comps = 0, []
        for y0 in range(H):
            for x0 in range(W):
                if mask[y0, x0] and lab[y0, x0] == 0:
                    cur += 1
                    q = deque([(y0, x0)]); lab[y0, x0] = cur; size = 0
                    while q:
                        y, x = q.popleft(); size += 1
                        for dy in (-1, 0, 1):
                            for dx in (-1, 0, 1):
                                yy, xx = y + dy, x + dx
                                if 0 <= yy < H and 0 <= xx < W and mask[yy, xx] and lab[yy, xx] == 0:
                                    lab[yy, xx] = cur; q.append((yy, xx))
                    comps.append((size, cur))
        comps.sort(reverse=True)
        if len(comps) <= 1:
            return np.ones_like(mask, bool), len(comps), 0
        th = max(40.0, comps[0][0] * min_ratio)
        ids = [c for s, c in comps if s >= th]
        return np.isin(lab, ids), len(comps), len(comps) - len(ids)


# --------------------------------------------------------------------------
# 3. 输出
# --------------------------------------------------------------------------
COLORS = {"black": (17, 17, 17), "blue": (18, 46, 138), "red": (170, 24, 32),
          "white": (255, 255, 255)}


def save_png(alpha, color, path, height):
    h, w = alpha.shape
    rgba = np.zeros((h, w, 4), np.uint8)
    rgba[..., 0], rgba[..., 1], rgba[..., 2] = color
    rgba[..., 3] = (np.clip(alpha, 0, 1) * 255).astype(np.uint8)
    img = Image.fromarray(rgba, "RGBA")
    if height and img.height < height:
        s = height / img.height
        img = img.resize((int(img.width * s), height), Image.LANCZOS)
    img.save(path)
    return img


def make_preview(alpha, path):
    h, w = alpha.shape
    pw, ph = 1000, 620
    prev = Image.new("RGB", (pw, ph), (255, 255, 255))
    d = ImageDraw.Draw(prev)
    d.rectangle([40, 40, pw - 40, ph - 40], outline=(228, 228, 228))
    d.rectangle([70, 120, pw - 70, 300], fill=(238, 243, 252), outline=(190, 200, 220))
    d.rectangle([70, 380, pw - 70, 540], fill=(245, 245, 245), outline=(210, 210, 210))
    sig = Image.fromarray(np.dstack([np.zeros((h, w, 3), np.uint8) + 17,
                                     (alpha * 255).astype(np.uint8)]), "RGBA")
    sh = 150
    sig = sig.resize((max(1, int(w * sh / h)), sh), Image.LANCZOS)
    prev.paste(sig, (100, 145), sig)
    prev.paste(sig, (100, 400), sig)
    prev.save(path)


def save_debug(u, alpha, info, path):
    """诊断图：看阈值落在哪、笔迹是否被吃/被糊。抠坏了发这个给我即可。"""
    W, H = 900, 300
    out = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(out)
    hist, edges = np.histogram(u.ravel(), bins=180, range=(0, 1))
    hist = hist / max(hist.max(), 1)
    for i, v in enumerate(hist):
        x0 = int(i * W / 180)
        x1 = int((i + 1) * W / 180)
        d.rectangle([x0, int(H * 0.45 - v * (H * 0.4)), x1, int(H * 0.45)], fill=(170, 180, 200))
    for val, col, tag in ((info["lo"], (30, 120, 40), "lo %.2f" % info["lo"]),
                          (info["hi"], (200, 60, 30), "hi %.2f" % info["hi"])):
        x = int(val * W)
        d.line([x, 20, x, int(H * 0.45)], fill=col, width=3)
        d.text((min(x + 4, W - 70), 6), tag, fill=col)
    h, w = alpha.shape
    s = (H * 0.5) / h
    th = Image.fromarray((alpha * 255).astype(np.uint8), "L").resize((max(1, int(w * s)), int(H * 0.5)), Image.LANCZOS)
    out.paste(th.convert("RGB"), (10, int(H * 0.47)))
    d.text((max(w * s * 0.6, 120), int(H * 0.5) + 5),
           "mode=%s src=%s sharpen=%s blur=%s comps=%s dropped=%s cov=%.1f%%" % (
               info["mode"], info["src"], info["sharpen"],
               ("%.0f" % info["blur"]) if info["blur"] else "-",
               info["comps"], info["dropped"], info["coverage"]), fill=(60, 60, 60))
    out.save(path)


def main():
    ap = argparse.ArgumentParser(description="手写签名/字迹抠图 → 透明底 PNG（自适应阈值）")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--outdir", default=None)
    ap.add_argument("--name", default=None, help="输出文件名前缀（多图时忽略，用各自文件名）")
    ap.add_argument("--lo", type=float, default=None, help="手动下限阈值（绝对，覆盖自动）")
    ap.add_argument("--hi", type=float, default=None, help="手动上限阈值（绝对，覆盖自动）")
    ap.add_argument("--ink", choices=["auto", "lum", "chroma"], default="auto")
    ap.add_argument("--sharpen", type=float, default=None, help="锐化强度，默认按模糊程度自适应")
    ap.add_argument("--scurve", type=int, default=2, help="S 曲线次数，默认 2")
    ap.add_argument("--close", action="store_true", help="桥接断笔")
    ap.add_argument("--min-comp", type=float, default=0.02, help="保留连通域的最小面积比")
    ap.add_argument("--pad", type=int, default=10)
    ap.add_argument("--height", type=int, default=1600)
    ap.add_argument("--colors", default="black,blue,ink")
    ap.add_argument("--no-preview", action="store_true")
    ap.add_argument("--debug", action="store_true", help="额外输出诊断图")
    args = ap.parse_args()

    multi = len(args.inputs) > 1
    for src in args.inputs:
        outdir = args.outdir or os.path.join(os.path.dirname(os.path.abspath(src)), "cutout")
        os.makedirs(outdir, exist_ok=True)
        name = os.path.splitext(os.path.basename(src))[0] if multi else (args.name or
                                                                        os.path.splitext(os.path.basename(src))[0])
        alpha, info, u = build_alpha(src, args)

        for c in [x.strip() for x in args.colors.split(",") if x.strip()]:
            rgb = info["ink_rgb"] if c == "ink" else COLORS.get(c)
            if rgb is None:
                print("  跳过未知颜色:", c); continue
            save_png(alpha, rgb, os.path.join(outdir, "%s_%s_透明底.png" % (name, c)), args.height)

        h, w = alpha.shape
        bgw = np.ones((h, w, 3), np.uint8) * 255
        al = alpha[..., None]
        Image.fromarray((bgw * (1 - al) + np.array([17, 17, 17], np.float32) * al).astype(np.uint8)) \
            .resize((w * 2, h * 2), Image.LANCZOS).save(os.path.join(outdir, "%s_白底.png" % name))
        if not args.no_preview:
            make_preview(alpha, os.path.join(outdir, "%s_预览.png" % name))
        if args.debug:
            save_debug(u, alpha, info, os.path.join(outdir, "%s_诊断.png" % name))

        print("[%s] %dx%d | 方式=%s%s | lo=%.2f hi=%.2f | 锐化=%.0f%s | 连通域=%d(丢弃%d) | 墨占比%.1f%% | 墨色%s"
              % (os.path.basename(src), info["size"][0], info["size"][1], info["mode"],
                 "(自动)" if info["src"] == "auto" else "(手动)",
                 info["lo"], info["hi"], info["sharpen"],
                 (" 模糊度%.0f" % info["blur"]) if info["blur"] else "",
                 info["comps"], info["dropped"], info["coverage"], info["ink_rgb"]))
    print("输出目录:", args.outdir or os.path.join(os.path.dirname(os.path.abspath(args.inputs[0])), "cutout"))


if __name__ == "__main__":
    main()
