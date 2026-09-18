#!/usr/bin/env python3
"""Contact sheet for picking a sprite candidate.

Runs every candidate under candidates/<name>/ through the prep_sprite.py
pipeline in memory and writes one self contained HTML page: per candidate the
raw generated image, the finished sprite at 6x nearest zoom on white, the four
shade histogram, and the exact prep_sprite.py command that promotes it into
examples/<name>/front.png or back.png.

Usage:
  python3 tools/picksheet.py --name ember
  open picksheet.html

Requires Pillow.
"""

import argparse
import base64
import html
import io
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit("error: Pillow is required (python3 -m pip install pillow)")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prep_sprite  # noqa: E402  (same directory, deliberate)

ZOOM = 6
RAW_PX = 200
SHADE_LABELS = ((0, "black"), (85, "dark"), (170, "light"), (255, "white"))


def b64_png(img, zoom=1):
    if zoom != 1:
        img = img.resize((img.width * zoom, img.height * zoom), Image.NEAREST)
    buf = io.BytesIO()
    img.convert("L" if img.mode == "L" else "RGB").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def b64_raw(path):
    img = Image.open(path).convert("RGB")
    if max(img.size) > 512:
        img.thumbnail((512, 512))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def histogram_html(hist, total):
    rows = []
    for value, label in SHADE_LABELS:
        n = hist.get(value, 0)
        pct = 100.0 * n / max(1, total)
        swatch = f"background:rgb({value},{value},{value})"
        rows.append(
            f"<tr><td><span class='sw' style='{swatch}'></span>{label} ({value})</td>"
            f"<td class='n'>{n}</td><td class='bar'>"
            f"<span style='width:{pct:.1f}%'></span></td></tr>")
    return "<table class='hist'>" + "".join(rows) + "</table>"


def card(path, view, size, name, args):
    try:
        sprite, info = prep_sprite.prepare(
            path, size=size, margin=args.margin, resample=args.resample,
            dither=args.dither, stretch=args.stretch)
    except (OSError, ValueError) as e:
        return (f"<figure class='bad'><figcaption><b>{html.escape(path.name)}</b>"
                f"<div class='warn'>{html.escape(str(e))}</div></figcaption></figure>")

    hist = info.get("histogram") or prep_sprite.shade_histogram(sprite)
    out_name = "front.png" if view == "front" else "back.png"
    flags = f" --size {size}" if view == "front" else ""
    if args.resample != "box":
        flags += f" --resample {args.resample}"
    if args.dither:
        flags += " --dither"
    if args.stretch:
        flags += " --stretch"
    if args.margin != 1:
        flags += f" --margin {args.margin}"
    if view == "front":
        cmd = (f"python3 tools/prep_sprite.py {path} "
               f"--out examples/{name}/front.png{flags}")
    else:
        cmd = (f"python3 tools/prep_sprite.py FRONT.png --out examples/{name}/front.png "
               f"--back {path} --back-out examples/{name}/back.png")
    warn = "".join(f"<div class='warn'>{html.escape(w)}</div>"
                   for w in info["warnings"])
    return (
        "<figure>"
        f"<div class='pair'>"
        f"<div><img class='raw' src='data:image/png;base64,{b64_raw(path)}'>"
        f"<div class='cap'>raw {info['source_size'][0]}x{info['source_size'][1]}</div></div>"
        f"<div><img class='sprite' src='data:image/png;base64,{b64_png(sprite, ZOOM)}'>"
        f"<div class='cap'>{size}x{size} at {ZOOM}x</div></div>"
        f"</div>"
        f"<figcaption><b>{html.escape(path.name)}</b>"
        f"<div class='meta'>background {html.escape(str(info['keyed']))}, "
        f"scale {info['scale']:.3f}, erode {info.get('erode', 0)}</div>"
        f"{histogram_html(hist, size * size)}{warn}"
        f"<code onclick='copyCmd(this)' title='click to copy'>{html.escape(cmd)}</code>"
        "</figcaption></figure>")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="candidates subfolder to show")
    ap.add_argument("--candidates", default="candidates", help="candidate root (default candidates)")
    ap.add_argument("--size", type=int, default=56, choices=prep_sprite.FRONT_SIZES,
                    help="front sprite edge (default 56)")
    ap.add_argument("--out", default="picksheet.html", help="output HTML (default picksheet.html)")
    ap.add_argument("--margin", type=int, default=1, help="prep margin (default 1)")
    ap.add_argument("--resample", choices=("box", "nearest"), default="box", help="prep resample filter")
    ap.add_argument("--dither", action="store_true", help="prep with ordered dither")
    ap.add_argument("--stretch", action="store_true", help="prep with contrast stretch")
    args = ap.parse_args()

    name = args.name.strip().lower()
    cdir = Path(args.candidates) / name
    if not cdir.is_dir():
        sys.exit(f"error: no candidates at {cdir} (run gen_candidates.py first)")

    sections = []
    shown = 0
    for view, size in (("front", args.size), ("back", prep_sprite.BACK_SIZE)):
        files = sorted(p for p in cdir.glob("*.png") if p.stem.startswith(view + "_"))
        if not files:
            continue
        cards = [card(p, view, size, name, args) for p in files]
        shown += len(cards)
        sections.append(
            f"<section><h2>{view} <small>{size}x{size}, {len(cards)} candidates</small></h2>"
            f"<div class='row'>{''.join(cards)}</div></section>")

    # Anything that does not follow the front_/back_ naming still gets shown,
    # treated as a front view, so hand dropped images work too.
    other = sorted(p for p in cdir.glob("*.png")
                   if not p.stem.startswith(("front_", "back_")))
    if other:
        cards = [card(p, "front", args.size, name, args) for p in other]
        shown += len(cards)
        sections.append(
            f"<section><h2>other <small>treated as front, {len(cards)} files</small></h2>"
            f"<div class='row'>{''.join(cards)}</div></section>")

    if not shown:
        sys.exit(f"error: no PNG candidates in {cdir}")

    out = Path(args.out)
    out.write_text(HTML_SHELL.replace("%%NAME%%", html.escape(name))
                   .replace("%%SECTIONS%%", "\n".join(sections)))
    print(f"wrote {out} ({shown} candidates)")
    print("open it, pick a winner, run the command under it.")


HTML_SHELL = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>%%NAME%% sprite candidates</title>
<style>
  :root { color-scheme: light; }
  body { margin: 0; padding: 24px; background: #f4f2ee; color: #1d1b18;
         font: 14px/1.5 -apple-system, "Helvetica Neue", Arial, sans-serif; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .lede { color: #6b665e; margin: 0 0 24px; }
  h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .08em;
       border-bottom: 1px solid #d8d3c9; padding-bottom: 6px; margin: 32px 0 16px; }
  h2 small { text-transform: none; letter-spacing: 0; color: #6b665e; font-weight: 400; }
  .row { display: flex; flex-wrap: wrap; gap: 16px; }
  figure { margin: 0; background: #fff; border: 1px solid #ddd8ce; border-radius: 6px;
           padding: 12px; width: 420px; }
  figure.bad { background: #fff4f4; border-color: #e0b4b4; }
  .pair { display: flex; gap: 12px; align-items: flex-start; }
  .pair > div { text-align: center; }
  img.raw { width: 200px; height: auto; display: block; background: #fff;
            border: 1px solid #eee; }
  img.sprite { display: block; background: #fff; border: 1px solid #eee;
               image-rendering: pixelated; }
  .cap { font-size: 11px; color: #8a847a; margin-top: 4px; }
  figcaption { margin-top: 10px; }
  .meta { font-size: 12px; color: #6b665e; margin: 2px 0 8px; }
  table.hist { border-collapse: collapse; width: 100%; font-size: 11px; color: #4a453e; }
  table.hist td { padding: 1px 4px 1px 0; }
  td.n { text-align: right; width: 48px; font-variant-numeric: tabular-nums; }
  td.bar { width: 45%; }
  td.bar span { display: block; height: 7px; background: #9aa7b8; }
  .sw { display: inline-block; width: 9px; height: 9px; margin-right: 5px;
        border: 1px solid #bbb; vertical-align: middle; }
  .warn { font-size: 12px; color: #9c4a1a; margin-top: 6px; }
  code { display: block; margin-top: 10px; padding: 7px 8px; background: #f0eee9;
         border: 1px solid #ddd8ce; border-radius: 4px; font-size: 11px;
         word-break: break-all; cursor: pointer; }
  code:hover { background: #e7e4dd; }
  @media (max-width: 520px) { figure { width: 100%; } .pair { flex-direction: column; } }
</style></head><body>
<h1>%%NAME%% sprite candidates</h1>
<p class="lede">Left: the raw generated image. Right: the finished pokered sprite at 6x
nearest zoom, exactly the pixels that go into the ROM. Click a command to copy it.</p>
%%SECTIONS%%
<script>
function copyCmd(el) {
  navigator.clipboard.writeText(el.textContent).then(function () {
    var old = el.style.background;
    el.style.background = '#cfe8cf';
    setTimeout(function () { el.style.background = old; }, 400);
  });
}
</script>
</body></html>
"""


if __name__ == "__main__":
    main()
