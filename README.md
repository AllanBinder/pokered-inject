# pokered-inject

Put your own Pokemon into Pokemon Red or Blue. You write a small JSON file (or ask Claude to
write it for you), drop in a front and a back sprite, run one command, and about six seconds
later you have a working `.gbc` with a brand new species: its own name, types, base stats,
level up learnset, TM and HM list, evolution, Pokedex entry, cry, palette, party menu icon,
and wild encounters on the routes you choose. It is a real species, not a glitch: it shows up
as number 152 in the Pokedex, it can be caught in the grass, it evolves, it learns TMs, and it
saves and loads like any other. This works because [pret/pokered](https://github.com/pret/pokered)
is a full disassembly of the game that rebuilds the retail ROM byte for byte, so the injector
edits the game's own data tables and runs the game's own build.

```
python3 inject.py examples/potash/mon.json --game blue
# -> out/potash_blue.gbc
```

## Requirements

- Python 3.8 or newer. `inject.py` uses the standard library only, nothing to pip install.
- [rgbds 1.0.3](https://github.com/gbdev/rgbds) (`brew install rgbds`, or build from source).
  This exact version is what pokered pins; other versions will refuse to assemble.
- `make` and a C compiler (pokered builds two small tools of its own) plus `git`.
- Pillow, only if you use the sprite tools in `tools/`. The injector itself never needs it.

First time setup:

```
git clone https://github.com/AllanBinder/pokered-inject
cd pokered-inject
git submodule update --init
```

## The three step workflow

**1. Write `mon.json`.** Copy `examples/potash/mon.json` and edit it, or paste the prompt at the
bottom of this README into Claude and let it write the file for you. Every key is documented
below.

**2. Make the sprites.** You need a front sprite (square, 40, 48 or 56 pixels) and a back
sprite (32x32), both grayscale PNGs using only the four Game Boy shades 0, 85, 170 and 255.
`tools/prep_sprite.py` turns any image into one, `tools/gen_candidates.py` generates candidates
from a text description, and `tools/picksheet.py` shows them all at true Game Boy size so you
can pick. See `tools/README.md`. The injector never resamples or recolors a sprite: if the file
is not already in the right shape it tells you and stops.

**3. Inject.**

```
python3 inject.py path/to/mon.json --game blue
```

The ROM and its symbol file land in `out/`. Play it in any emulator, or on hardware by flashing
it to a flash cart.

## Command line

```
python3 inject.py SPEC [options]

--game blue|red|both   which ROM to build (default: blue)
--replace SPECIES      overwrite an existing species (for example --replace PIDGEY)
                       instead of adding a new one
--reset                restore the pokered checkout to HEAD before injecting.
                       Without it, a modified pokered checkout is refused.
--dry-run              print every planned edit and touch nothing
--diff                 with --dry-run, also print a unified diff
--no-build             patch pokered but do not run make
--pokered PATH         path to the pokered checkout (default: ./pokered)
--out DIR              where to put the ROM and .sym (default: ./out)
```

Injection is re-runnable. Running it again with the same spec updates the same species in place,
and running it with a second spec adds a second species next to the first. It refuses to start
on a modified pokered checkout unless you pass `--reset`, so it can never quietly throw away
edits you made yourself.

`--game both` matters for wild encounters: Red and Blue have different encounter tables in the
same file, so a `--game blue` run only rewrites the slots Blue reads. Use `--game both` if you
want the same species in both versions.

## mon.json key reference

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Species name, 1 to 10 characters, A to Z, digits, space, `.`, `'`, `-`. Shown in caps in game. |
| `category` | yes | The Pokedex label under the name, at most 10 characters. Charmander's is `LIZARD`. |
| `types` | yes | One or two type constants: `NORMAL FIGHTING FLYING POISON GROUND ROCK BUG GHOST FIRE WATER GRASS ELECTRIC PSYCHIC ICE DRAGON`. One entry means both types are the same, which is how pokered stores a single type mon. |
| `stats` | yes | `{"hp", "atk", "def", "spd", "spc"}`, each 1 to 255. `spc` is Special: Gen 1 has no split. |
| `catch_rate` | no (45) | 0 to 255. Higher is easier. Rattata is 255, the legendary birds are 3. |
| `base_exp` | no (65) | 0 to 255, experience yield. |
| `growth` | no (`MEDIUM_SLOW`) | `MEDIUM_FAST`, `SLIGHTLY_FAST`, `SLIGHTLY_SLOW`, `MEDIUM_SLOW`, `FAST`, `SLOW`. The `GROWTH_` prefix is optional. |
| `moves.level1` | yes | 1 to 4 move constants known at level 1. |
| `moves.learnset` | no | `{"level": "MOVE"}` for level up moves, levels 2 to 100. Order does not matter, the injector sorts it. |
| `moves.tmhm` | no | Move constants this species can learn from a TM or an HM. Only real TM and HM moves are accepted, and a wrong name is reported with the closest matches. |
| `evolution` | no (`null`) | `null`, or `{"method": "level", "level": 16, "into": "CHARMELEON"}`, or `{"method": "item", "item": "FIRE_STONE", "into": "..."}`, or `{"method": "trade", "into": "..."}`. |
| `dex.height_ft` / `dex.height_in` | no (0) | Height in feet and inches, as the Pokedex prints it. |
| `dex.weight_lb` | no (0) | Weight in pounds, one decimal place is kept. |
| `dex.text` | yes | The Pokedex entry as one sentence or two. It is wrapped for you into at most 6 lines of 18 characters (two pages of three lines). Longer text is an error that tells you how much to cut. |
| `cry.base` | no (`CRY_00`) | Base cry, `CRY_00` through `CRY_25` (hex, so `CRY_1F` is valid). The `SFX_` prefix is optional. |
| `cry.pitch` | no (0) | 0 to 255. Higher is higher pitched. |
| `cry.length` | no (128) | 0 to 255 cry length. |
| `palette` | no (`PAL_MEWMON`) | Game Boy Color palette: `PAL_MEWMON PAL_BLUEMON PAL_REDMON PAL_CYANMON PAL_PURPLEMON PAL_BROWNMON PAL_GREENMON PAL_PINKMON PAL_YELLOWMON PAL_GRAYMON`. The `PAL_` prefix is optional. |
| `icon` | no (`ICON_MON`) | Party menu icon: `ICON_MON ICON_BALL ICON_HELIX ICON_FAIRY ICON_BIRD ICON_WATER ICON_BUG ICON_GRASS ICON_SNAKE ICON_QUADRUPED`. The `ICON_` prefix is optional. |
| `sprites.front` | yes | Path to the front sprite PNG, relative to the mon.json. Square, 40, 48 or 56 pixels. |
| `sprites.back` | yes | Path to the back sprite PNG, relative to the mon.json. 32x32. |
| `wild` | no | List of `{"map": "Route1", "kind": "grass", "slots": [2, 5], "level": 4}`. Each entry replaces those encounter slots on that map. |
| `species_const` | no | Override the assembler constant for the species. Only needed if the automatic name clashes; see below. |
| `label` | no | Override the CamelCase name used for assembly labels and for the sprite filenames. |

### Wild encounter slots

Every map has ten grass slots and ten surfing slots, and the slot number decides how often you
meet it:

| Slot | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Chance | 20% | 20% | 15% | 10% | 10% | 10% | 5% | 5% | 4% | 1% |

`kind` is `grass` or `water`. Map names are the file names in
`pokered/data/wild/maps/` (`Route1`, `ViridianForest`, `MtMoon1F`, `SeaRoutes`, and so on); a
typo lists the closest matches. Asking for water on a map with no surfing table, or grass on a
map with no grass, is an error rather than a silently broken ROM.

### Ask Claude to write the spec

Paste this, filled in, into Claude Code in this repo (or any Claude chat, then save the result
as `mon.json`):

```
Read the mon.json key reference in the README of this repo, then write a complete
mon.json for a new Gen 1 Pokemon and save it to examples/<name>/mon.json.

Concept: <one or two sentences, for example "a nocturnal moth that carries embers
in the dust on its wings">
Type: <for example FIRE / BUG>
Feel: <for example an early route mon a player meets around level 6, weak but fast>
Wild: <for example Viridian Forest grass, common, level 6 to 8>
Evolution: <none, or "evolves at level 24 into something bigger">

Rules: use only real pokered constants for moves, types, growth, palette and icon.
Keep base stats in the range of a real mon of that stage, total around 300 for an
early mon. The Pokedex text must fit in 6 lines of 18 characters. Only put real
TM and HM moves in moves.tmhm. Then run
python3 inject.py examples/<name>/mon.json --dry-run
and fix anything it complains about.
```

Claude cannot draw the sprites for you here: use `tools/gen_candidates.py` and
`tools/prep_sprite.py` for those.

## How it works

For each run, `inject.py` edits pokered's own tables, anchored on the text of the lines rather
than on line numbers so repeated runs stack:

- `constants/pokemon_constants.asm`: the new species takes the first unused internal index (the
  game ships 190 index slots and 36 of them are the MissingNo holes, so nothing existing moves).
- `constants/pokedex_constants.asm`: a new `DEX_` constant, which bumps `NUM_POKEMON` to 152.
- `data/pokemon/names.asm`, `cries.asm`, `dex_order.asm`, `dex_entries.asm`, `evos_moves.asm`:
  the five tables indexed by internal index, plus the appended Pokedex entry and evolution or
  learnset blocks.
- `data/pokemon/dex_text.asm`: the wrapped Pokedex text.
- `data/pokemon/base_stats/<name>.asm` and its `INCLUDE` in `data/pokemon/base_stats.asm`.
- `data/pokemon/palettes.asm` and `data/pokemon/menu_icons.asm`: the two tables indexed by
  Pokedex number.
- `gfx/pokemon/front/<name>.png` and `gfx/pokemon/back/<name>b.png`: your sprites. The Makefile
  turns them into `.2bpp` and then into the game's compressed `.pic` format on its own.
- `gfx/pics.asm` and `home/pics.asm`: sprites go into their own floating section, because every
  retail pic bank has under 400 bytes of room left. The game picks a sprite's bank from the
  species index, so the injector adds one index special case (7 bytes of bank 0) next to the
  ones the game already has for Mew and the fossil pics.
- `data/wild/maps/<Map>.asm`: the encounter slots you asked for.

Two details worth knowing if you go reading the diff. `BaseStats` in retail stops at Pokedex
number 150 because Mew's row lives in another bank, so adding number 152 also fills Mew's empty
row (with Mew's own stats, which the game never reads from there). And the species constant
shares one namespace with move, item and type constants, so a species called EMBER would clash
with the move EMBER: the injector notices and uses `EMBER_MON` for the constant, which changes
nothing you can see in game. Set `species_const` yourself if you want a different one.

## Limits

- Pokedex number 152 is free. Number 153 and up still build, but the "seen" and "owned"
  bitfields grow by a byte and shift the save layout, so old save files will not load. The
  injector warns when that happens.
- Names are at most 10 characters, the Pokedex category at most 10, and the Pokedex entry at
  most 6 lines of 18 characters. Only characters the Game Boy font has are allowed.
- Sprites are four shades, no more: 0, 85, 170 and 255. Front sprites are square at 40, 48 or 56
  pixels, back sprites are 32x32. The compressor the game uses is happiest with flat shapes and
  a clear outline.
- Special is one stat in Gen 1, there are no abilities, no held items, and no natures.
- Trading or link battling with an unpatched cartridge shows your species as MissingNo on the
  other side. Both players need the same ROM.
- Every added species costs about 50 bytes in the bank that holds the base stats and evolution
  data, 7 bytes of bank 0, and the size of its two sprites in a spare bank. There is room for
  roughly a dozen before bank 0 runs out; after that, `make` fails with a section overflow
  instead of building something broken.
- `--replace SPECIES` swaps a real species in place and keeps its Pokedex number, so saves stay
  compatible. Mew cannot be replaced (its data lives outside the main table).

## Credits and legal

The heavy lifting is [pret/pokered](https://github.com/pret/pokered), the Pokemon Red and Blue
disassembly, and [rgbds](https://github.com/gbdev/rgbds), the assembler it builds with. Thank
you to everyone who worked on both. `inject.py` is only a patch script on top of them.

This repository ships no Nintendo assets: no ROM, no sprites, no text, no music. pokered is a
disassembly of the game's code, and building it requires nothing from a retail cartridge. The
ROM you produce is a modified build of that disassembly, for use with a copy of the game you own,
in place of your own cartridge dump. Do not distribute the ROMs you build. Pokemon is a trademark
of Nintendo, Creatures Inc. and GAME FREAK Inc.; this project is not affiliated with any of them.

`inject.py`, the tools and this documentation are MIT licensed. See LICENSE.
