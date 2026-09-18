#!/usr/bin/env python3
"""Any image -> a pokered sprite PNG.

Takes whatever you have (a Gemini candidate on flat magenta, a PNG exported
from Aseprite, a photo of a drawing on white paper) and produces the exact
format pokered wants:

  front: square, 40x40 or 48x48 or 56x56, mode L, only the values 0/85/170/255
  back:  32x32, same format

Pipeline: key out the flat background (magenta first, otherwise the color
sampled from the four corners), autocrop to the content, fit it into the square
with a margin, downsample, then quantize every pixel to the nearest of the four
pokered tones (background becomes 255, which is what a real pokered sprite uses
for its background, see gfx/pokemon/front/charmander.png).

Usage:
  python3 tools/prep_sprite.py cand.png --out examples/ember/front.png --size 56
  python3 tools/prep_sprite.py cand.png --out front.png \
      --back cand_back.png --back-out back.png
  python3 tools/prep_sprite.py cand.png --out front.png --resample nearest --dither

Requires Pillow: python3 -m pip install pillow
"""

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit("error: Pillow is required (python3 -m pip install pillow)")

# The four tones a pokered sprite PNG may contain, black to white.
TONES = (0, 85, 170, 255)
BACKGROUND_TONE = 255

FRONT_SIZES = (40, 48, 56)
BACK_SIZE = 32

# Terminal preview glyphs, darkest first, one character per pixel.
PREVIEW_CHARS = {0: "#", 85: "+", 170: ".", 255: " "}

# 4x4 ordered (Bayer) matrix, values 0..15, used only with --dither.
BAYER4 = (
    (0, 8, 2, 10),
    (12, 4, 14, 6),
    (3, 11, 1, 9),
    (15, 7, 13, 5),
)

RESAMPLERS = {"nearest": Image.NEAREST, "box": Image.BOX}


def _is_magenta(r, g, b):
    """Generation-background magenta (#FF00FF and anything close to it)."""
    return r > 150 and b > 150 and g < min(r, b) - 70


def _magenta_fraction(rgba):
    px = rgba.load()
    w, h = rgba.size
    hits = 0
    total = 0
    for y in range(0, h, max(1, h // 64)):
        for x in range(0, w, max(1, w // 64)):
            r, g, b, _ = px[x, y]
            total += 1
            if _is_magenta(r, g, b):
                hits += 1
    return hits / max(1, total)


def _corner_color(rgba):
    """Median-ish background color from the four corners (mean of them)."""
    w, h = rgba.size
    px = rgba.load()
    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    n = len(corners)
    return tuple(sum(c[i] for c in corners) // n for i in range(3))


def key_background(rgba, tolerance=40):
    """RGBA in, LA out: flat background keyed to alpha 0, artwork to gray.

    Magenta wins if the image actually has a magenta background, because that
    is the convention the generator is asked for and it never collides with a
    grayscale sprite. Otherwise the corner color is treated as the background,
    which covers white-background art and anything exported on a flat color.
    """
    w, h = rgba.size
    px = rgba.load()
    alpha = Image.new("L", (w, h), 255)
    ap = alpha.load()
    use_magenta = _magenta_fraction(rgba) >= 0.02
    bg = _corner_color(rgba)
    tol2 = tolerance * tolerance
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                ap[x, y] = 0
                continue
            if use_magenta:
                if _is_magenta(r, g, b):
                    ap[x, y] = 0
            else:
                d = (r - bg[0]) ** 2 + (g - bg[1]) ** 2 + (b - bg[2]) ** 2
                if d <= tol2:
                    ap[x, y] = 0
    return Image.merge("LA", (rgba.convert("L"), alpha)), use_magenta, bg


def erode_alpha(img_la, passes=1):
    """Shave `passes` one pixel rims off the keyed silhouette.

    Generated art has a blended fringe where the artwork meets the background;
    at large source sizes that fringe is many pixels wide and it survives the
    downsample as a halo. Eroding at source resolution costs nothing visible
    when the image is about to shrink several times over, which is why the
    default is scale dependent (see prepare()).
    """
    alpha = img_la.getchannel("A")
    w, h = alpha.size
    for _ in range(passes):
        src = alpha.load()
        out = alpha.copy()
        op = out.load()
        for y in range(h):
            for x in range(w):
                if src[x, y] == 0:
                    continue
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h and src[nx, ny] == 0:
                        op[x, y] = 0
                        break
        alpha = out
    return Image.merge("LA", (img_la.getchannel("L"), alpha))


def stretch_contrast(img_la):
    """Rescale the artwork's luminance to the full 0..255 range.

    Off by default. Useful when a candidate comes back washed out (all four of
    its tones sitting in the light half), which otherwise quantizes to two.
    """
    lum = img_la.getchannel("L")
    alpha = img_la.getchannel("A")
    lp, apx = lum.load(), alpha.load()
    w, h = lum.size
    vals = [lp[x, y] for y in range(h) for x in range(w) if apx[x, y] > 0]
    if not vals:
        return img_la
    lo, hi = min(vals), max(vals)
    if hi - lo < 8:
        return img_la
    scale = 255.0 / (hi - lo)
    lum = lum.point(lambda v: max(0, min(255, int(round((v - lo) * scale)))))
    return Image.merge("LA", (lum, alpha))


def fit_square(img_la, size, margin, resample, anchor="bottom"):
    """Scale the cropped artwork into a size x size box, leaving `margin` free.

    Never upscales, and the margin only applies to artwork that has to shrink
    anyway: art that already fits the box is placed at its own size, so a sprite
    that is already at the target resolution keeps its pixels intact even if it
    touches the frame edge (real pokered sprites often do).

    Horizontally the content is centered. Vertically the default is bottom
    aligned, which is what real Gen 1 sprites do: measured over all 153 front
    PNGs in pokered the mean padding is 1.5 left, 1.6 right, 2.2 top and 1.0
    bottom, so the creature stands on the bottom edge of its box.
    """
    w, h = img_la.size
    if w <= size and h <= size:
        scale = 1.0
    else:
        inner = max(1, size - 2 * margin)
        scale = min(inner / w, inner / h, 1.0)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    if (nw, nh) != (w, h):
        img_la = img_la.resize((nw, nh), resample)
    canvas = Image.new("LA", (size, size), (BACKGROUND_TONE, 0))
    # The margin is a scaling constraint, not a placement inset: once the
    # artwork fits, bottom anchoring puts it flush on the bottom edge.
    top = (size - nh) if anchor == "bottom" else (size - nh) // 2
    canvas.paste(img_la, ((size - nw) // 2, top))
    return canvas, scale


def quantize(img_la, dither=False):
    """LA in, mode L out with only the four pokered tones.

    Each artwork pixel goes to the nearest of 0/85/170/255, which is the same as
    slicing the range into even thirds between the tones; keyed background goes
    to 255. Nearest-tone mapping is exactly the identity on an image that is
    already quantized, so running this tool on a finished sprite is a no-op.
    """
    lum = img_la.getchannel("L")
    alpha = img_la.getchannel("A")
    w, h = lum.size
    lp, apx = lum.load(), alpha.load()
    out = Image.new("L", (w, h), BACKGROUND_TONE)
    op = out.load()
    step = 85.0
    for y in range(h):
        for x in range(w):
            if apx[x, y] == 0:
                continue
            v = lp[x, y]
            if dither:
                bias = (BAYER4[y % 4][x % 4] / 16.0 - 0.5) * step
                v = max(0, min(255, v + bias))
            op[x, y] = TONES[max(0, min(3, int(round(v / step))))]
    return out


def prepare(src, size=56, margin=1, resample="box", dither=False, stretch=False,
            erode=None, tolerance=40, passthrough=True, anchor="bottom"):
    """Run the whole pipeline. Returns (image, info).

    src         path, str or a PIL Image
    size        output square edge (56/48/40 front, 32 back)
    margin      pixels of background kept on every side (content is centered)
    resample    "box" or "nearest" (see main() for why box is the default)
    dither      4x4 ordered dither before quantizing (off: pixel art stays flat)
    stretch     rescale artwork luminance to full range before quantizing
    erode       alpha erosion passes, None picks by downscale factor
    tolerance   corner-color keying tolerance (euclidean, per channel 0..255)
    passthrough True returns an input that is already a conformant sprite of
                this size untouched, so the tool is idempotent
    anchor      "bottom" (Gen 1 convention) or "center" vertical placement

    image is a mode L PIL Image of size x size using only 0/85/170/255.
    info is a dict with the keying mode, scale, margin and any warnings.
    """
    img = src if isinstance(src, Image.Image) else Image.open(src)
    info = {"warnings": [], "source_size": img.size}

    if passthrough and is_conformant(img, size):
        info["keyed"] = "none (already a pokered sprite)"
        info["scale"] = 1.0
        return img.convert("L"), info

    rgba = img.convert("RGBA")
    keyed, used_magenta, bg = key_background(rgba, tolerance=tolerance)
    info["keyed"] = "magenta" if used_magenta else f"corner color rgb{bg}"
    if not used_magenta and max(bg) - min(bg) > 40:
        info["warnings"].append(
            f"background sampled from the corners is not neutral (rgb{bg}); "
            "if the corners are artwork, key it out by hand first")

    bbox = keyed.getchannel("A").getbbox()
    if bbox is None:
        raise ValueError("keying removed the whole image: the background color "
                         "matches the artwork, try --tolerance 10")
    content_w, content_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    inner = max(1, size - 2 * margin)
    if content_w <= size and content_h <= size:
        rough = 1.0
    else:
        rough = min(inner / content_w, inner / content_h, 1.0)
    passes = erode if erode is not None else (1 if rough < 0.5 else 0)
    if passes:
        keyed = erode_alpha(keyed, passes)
        bbox = keyed.getchannel("A").getbbox()
        if bbox is None:
            raise ValueError("erosion removed the whole image, try --erode 0")
    info["erode"] = passes

    cropped = keyed.crop(bbox)
    if stretch:
        cropped = stretch_contrast(cropped)
    fitted, scale = fit_square(cropped, size, margin, RESAMPLERS[resample], anchor)
    info["scale"] = scale
    if scale == 1.0 and max(cropped.size) < inner // 2:
        info["warnings"].append(
            f"artwork is only {cropped.size[0]}x{cropped.size[1]} and is not upscaled; "
            "it will sit small inside the sprite box")

    out = quantize(fitted, dither=dither)
    used = shade_histogram(out)
    # Count a tone as used only above half a percent of the sprite, so a
    # handful of stray pixels does not hide a washed out candidate.
    floor = 0.005 * size * size
    if sum(1 for t in TONES if used[t] > floor) < 3:
        info["warnings"].append(
            "fewer than 3 of the 4 tones are really used; the sprite will look "
            "flat (try --stretch, or ask for more contrast in the brief)")
    info["histogram"] = used
    return out, info


def is_conformant(img, size=None):
    """True if img is already a pokered sprite PNG (mode L, square, 4 tones)."""
    if img.mode != "L":
        return False
    w, h = img.size
    if w != h:
        return False
    if size is not None and w != size:
        return False
    if size is None and w not in FRONT_SIZES + (BACK_SIZE,):
        return False
    h = img.histogram()
    return all(n == 0 for v, n in enumerate(h[:256]) if v not in TONES)


def shade_histogram(img_l):
    """{0: n, 85: n, 170: n, 255: n} for a quantized mode L image."""
    h = img_l.convert("L").histogram()
    return {t: h[t] for t in TONES}


def text_preview(img_l):
    """One character per pixel: '#' black, '+' dark, '.' light, ' ' white."""
    w, h = img_l.size
    px = img_l.load()
    rows = []
    for y in range(h):
        rows.append("".join(PREVIEW_CHARS.get(px[x, y], "?") for x in range(w)))
    return "\n".join(rows)


def _run_one(src, out_path, size, args, label):
    img, info = prepare(
        src, size=size, margin=args.margin, resample=args.resample,
        dither=args.dither, stretch=args.stretch, erode=args.erode,
        tolerance=args.tolerance, passthrough=not args.no_passthrough,
        anchor=args.anchor)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
    hist = info.get("histogram") or shade_histogram(img)
    print(f"\n{label}: {out_path}  {size}x{size} mode L")
    print(f"  source {info['source_size'][0]}x{info['source_size'][1]}, "
          f"background {info['keyed']}, scale {info['scale']:.3f}, "
          f"erode {info.get('erode', 0)}")
    print("  shades  black %d  dark %d  light %d  white %d"
          % (hist[0], hist[85], hist[170], hist[255]))
    print(text_preview(img))
    for w in info["warnings"]:
        print(f"  WARNING: {w}")
    return img


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="any image: the front view candidate")
    ap.add_argument("--out", required=True, help="output front PNG path")
    ap.add_argument("--size", type=int, default=56, choices=FRONT_SIZES,
                    help="front sprite edge (default 56)")
    ap.add_argument("--back", help="second input image: the back view candidate")
    ap.add_argument("--back-out", help=f"output back PNG path ({BACK_SIZE}x{BACK_SIZE})")
    # box averages every source pixel that lands in an output pixel, so a clean
    # upscale of a pixel grid (what the generator is asked for) collapses back
    # to its original blocks instead of aliasing. nearest is there for input
    # that is already at or near the target resolution, where box would soften
    # the shade boundaries.
    ap.add_argument("--resample", choices=sorted(RESAMPLERS), default="box",
                    help="downsample filter (default box)")
    ap.add_argument("--dither", action="store_true",
                    help="4x4 ordered dither before quantizing (default off: "
                         "pixel art should stay flat)")
    ap.add_argument("--stretch", action="store_true",
                    help="rescale artwork luminance to the full range first")
    ap.add_argument("--anchor", choices=("bottom", "center"), default="bottom",
                    help="vertical placement in the box (default bottom, the "
                         "Gen 1 convention)")
    ap.add_argument("--margin", type=int, default=1,
                    help="background pixels kept on each side (default 1)")
    ap.add_argument("--erode", type=int, default=None,
                    help="alpha erosion passes after keying (default: 1 when "
                         "downscaling more than 2x, else 0)")
    ap.add_argument("--tolerance", type=int, default=40,
                    help="corner-color keying tolerance (default 40)")
    ap.add_argument("--no-passthrough", action="store_true",
                    help="re-run the pipeline even if the input is already a "
                         "conformant sprite of the target size")
    args = ap.parse_args()

    if args.back and not args.back_out:
        ap.error("--back needs --back-out")
    if args.back_out and not args.back:
        ap.error("--back-out needs --back")
    if args.margin < 0 or args.margin * 2 >= args.size:
        ap.error(f"--margin must be between 0 and {args.size // 2 - 1}")

    try:
        _run_one(args.input, args.out, args.size, args, "front")
        if args.back:
            _run_one(args.back, args.back_out, BACK_SIZE, args, "back")
    except (OSError, ValueError) as e:
        sys.exit(f"error: {e}")
    print("\nnext: point mon.json sprites.front/back at these files and run inject.py")


if __name__ == "__main__":
    main()
