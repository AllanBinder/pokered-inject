# Sprite tools

A pokered sprite is a mode L PNG using only the four values 0, 85, 170 and 255:
40x40, 48x48 or 56x56 for the front view, 32x32 for the back view. These three
tools get you there from a sentence, or from any image you already have.

## Workflow

1. Write a one line brief: `a small fire lizard that keeps a coal alight in its belly`.
2. `python3 tools/gen_candidates.py --brief "..." --name ember --count 6` writes `candidates/ember/front_1..6.png`.
3. `python3 tools/picksheet.py --name ember && open picksheet.html` shows every candidate as the finished sprite at 6x zoom, with the command to promote it.
4. Run that command: `python3 tools/prep_sprite.py candidates/ember/front_3.png --out examples/ember/front.png --size 56`, then repeat steps 2 to 4 with `--view back --front-ref candidates/ember/front_3.png` for the 32x32 back view.
5. Point `mon.json` at the two PNGs and run `python3 inject.py examples/ember/mon.json`.

## You do not need Gemini

`gen_candidates.py` is a convenience. Anything that produces an image works:
Aseprite, Photoshop, a phone photo of a drawing on white paper, a sprite you
already own. `prep_sprite.py` is the only required step, and it is happy with
RGB or RGBA input of any size on a flat magenta or flat light background. It
keys the background out, autocrops, centers the artwork on the bottom edge of
the square, downsamples, quantizes to the four tones and prints a text preview
of the result in the terminal, so you (or Claude) can check the sprite without
opening a file. Running it on a sprite that is already conformant is a no-op.

## The Gemini key

`gen_candidates.py` reads `GEMINI_API_KEY` from the environment and calls
`gemini-3.1-flash-image`. Image generation has no free tier: the key's Google
Cloud project needs billing enabled, or every call comes back as a quota error.
Use `--dry-run` to see the exact prompts and the file plan without calling the
API. Candidate images are throwaway; keep them out of the repo.

## Flags worth knowing

- `prep_sprite.py --size 40|48|56` front edge. Bigger is not better: 56x56 pics
  eat more ROM bank space, and 40x40 is what most Gen 1 mons use.
- `--resample box` (default) averages the source pixels that land in each output
  pixel, which is what you want for a large clean upscale of a pixel grid.
  `--resample nearest` suits input that is already at or near the target size.
- `--dither` applies a 4x4 ordered dither before quantizing. Off by default:
  Game Boy sprites are flat, not dithered.
- `--stretch` rescales the artwork's luminance to the full range first. Use it
  when a candidate comes back washed out and prep warns that fewer than three
  tones are really used.
- `--margin N` background pixels kept on each side of artwork that has to be
  scaled down. `--anchor center` if you do not want the creature standing on the
  bottom edge of its box.
