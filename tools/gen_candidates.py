#!/usr/bin/env python3
"""Generate Pokemon Red/Blue style sprite candidates from a text brief.

Calls the Gemini image model with a locked style prompt (Game Boy four tone
sprite, black outline, flat magenta background) plus your one line brief, and
writes numbered candidates to candidates/<name>/front_<n>.png and back_<n>.png.
Nothing here touches the ROM: the winner goes through tools/prep_sprite.py.

Usage:
  export GEMINI_API_KEY=...
  python3 tools/gen_candidates.py --brief "a small fire lizard with a coal belly" \
      --name ember --count 6 --dry-run
  python3 tools/gen_candidates.py --brief "..." --name ember --count 6
  python3 tools/gen_candidates.py --brief "..." --name ember --count 4 --view back \
      --front-ref candidates/ember/front_3.png

Image generation has no free tier: the key's Google Cloud project needs billing
enabled. Model default is gemini-3.1-flash-image.

Stdlib only (urllib, json, base64). No pip installs.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "gemini-3.1-flash-image"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

# Locked style block. Gen 1 sprites are 56x56 (front) and 32x32 (back) four
# tone tiles; the model cannot draw at that resolution, so it is asked for a
# clean large upscale of a 56x56 grid and prep_sprite.py does the downsample.
STYLE_BLOCK = (
    "An authentic Game Boy Pokemon Red and Blue monster sprite, drawn on a 56 by 56 "
    "pixel grid and rendered large as a clean upscale of that grid: every pixel is a "
    "crisp square block of uniform color with hard edges, the block grid perfectly "
    "aligned to the frame. Use exactly four flat tones and nothing between them: "
    "white, light gray, dark gray and black. One pixel wide solid black outline around "
    "the whole creature and around its interior shapes. No anti aliasing, no blur, no "
    "gradients, no soft shading, no highlights, no glow, no drop shadow, no ground, no "
    "text, no signature, no border, no frame, no extra characters or objects. One "
    "creature only, filling most of the frame."
)

FRONT_VIEW = (
    "Front view: the creature faces the viewer at a three quarter angle, its whole body "
    "in frame, the pose readable as a battle sprite, the classic Gen 1 front sprite look."
)

BACK_VIEW = (
    "Back view: the same creature seen from directly behind, as the player sees their own "
    "Pokemon at the bottom of a Gen 1 battle screen. Zoomed in and cropped: only the head, "
    "back and upper body are in frame, the lower body runs off the bottom edge. Chunkier "
    "and simpler than the front view, because this sprite is only 32 by 32 pixels."
)

MAGENTA_BLOCK = (
    "The background is one flat, perfectly uniform solid magenta (#FF00FF) filling the "
    "whole frame, with no gradient, no vignette, no pattern, and no magenta anywhere in "
    "the creature itself, so the background can be keyed out."
)

FRONT_REF_NOTE = (
    "The attached image is the SAME creature seen from the front. Draw that exact creature "
    "from behind: same body shape, same markings, same tones, same style."
)

VIEWS = ("front", "back")

# Retry policy: NO_IMAGE is a transient empty answer, 429/5xx are rate limits.
NO_IMAGE_TRIES = 3
NO_IMAGE_WAIT = 5
HTTP_RETRIES = 3


class NoImageError(RuntimeError):
    """HTTP 200 with no image part, for example finishReason NO_IMAGE."""


def build_prompt(brief, view):
    subject = brief.strip()
    if subject and subject[-1] not in ".!?":
        subject += "."
    parts = [STYLE_BLOCK, FRONT_VIEW if view == "front" else BACK_VIEW,
             f"The creature: {subject}", MAGENTA_BLOCK]
    return " ".join(parts)


def image_part(path):
    data = Path(path).read_bytes()
    return {"inline_data": {"mime_type": "image/png",
                            "data": base64.b64encode(data).decode("ascii")}}


def build_request(brief, view, front_ref=None):
    prompt = build_prompt(brief, view)
    parts = []
    if view == "back" and front_ref:
        prompt = prompt + " " + FRONT_REF_NOTE
    parts.append({"text": prompt})
    if view == "back" and front_ref:
        parts.append(image_part(front_ref))
    return {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE"],
                             "imageConfig": {"aspectRatio": "1:1"}},
    }, prompt


def call_api(req, api_key, model, retries=HTTP_RETRIES):
    url = f"{API_ROOT}/{model}:generateContent"
    body = json.dumps(req).encode("utf-8")
    for attempt in range(retries + 1):
        r = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
        try:
            with urllib.request.urlopen(r, timeout=180) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code in (429, 500, 502, 503) and attempt < retries:
                wait = 15 * (attempt + 1)
                print(f"    HTTP {e.code}, retrying in {wait}s: {detail}")
                time.sleep(wait)
                continue
            raise RuntimeError(f"API error HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                print(f"    network error, retrying in 10s: {e}")
                time.sleep(10)
                continue
            raise RuntimeError(f"network error: {e}") from e


def extract_image(resp):
    for cand in resp.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
    raise NoImageError(f"no image in response: {json.dumps(resp)[:400]}")


def generate_one(req, api_key, model, label):
    """One candidate image, or None if this candidate should be skipped.

    A failure never kills the batch; images already written stay on disk and a
    later run fills the gaps (numbering resumes past whatever exists).
    """
    try:
        for attempt in range(NO_IMAGE_TRIES):
            try:
                return extract_image(call_api(req, api_key, model))
            except NoImageError as e:
                if attempt == NO_IMAGE_TRIES - 1:
                    break
                print(f"    no image returned, retrying in {NO_IMAGE_WAIT}s: {e}")
                time.sleep(NO_IMAGE_WAIT)
        print(f"WARNING: {label}: no image after {NO_IMAGE_TRIES} tries, skipping")
    except RuntimeError as e:
        print(f"WARNING: {label}: {e}; skipping")
    return None


def existing_count(outdir, view):
    return sum(1 for p in outdir.glob(f"{view}_*.png")
               if p.stem.split("_", 1)[1].isdigit())


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brief", required=True,
                    help="one line description of the creature")
    ap.add_argument("--name", required=True,
                    help="species name, used as the candidates subfolder")
    ap.add_argument("--count", type=int, default=6, help="candidates per view (default 6)")
    ap.add_argument("--view", choices=VIEWS + ("both",), default="front",
                    help="which views to generate (default front)")
    ap.add_argument("--front-ref", metavar="PATH",
                    help="the chosen front candidate, attached to back requests "
                         "so the back view is the same creature")
    ap.add_argument("--out", default="candidates", help="output root (default candidates)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"model id (default {DEFAULT_MODEL})")
    ap.add_argument("--force", action="store_true",
                    help="generate a full --count batch even if candidates exist")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompts and the file plan, call nothing")
    args = ap.parse_args()

    if args.count < 1:
        ap.error("--count must be at least 1")
    name = args.name.strip().lower()
    views = list(VIEWS) if args.view == "both" else [args.view]
    if args.front_ref and not Path(args.front_ref).exists():
        ap.error(f"--front-ref {args.front_ref} not found")
    if "back" in views and not args.front_ref:
        print("note: no --front-ref given, the back candidates will not be tied to a "
              "specific front sprite. Pick a front first, then rerun with --front-ref.")

    outdir = Path(args.out) / name
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key and not args.dry_run:
        sys.exit("error: GEMINI_API_KEY is not set (image generation needs a "
                 "billing enabled key)")

    made = 0
    for view in views:
        req, prompt = build_request(args.brief, view, args.front_ref)
        have = 0 if args.dry_run else (0 if args.force else existing_count(outdir, view))
        want = max(0, args.count - have)
        if args.dry_run:
            refs = len(req["contents"][0]["parts"]) - 1
            print(f"\n=== {name} {view}: {args.count} candidates, "
                  f"{refs} reference image(s), model {args.model} ===")
            print(prompt)
            print("  plan: " + ", ".join(
                str(outdir / f"{view}_{n}.png") for n in range(1, args.count + 1)))
            continue
        if want == 0:
            print(f"skip {name} {view}: already has {have} candidates (use --force)")
            continue
        outdir.mkdir(parents=True, exist_ok=True)
        for i in range(want):
            n = have + i + 1
            label = f"{name} {view} {n}"
            print(f"{label}: candidate {n}/{args.count} ...")
            png = generate_one(req, api_key, args.model, label)
            if png is None:
                continue
            (outdir / f"{view}_{n}.png").write_bytes(png)
            made += 1

    if args.dry_run:
        print("\ndry run: nothing written, no API calls made")
        return
    print(f"\ndone: {made} images -> {outdir}")
    print(f"next: python3 tools/picksheet.py --name {name} && open picksheet.html")


if __name__ == "__main__":
    main()
