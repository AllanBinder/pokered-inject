# Put your own Pokemon in Red/Blue: the step by step guide

This is the short version of the [README](README.md). You write one small JSON file, drop
in two sprite PNGs, run one command, and about six seconds later you have a Pokemon Blue
(or Red) ROM with a brand new species: its own name, types, base stats, learnset, TM list,
evolution, Pokedex entry, cry, palette, and wild encounters on the routes you choose. It
shows up as number 152 in the Pokedex, gets caught in the grass, and saves and loads like
any other. It works because [pret/pokered](https://github.com/pret/pokered) is a full
disassembly of the game that rebuilds the retail ROM byte for byte, so the injector edits
the game's own data tables and runs the game's own build.

## 1. What you need

- A Mac or Linux machine with `git`, `make` and a C compiler (Xcode command line tools on
  a Mac, `build-essential` on Debian or Ubuntu).
- Python 3.8 or newer. The injector uses the standard library only.
- [rgbds 1.0.3](https://github.com/gbdev/rgbds), the Game Boy assembler pokered pins to:
  `brew install rgbds`, or build it from source. Other versions refuse to assemble.
- Pillow (`pip install pillow`), only if you use the sprite tools.
- Optional: a Gemini API key with billing enabled, only for the tool that generates sprite
  candidates from a sentence. You can draw or find sprites any other way.
- An emulator to play the result (any Game Boy emulator works), or a flash cart for real
  hardware.

You do not need a ROM dump. This repo ships no Nintendo assets, and pokered builds the game
from source.

## 2. Set up once

```
git clone https://github.com/AllanBinder/pokered-inject
cd pokered-inject
git submodule update --init
```

The last line pulls the pokered disassembly into `pokered/`. Prove the toolchain works by
building the example:

```
python3 inject.py examples/potash/mon.json --game blue
```

The ROM lands at `out/potash_blue.gbc`. Open it in an emulator, start a new game, walk to
Route 16 and step in the grass. POTASH, a fire type kiln pot, is about one in five
encounters.

## 3. Write your mon.json

Copy `examples/potash/mon.json` to `examples/<yourname>/mon.json` and edit it. The full key
reference is in the README; the keys that matter:

- `name` (up to 10 characters) and `category` (the Pokedex label, like LIZARD, up to 10).
- `types`: one or two of NORMAL FIGHTING FLYING POISON GROUND ROCK BUG GHOST FIRE WATER
  GRASS ELECTRIC PSYCHIC ICE DRAGON.
- `stats`: hp, atk, def, spd, spc, each 1 to 255. Gen 1 has one Special stat. A total
  around 300 feels like an early route mon, 500 like a fully evolved one.
- `catch_rate` (255 is Rattata, 3 is a legendary bird), `base_exp`, `growth`.
- `moves.level1` (1 to 4 moves known at level 1), `moves.learnset` (`{"level": "MOVE"}`),
  `moves.tmhm` (TM and HM moves it can learn). Move names are pokered constants like
  EMBER, ROCK_THROW, FIRE_BLAST. A wrong name is reported with the closest matches.
- `evolution`: `null`, or `{"method": "level", "level": 16, "into": "CHARMELEON"}`, or an
  item or trade evolution.
- `dex`: height, weight and the Pokedex text. The text must fit six lines of eighteen
  characters (two pages of three lines). The injector tells you how much to cut.
- `cry`: a base cry CRY_00 to CRY_25 plus a pitch and a length, 0 to 255 each. Try a few;
  the cry plays every time you meet it.
- `palette` (Game Boy Color tint, for example PAL_BROWNMON) and `icon` (party menu icon,
  for example ICON_MON).
- `sprites.front` and `sprites.back`: paths to the two PNGs, relative to the JSON.
- `wild`: a list of `{"map": "Route16", "kind": "grass", "slots": [3, 6], "level": 20}`.
  Map names are the file names in `pokered/data/wild/maps/`. Slot numbers set how common
  it is: slots 0 and 1 are 20 percent each, 2 is 15, 3 to 5 are 10, 6 and 7 are 5, 8 is 4,
  9 is 1.

The easy way: paste the prompt from the README section "Ask Claude to write the spec" into
Claude Code inside the repo, with your concept, type, feel, wild location and evolution
filled in. Claude writes the file, runs the dry run, and fixes anything the injector
complains about.

## 4. Make the two sprites

The game wants a front sprite that is square at 40, 48 or 56 pixels and a back sprite at
32x32, both grayscale PNGs using only the four Game Boy shades (0, 85, 170, 255). The
injector never resamples or recolors; it tells you and stops if a file is not in that shape.

Any image source works: Aseprite, a phone photo of a drawing on white paper, a sprite you
own. Then:

```
python3 tools/prep_sprite.py my_front.png --out examples/<yourname>/front.png --size 56
python3 tools/prep_sprite.py my_back.png --out examples/<yourname>/back.png --size 32
```

It keys out a flat background, crops, centers the creature on the bottom edge, downsamples,
quantizes to the four tones and prints a text preview in the terminal so you can check it
without opening a file. Use `--stretch` if the result looks washed out and `--size 40` if
you want to save ROM space (most Gen 1 mons are 40x40).

If you have a Gemini key, `python3 tools/gen_candidates.py --brief "a clay kiln that came
alive" --name kiln --count 6` writes six candidates, and `python3 tools/picksheet.py --name
kiln` builds an HTML pick sheet showing each one at true Game Boy size with the exact prep
command to promote it. Repeat with `--view back --front-ref <chosen front>` for the back
view. One lesson from making POTASH: generic animal briefs come back looking like existing
Pokemon, so describe an original creature or an object. See `tools/README.md`.

## 5. Inject and play

```
python3 inject.py examples/<yourname>/mon.json --game blue
```

Useful flags:

- `--dry-run` prints every planned edit and touches nothing. Add `--diff` to see the
  unified diff.
- `--game red`, or `--game both` if you want the same wild encounters in both versions
  (Red and Blue have separate encounter tables).
- `--replace PIDGEY` overwrites an existing species instead of adding number 152.
- `--reset` restores the pokered checkout to pristine before injecting. The injector
  refuses to run on a modified checkout without it, so it never throws away your own edits
  by accident.

Running it again with the same spec updates the species in place. Running it with a second
spec adds a second species next to the first. Load `out/<yourname>_blue.gbc` in your
emulator.

## 6. Things to know

- Saves made on the retail game keep working on the injected ROM. Bank 0 and every map and
  tileset bank keep their retail layout, so you can carry your existing save over and go
  catch the new species.
- Number 152 is free. Adding a 153rd and up still builds, but shifts the save layout, so
  old saves will not load. The injector warns.
- There is room for roughly ten added species before bank 0's free space runs out; after
  that the build fails loudly instead of producing a broken ROM.
- Trading or link battling with an unpatched cartridge shows your species as MissingNo on
  the other side. Both players need the same ROM.
- A species whose name matches a move, item or type (EMBER, for example) gets an internal
  constant like EMBER_MON. Nothing visible changes in game.
- Mew cannot be replaced; its data lives outside the main table.
- Do not distribute the ROMs you build. The tool and its docs are MIT; the game is
  Nintendo's. Use the result in place of a cartridge you own.

## Getting help

Open an issue on this repo with your `mon.json`, the command you ran and the full output.
The injector's error messages are written to be pasted into a Claude chat, which is how
this tool was built: the injector, the sprite tools and this guide were written with Claude
Code (Anthropic's Claude) alongside the repo owner.
