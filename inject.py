#!/usr/bin/env python3
"""inject.py: add a custom Pokemon species to the pret/pokered disassembly, then build a ROM.

Reads a small JSON spec (see examples/ember/mon.json and the README key reference),
patches every pokered table a real species touches, and runs pokered's own Makefile.
Python 3 standard library only.

Usage:
    python3 inject.py examples/ember/mon.json --game blue
    python3 inject.py examples/ember/mon.json --game both --reset
    python3 inject.py examples/ember/mon.json --dry-run --diff
    python3 inject.py examples/ember/mon.json --replace PIDGEY
"""

import argparse
import difflib
import json
import re
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

# ---------------------------------------------------------------------------
# constants of the injector itself
# ---------------------------------------------------------------------------

FRONT_SIZES = (40, 48, 56)
BACK_SIZE = 32
SHADES = (0, 85, 170, 255)

# The pics bank for a species is chosen from its internal index in home/pics.asm.
# Every "Pics N" bank in retail has under 400 bytes of slack, so an injected pic
# pair never fits there. We put injected pics in their own floating section and
# add an index special case to home/pics.asm, next to the ones for Mew and the
# fossil pics. See the README "How it works" section.
PIC_SECTION = 'SECTION "Injected Pics", ROMX'

# Text that can be typed into a dex entry or a dex category. Derived from
# constants/charmap.asm (the English font).
TEXT_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    " ():;[]'-?!.,/"
    "é♂♀×"
)
# A species name is drawn with the same font but only ever shown in caps.
NAME_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .'-♂♀")

DEX_LINE_WIDTH = 18
DEX_MAX_LINES = 6

TYPE_ALIASES = {"PSYCHIC": "PSYCHIC_TYPE", "PSY": "PSYCHIC_TYPE"}
MOVE_ALIASES = {"PSYCHIC": "PSYCHIC_M"}

# Species constants that cannot be replaced: Mew's base stats live outside the
# BaseStats table, and the rest have no Pokedex number at all.
UNREPLACEABLE = {
    "MEW": "Mew's base stats live in their own bank (see home/pokemon.asm)",
    "FOSSIL_KABUTOPS": "not a real species, it is a fossil pic",
    "FOSSIL_AERODACTYL": "not a real species, it is a fossil pic",
    "MON_GHOST": "not a real species, it is the Marowak ghost pic",
}

BUILD_ARTIFACT_SUFFIXES = (
    ".o", ".2bpp", ".1bpp", ".pic", ".gbc", ".gb", ".sym", ".map", ".patch",
)


class InjectError(Exception):
    """A problem the user can fix. Printed without a traceback."""


def suggest(name, candidates, prefix=""):
    """Return a ' did you mean ...' tail for an unknown constant name."""
    by_upper = {}
    for c in sorted(candidates):
        by_upper.setdefault(c.upper(), c)
    close = difflib.get_close_matches(str(name).upper(), sorted(by_upper), n=3, cutoff=0.5)
    if not close:
        return ""
    return " Did you mean: " + ", ".join(prefix + by_upper[c] for c in close) + "?"


# ---------------------------------------------------------------------------
# PNG reading (stdlib only): enough of the format to validate a pokered sprite
# ---------------------------------------------------------------------------

def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw, width, height, bit_depth):
    stride = (width * bit_depth + 7) // 8
    fbpp = max(1, bit_depth // 8)
    out = []
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        if pos >= len(raw):
            raise InjectError("truncated PNG image data")
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        if len(line) != stride:
            raise InjectError("truncated PNG image data")
        pos += stride
        if ftype == 0:
            pass
        elif ftype == 1:
            for x in range(fbpp, stride):
                line[x] = (line[x] + line[x - fbpp]) & 0xFF
        elif ftype == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ftype == 3:
            for x in range(stride):
                a = line[x - fbpp] if x >= fbpp else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 0xFF
        elif ftype == 4:
            for x in range(stride):
                a = line[x - fbpp] if x >= fbpp else 0
                c = prev[x - fbpp] if x >= fbpp else 0
                line[x] = (line[x] + _paeth(a, prev[x], c)) & 0xFF
        else:
            raise InjectError("unknown PNG filter type %d" % ftype)
        out.append(bytes(line))
        prev = line
    return out, stride


def _samples(row, width, bit_depth):
    if bit_depth == 8:
        return list(row[:width])
    per_byte = 8 // bit_depth
    mask = (1 << bit_depth) - 1
    vals = []
    for x in range(width):
        byte = row[x // per_byte]
        shift = 8 - bit_depth * (x % per_byte + 1)
        vals.append((byte >> shift) & mask)
    return vals


def read_png_gray(path):
    """Return (width, height, set_of_8bit_gray_values) for a grayscale PNG.

    Accepts grayscale (color type 0) at 1/2/4/8 bits and indexed color
    (color type 3) with a gray palette, which is what pokered's own sprite
    PNGs and Pillow's mode L / mode P output look like.
    """
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise InjectError("%s is not a PNG file" % path)
    pos = 8
    ihdr = None
    plte = None
    idat = bytearray()
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            ihdr = body
        elif ctype == b"PLTE":
            plte = body
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
    if ihdr is None or len(ihdr) < 13:
        raise InjectError("%s has no PNG header" % path)
    width = int.from_bytes(ihdr[0:4], "big")
    height = int.from_bytes(ihdr[4:8], "big")
    bit_depth = ihdr[8]
    color_type = ihdr[9]
    interlace = ihdr[12]
    if interlace:
        raise InjectError("%s is interlaced; save it without interlacing" % path)
    if color_type not in (0, 3):
        raise InjectError(
            "%s is not grayscale or indexed (PNG color type %d). Run it through "
            "tools/prep_sprite.py." % (path, color_type)
        )
    if bit_depth not in (1, 2, 4, 8):
        raise InjectError("%s has an unsupported PNG bit depth of %d" % (path, bit_depth))
    rows, _ = _unfilter(zlib.decompress(bytes(idat)), width, height, bit_depth)

    values = set()
    if color_type == 0:
        scale = {1: 255, 2: 85, 4: 17, 8: 1}[bit_depth]
        for row in rows:
            values.update(v * scale for v in _samples(row, width, bit_depth))
    else:
        if plte is None:
            raise InjectError("%s is indexed but has no palette" % path)
        entries = [tuple(plte[i:i + 3]) for i in range(0, len(plte), 3)]
        used = set()
        for row in rows:
            used.update(_samples(row, width, bit_depth))
        for idx in sorted(used):
            if idx >= len(entries):
                raise InjectError("%s uses a palette index outside its palette" % path)
            r, g, b = entries[idx]
            if not (r == g == b):
                raise InjectError(
                    "%s has a non-gray palette entry (%d,%d,%d). Run it through "
                    "tools/prep_sprite.py." % (path, r, g, b)
                )
            values.add(r)
    return width, height, values


def validate_sprite(path, kind):
    width, height, values = read_png_gray(path)
    hint = "Run it through tools/prep_sprite.py; inject.py never resamples."
    if kind == "front":
        if width != height or width not in FRONT_SIZES:
            raise InjectError(
                "front sprite %s is %dx%d; it must be square and 40, 48 or 56 pixels. %s"
                % (path, width, height, hint)
            )
    else:
        if (width, height) != (BACK_SIZE, BACK_SIZE):
            raise InjectError(
                "back sprite %s is %dx%d; it must be 32x32. %s" % (path, width, height, hint)
            )
    bad = sorted(v for v in values if v not in SHADES)
    if bad:
        raise InjectError(
            "%s uses gray values %s; a pokered sprite may only use 0, 85, 170 and 255. %s"
            % (path, ", ".join(str(v) for v in bad), hint)
        )
    return width


# ---------------------------------------------------------------------------
# the pokered working tree
# ---------------------------------------------------------------------------

class Tree:
    """Buffers every edit so --dry-run can print them and a failure changes nothing."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        if not (self.root / "Makefile").is_file():
            raise InjectError("%s does not look like a pokered checkout" % self.root)
        self._orig = {}
        self._lines = {}
        self.new_files = {}
        self.copies = {}
        self.log = []

    # --- file access -------------------------------------------------------

    def lines(self, rel):
        if rel not in self._lines:
            path = self.root / rel
            if not path.is_file():
                raise InjectError("pokered is missing %s" % rel)
            text = path.read_text(encoding="utf-8")
            self._orig[rel] = text
            self._lines[rel] = text.split("\n")
        return self._lines[rel]

    def text(self, rel):
        return "\n".join(self.lines(rel))

    def note(self, action, rel, detail=""):
        self.log.append((action, rel, detail))

    def set_line(self, rel, i, new, why=""):
        lines = self.lines(rel)
        old = lines[i]
        if old == new:
            return
        lines[i] = new
        self.note("edit", "%s:%d" % (rel, i + 1), "%s  ->  %s" % (old.strip(), new.strip()) + (" (%s)" % why if why else ""))

    def insert(self, rel, i, new_lines, why=""):
        lines = self.lines(rel)
        lines[i:i] = list(new_lines)
        self.note("insert", "%s:%d" % (rel, i + 1), (why or new_lines[0].strip()))

    def append_block(self, rel, block, why=""):
        lines = self.lines(rel)
        while lines and lines[-1].strip() == "":
            lines.pop()
        lines.append("")
        lines.extend(block)
        lines.append("")
        self.note("append", rel, why or block[0].strip())

    def drop_block(self, rel, start_marker, next_marker_re):
        """Remove an earlier injection of the same species so re-runs stay clean."""
        lines = self.lines(rel)
        start = None
        for i, line in enumerate(lines):
            if line.startswith(start_marker):
                start = i
                break
        if start is None:
            return False
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if re.match(next_marker_re, lines[i]):
                end = i
                break
        while end > start and lines[end - 1].strip() == "":
            end -= 1
        del lines[start:end]
        self.note("replace", rel, "existing %s block" % start_marker.rstrip(":"))
        return True

    def add_file(self, rel, text, why=""):
        self.new_files[rel] = text
        self.note("write", rel, why)

    def add_copy(self, rel, src, why=""):
        self.copies[rel] = Path(src)
        self.note("copy", rel, why or str(src))

    # --- row helpers -------------------------------------------------------

    @staticmethod
    def rows(lines, pattern, stop=None, start=0):
        """Indices of the data lines of a table, in table order."""
        out = []
        for i in range(start, len(lines)):
            s = lines[i].strip()
            if stop and re.match(stop, s):
                break
            if re.match(pattern, s):
                out.append(i)
        return out

    def find(self, rel, pattern, what=None):
        for i, line in enumerate(self.lines(rel)):
            if re.search(pattern, line):
                return i
        raise InjectError(
            "could not find %s in %s (pokered may have changed; expected /%s/)"
            % (what or "the anchor", rel, pattern)
        )

    # --- output ------------------------------------------------------------

    def print_plan(self, show_diff=False):
        width = max((len(a) for a, _, _ in self.log), default=4)
        for action, rel, detail in self.log:
            print("  %-*s %-46s %s" % (width, action, rel, detail))
        if not show_diff:
            return
        print()
        for rel, text in sorted(self._orig.items()):
            new = "\n".join(self._lines[rel])
            if new == text:
                continue
            diff = difflib.unified_diff(
                text.split("\n"), new.split("\n"),
                fromfile="a/" + rel, tofile="b/" + rel, lineterm="",
            )
            print("\n".join(diff))
            print()

    def commit(self):
        for rel, lines in self._lines.items():
            new = "\n".join(lines)
            if new != self._orig[rel]:
                (self.root / rel).write_text(new, encoding="utf-8")
        for rel, text in self.new_files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        for rel, src in self.copies.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, path)


# ---------------------------------------------------------------------------
# reading pokered's constant tables (never hardcoded here)
# ---------------------------------------------------------------------------

def parse_const_list(text):
    """Walk a const_def block and return [(index, name_or_None, line_index, is_skip)]."""
    out = []
    value = 0
    for i, line in enumerate(text.split("\n")):
        s = line.strip()
        if s.startswith(";"):
            continue
        m = re.match(r"const_def(?:\s+(-?\d+))?", s)
        if m:
            value = int(m.group(1)) if m.group(1) else 0
            continue
        m = re.match(r"const_next\s+(\d+)", s)
        if m:
            value = int(m.group(1))
            continue
        m = re.match(r"const\s+([A-Za-z_][A-Za-z0-9_]*)", s)
        if m:
            out.append((value, m.group(1), i, False))
            value += 1
            continue
        m = re.match(r"const_skip(?:\s+(\d+))?", s)
        if m:
            n = int(m.group(1)) if m.group(1) else 1
            for k in range(n):
                out.append((value + k, None, i, n == 1))
            value += n
    return out


def const_names(text, macro="const"):
    pat = re.compile(r"^\s*%s\s+([A-Za-z_][A-Za-z0-9_]*)" % macro)
    return [m.group(1) for m in (pat.match(l) for l in text.split("\n")) if m]


class Pokered:
    """Constant tables and species metadata read straight out of the checkout."""

    def __init__(self, tree):
        self.tree = tree
        self.species = parse_const_list(tree.text("constants/pokemon_constants.asm"))
        self.dex = parse_const_list(tree.text("constants/pokedex_constants.asm"))
        self.species_by_name = {n: i for i, n, _, _ in self.species if n}
        self.dex_by_name = {n: i for i, n, _, _ in self.dex if n}
        self.moves = set(const_names(tree.text("constants/move_constants.asm")))
        self.types = set(const_names(tree.text("constants/type_constants.asm")))
        self.growths = {n for n in const_names(tree.text("constants/pokemon_data_constants.asm"))
                        if n.startswith("GROWTH_")}
        pal = tree.text("constants/palette_constants.asm")
        self.palettes = {n for n in const_names(pal) if n.startswith("PAL_")}
        self.icons = {n for n in const_names(tree.text("constants/icon_constants.asm"))
                      if n.startswith("ICON_")}
        self.cries = set(re.findall(r"music_const\s+(SFX_CRY_[0-9A-F]+)",
                                    tree.text("constants/music_constants.asm")))
        items = tree.text("constants/item_constants.asm")
        self.items = set(const_names(items))
        self.tm_moves = set(re.findall(r"^\s*add_(?:tm|hm)\s+([A-Z0-9_]+)", items, re.M))
        self.maps = sorted(p.stem for p in (tree.root / "data/wild/maps").glob("*.asm")
                           if p.stem != "nothing")
        self.symbols = self._all_symbols()
        self.base_stats_files = self._base_stats_files()

    def _all_symbols(self):
        names = set()
        pat = re.compile(r"^\s*(?:const|DEF)\s+([A-Za-z_][A-Za-z0-9_]*)")
        for path in sorted((self.tree.root / "constants").glob("*.asm")):
            for line in path.read_text(encoding="utf-8").split("\n"):
                m = pat.match(line)
                if m:
                    names.add(m.group(1))
        return names

    def _base_stats_files(self):
        """dex constant -> data/pokemon/base_stats/<file>.asm"""
        out = {}
        for path in sorted((self.tree.root / "data/pokemon/base_stats").glob("*.asm")):
            m = re.search(r"db\s+(DEX_[A-Z0-9_]+)", path.read_text(encoding="utf-8"))
            if m:
                out[m.group(1)] = "data/pokemon/base_stats/%s" % path.name
        return out

    def num_pokemon(self):
        return max(i for i, n, _, _ in self.dex if n)

    def first_free_index(self):
        for index, name, line, single in self.species:
            if name is None and single and index > 0:
                return index, line
        raise InjectError("no free MissingNo species index left in constants/pokemon_constants.asm")

    def index_of(self, name):
        return self.species_by_name.get(name)


# ---------------------------------------------------------------------------
# the spec
# ---------------------------------------------------------------------------

def want(spec, key, kind=dict):
    if key not in spec:
        raise InjectError('mon.json is missing the "%s" key' % key)
    value = spec[key]
    if not isinstance(value, kind):
        raise InjectError('mon.json key "%s" should be a %s' % (key, kind.__name__))
    return value


def as_int(value, key, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise InjectError('mon.json key "%s" should be a whole number, got %r' % (key, value))
    if not lo <= n <= hi:
        raise InjectError('mon.json key "%s" should be between %d and %d, got %d' % (key, lo, hi, n))
    return n


def norm(value, key, valid, pk, prefix="", aliases=None):
    """Accept a constant with or without its pokered prefix, suggest on a typo."""
    if value is None:
        raise InjectError('mon.json key "%s" is required' % key)
    name = str(value).strip().upper().replace(" ", "_")
    if aliases and name in aliases:
        name = aliases[name]
    candidates = [name, prefix + name]
    # Accept a partly written prefix too, so "CRY_00" finds SFX_CRY_00.
    for i in range(1, len(prefix)):
        if prefix[i:] and name.startswith(prefix[i:]):
            candidates.append(prefix[:i] + name)
    if name.isdigit():
        candidates.append(prefix + name.zfill(2))
    for candidate in candidates:
        if candidate in valid:
            return candidate
    raise InjectError(
        'mon.json key "%s": unknown value "%s".%s' % (key, value, suggest(name, valid, ""))
    )


def camel(name):
    parts = re.split(r"[^A-Za-z0-9]+", name.strip())
    return "".join(p[:1].upper() + p[1:].lower() for p in parts if p)


def wrap_dex_text(text):
    words = text.split()
    lines, cur = [], ""
    for word in words:
        if len(word) > DEX_LINE_WIDTH:
            raise InjectError(
                'dex text word "%s" is longer than the %d character Pokedex line'
                % (word, DEX_LINE_WIDTH)
            )
        candidate = (cur + " " + word).strip()
        if len(candidate) > DEX_LINE_WIDTH:
            lines.append(cur)
            cur = word
        else:
            cur = candidate
    if cur:
        lines.append(cur)
    if len(lines) > DEX_MAX_LINES:
        raise InjectError(
            "dex text needs %d lines but the Pokedex only shows %d (two pages of three "
            "%d character lines). Shorten it by about %d characters."
            % (len(lines), DEX_MAX_LINES, DEX_LINE_WIDTH,
               sum(len(l) + 1 for l in lines[DEX_MAX_LINES:]))
        )
    return lines


def check_chars(value, key, allowed):
    bad = sorted({c for c in value if c not in allowed})
    if bad:
        raise InjectError(
            'mon.json key "%s" uses characters the Game Boy font does not have: %s'
            % (key, " ".join(repr(c) for c in bad))
        )


class Mon:
    """The validated spec, resolved against this pokered checkout."""

    def __init__(self, spec, spec_path, pk, replace=None):
        self.spec_dir = spec_path.parent
        self.name = str(want(spec, "name", str)).strip().upper()
        if not 1 <= len(self.name) <= 10:
            raise InjectError('"name" must be 1 to 10 characters, got %d' % len(self.name))
        check_chars(self.name, "name", NAME_CHARS)
        self.category = str(want(spec, "category", str)).strip().upper()
        if len(self.category) > 10:
            raise InjectError('"category" must be at most 10 characters, got %d' % len(self.category))
        check_chars(self.category, "category", NAME_CHARS)

        self.label = camel(spec.get("label") or self.name)
        self.file_base = re.sub(r"[^a-z0-9]", "", self.label.lower())
        if not self.file_base:
            raise InjectError('"name" must contain at least one letter or digit')

        # The species constant shares rgbasm's global namespace with move, item
        # and type constants, so EMBER the species would clash with EMBER the
        # move. Fall back to <NAME>_MON when that happens.
        self.const = spec.get("species_const") or re.sub(r"[^A-Z0-9_]", "_", self.name)
        self.const_auto_renamed = False
        if ("species_const" not in spec and self.const in pk.symbols
                and self.const not in pk.species_by_name):
            self.const = self.const + "_MON"
            self.const_auto_renamed = True
        # An earlier run of this same spec leaves its pic labels behind, which is
        # how a re-run tells itself apart from a name that collides with a real
        # species.
        already_injected = ("BANK(%sPicFront)" % self.label) in pk.tree.text("home/pics.asm")
        if not replace and self.const in pk.species_by_name and not already_injected:
            raise InjectError(
                "%s is already a species in pokered. Use --replace %s to overwrite it, or "
                'set "species_const" in mon.json to an unused constant name.'
                % (self.const, self.const)
            )
        if not replace and self.const in pk.symbols and self.const not in pk.species_by_name:
            raise InjectError(
                'species constant %s is already used in pokered for something else. Set '
                '"species_const" in mon.json to something unused.' % self.const
            )
        self.dex_const = "DEX_" + re.sub(r"[^A-Z0-9_]", "_", self.name)

        types = want(spec, "types", list)
        if len(types) not in (1, 2):
            raise InjectError('"types" needs one or two entries')
        self.types = [norm(t, "types", pk.types, pk, aliases=TYPE_ALIASES) for t in types]
        if len(self.types) == 1:
            self.types *= 2

        stats = want(spec, "stats")
        self.stats = [as_int(stats.get(k), "stats.%s" % k, 1, 255) for k in ("hp", "atk", "def", "spd", "spc")]
        self.catch_rate = as_int(spec.get("catch_rate", 45), "catch_rate", 0, 255)
        self.base_exp = as_int(spec.get("base_exp", 65), "base_exp", 0, 255)
        self.growth = norm(spec.get("growth", "MEDIUM_SLOW"), "growth", pk.growths, pk, prefix="GROWTH_")

        moves = want(spec, "moves")
        level1 = moves.get("level1") or []
        if not isinstance(level1, list) or not 1 <= len(level1) <= 4:
            raise InjectError('"moves.level1" needs 1 to 4 move names')
        self.level1 = [norm(m, "moves.level1", pk.moves, pk, aliases=MOVE_ALIASES) for m in level1]
        self.level1 += ["NO_MOVE"] * (4 - len(self.level1))

        learnset = moves.get("learnset") or {}
        if not isinstance(learnset, dict):
            raise InjectError('"moves.learnset" should be an object of {"level": "MOVE"}')
        pairs = []
        for level, move in learnset.items():
            pairs.append((as_int(level, "moves.learnset key", 2, 100),
                          norm(move, "moves.learnset", pk.moves, pk, aliases=MOVE_ALIASES)))
        self.learnset = sorted(pairs)

        tmhm = moves.get("tmhm") or []
        if not isinstance(tmhm, list):
            raise InjectError('"moves.tmhm" should be a list of TM or HM move names')
        self.tmhm = []
        for move in tmhm:
            name = norm(move, "moves.tmhm", pk.moves, pk, aliases=MOVE_ALIASES)
            if name not in pk.tm_moves:
                raise InjectError(
                    '"moves.tmhm": %s is a move but not a TM or HM move.%s'
                    % (name, suggest(name, pk.tm_moves))
                )
            if name not in self.tmhm:
                self.tmhm.append(name)

        self.evolution = self._evolution(spec.get("evolution"), pk)

        dex = want(spec, "dex")
        self.height_ft = as_int(dex.get("height_ft", 0), "dex.height_ft", 0, 99)
        self.height_in = as_int(dex.get("height_in", 0), "dex.height_in", 0, 11)
        try:
            weight = float(dex.get("weight_lb", 0))
        except (TypeError, ValueError):
            raise InjectError('"dex.weight_lb" should be a number')
        self.weight_tenths = int(round(weight * 10))
        if not 0 <= self.weight_tenths <= 0xFFFF:
            raise InjectError('"dex.weight_lb" must be between 0 and 6553.5')
        dex_text = str(dex.get("text", "")).strip()
        if not dex_text:
            raise InjectError('"dex.text" is required')
        check_chars(dex_text, "dex.text", TEXT_CHARS)
        self.dex_lines = wrap_dex_text(dex_text)

        cry = spec.get("cry") or {}
        self.cry_base = norm(cry.get("base", "SFX_CRY_00"), "cry.base", pk.cries, pk, prefix="SFX_CRY_")
        self.cry_pitch = as_int(cry.get("pitch", 0), "cry.pitch", 0, 255)
        self.cry_length = as_int(cry.get("length", 0x80), "cry.length", 0, 255)

        self.palette = norm(spec.get("palette", "PAL_MEWMON"), "palette", pk.palettes, pk, prefix="PAL_")
        self.icon = norm(spec.get("icon", "ICON_MON"), "icon", pk.icons, pk, prefix="ICON_")

        sprites = want(spec, "sprites")
        self.front_png = self._sprite_path(sprites, "front")
        self.back_png = self._sprite_path(sprites, "back")

        self.wild = self._wild(spec.get("wild") or [], pk)

    def _sprite_path(self, sprites, key):
        if key not in sprites:
            raise InjectError('"sprites.%s" is required' % key)
        path = (self.spec_dir / str(sprites[key])).resolve()
        if not path.is_file():
            raise InjectError(
                '"sprites.%s" points at %s, which does not exist. Make it with '
                "tools/prep_sprite.py." % (key, path)
            )
        return path

    def _evolution(self, evo, pk):
        if evo in (None, False, {}, ""):
            return None
        if not isinstance(evo, dict):
            raise InjectError('"evolution" should be null or an object')
        method = str(evo.get("method", "level")).strip().lower()
        into = str(evo.get("into", "")).strip().upper()
        if into not in pk.species_by_name:
            raise InjectError(
                '"evolution.into": unknown species "%s".%s'
                % (evo.get("into"), suggest(into, pk.species_by_name))
            )
        if method == "level":
            return ("EVOLVE_LEVEL", [as_int(evo.get("level", 16), "evolution.level", 2, 100), into])
        if method == "item":
            item = str(evo.get("item", "")).strip().upper()
            if item not in pk.items:
                raise InjectError(
                    '"evolution.item": unknown item "%s".%s'
                    % (evo.get("item"), suggest(item, pk.items))
                )
            return ("EVOLVE_ITEM", [item, as_int(evo.get("level", 1), "evolution.level", 1, 100), into])
        if method == "trade":
            return ("EVOLVE_TRADE", [as_int(evo.get("level", 1), "evolution.level", 1, 100), into])
        raise InjectError('"evolution.method" should be "level", "item" or "trade", got "%s"' % method)

    def _wild(self, wild, pk):
        if not isinstance(wild, list):
            raise InjectError('"wild" should be a list of encounter entries')
        out = []
        for entry in wild:
            if not isinstance(entry, dict):
                raise InjectError('each "wild" entry should be an object')
            map_name = str(entry.get("map", "")).strip()
            if map_name not in pk.maps:
                raise InjectError(
                    '"wild.map": no wild encounter table for map "%s".%s'
                    % (map_name, suggest(map_name, pk.maps))
                )
            kind = str(entry.get("kind", "grass")).strip().lower()
            if kind not in ("grass", "water"):
                raise InjectError('"wild.kind" should be "grass" or "water", got "%s"' % kind)
            slots = entry.get("slots")
            if not isinstance(slots, list) or not slots:
                raise InjectError('"wild.slots" should be a list of slot numbers 0 to 9')
            slots = sorted({as_int(s, "wild.slots", 0, 9) for s in slots})
            level = as_int(entry.get("level", 5), "wild.level", 1, 100)
            out.append({"map": map_name, "kind": kind, "slots": slots, "level": level})
        return out


# ---------------------------------------------------------------------------
# asm generation
# ---------------------------------------------------------------------------

def base_stats_asm(mon, dex_const):
    lines = [
        "\tdb %s ; pokedex id" % dex_const,
        "",
        "\tdb %s" % ", ".join("%3d" % s for s in mon.stats),
        "\t;   hp  atk  def  spd  spc",
        "",
        "\tdb %s, %s ; type" % (mon.types[0], mon.types[1]),
        "\tdb %d ; catch rate" % mon.catch_rate,
        "\tdb %d ; base exp" % mon.base_exp,
        "",
        '\tINCBIN "gfx/pokemon/front/%s.pic", 0, 1 ; sprite dimensions' % mon.file_base,
        "\tdw %sPicFront, %sPicBack" % (mon.label, mon.label),
        "",
        "\tdb %s ; level 1 learnset" % ", ".join(mon.level1),
        "\tdb %s ; growth rate" % mon.growth,
        "",
        "\t; tm/hm learnset",
    ]
    if not mon.tmhm:
        lines.append("\ttmhm")
    else:
        chunks = [mon.tmhm[i:i + 5] for i in range(0, len(mon.tmhm), 5)]
        for n, chunk in enumerate(chunks):
            last_row = n == len(chunks) - 1
            cells = []
            for k, move in enumerate(chunk):
                final = last_row and k == len(chunk) - 1
                cells.append(("%s" if final else "%s,") % move)
            body = "".join(cell.ljust(14) for cell in cells).rstrip()
            prefix = "\ttmhm " if n == 0 else "\t     "
            lines.append(prefix + body + ("" if last_row else " \\"))
    lines += [
        "\t; end",
        "",
        "\tdb 0 ; padding",
        "",
    ]
    return "\n".join(lines)


def dex_entry_asm(mon):
    return [
        "%sDexEntry:" % mon.label,
        '\tdb "%s@"' % mon.category,
        "\tdb %d,%d" % (mon.height_ft, mon.height_in),
        "\tdw %d" % mon.weight_tenths,
        "\ttext_far _%sDexEntry" % mon.label,
        "\ttext_end",
    ]


def dex_text_asm(mon):
    out = ["_%sDexEntry::" % mon.label]
    page1, page2 = mon.dex_lines[:3], mon.dex_lines[3:]
    for n, line in enumerate(page1):
        out.append('\t%s "%s"' % ("text" if n == 0 else "next", line))
    if page2:
        out.append("")
        for n, line in enumerate(page2):
            out.append('\t%s "%s"' % ("page" if n == 0 else "next", line))
    out.append("\tdex")
    return out


def evos_moves_asm(mon):
    out = ["%sEvosMoves:" % mon.label, "; Evolutions"]
    if mon.evolution:
        method, args = mon.evolution
        out.append("\tdb %s, %s" % (method, ", ".join(str(a) for a in args)))
    out.append("\tdb 0")
    out.append("; Learnset")
    for level, move in mon.learnset:
        out.append("\tdb %d, %s" % (level, move))
    out.append("\tdb 0")
    return out


# ---------------------------------------------------------------------------
# the injection itself
# ---------------------------------------------------------------------------

def patch_species_constant(tree, pk, mon):
    rel = "constants/pokemon_constants.asm"
    existing = pk.index_of(mon.const)
    if existing is not None:
        return existing
    index, line_no = pk.first_free_index()
    tree.set_line(rel, line_no,
                  "\tconst %-20s ; $%02X" % (mon.const, index),
                  why="free MissingNo slot")
    return index


def patch_dex_constant(tree, pk, mon):
    rel = "constants/pokedex_constants.asm"
    if mon.dex_const in pk.dex_by_name:
        return pk.dex_by_name[mon.dex_const], pk.num_pokemon()
    anchor = tree.find(rel, r"^DEF NUM_POKEMON EQU", "the NUM_POKEMON definition")
    number = pk.num_pokemon() + 1
    i = anchor
    while i > 0 and tree.lines(rel)[i - 1].strip() == "":
        i -= 1
    tree.insert(rel, i, ["\tconst %-14s ; %d" % (mon.dex_const, number)],
                why="%s = dex %d, NUM_POKEMON becomes %d" % (mon.dex_const, number, number))
    return number, number


def patch_indexed_tables(tree, pk, mon, index):
    """The five tables indexed by internal species index (190 rows each)."""
    row = index - 1

    rel = "data/pokemon/names.asm"
    rows = Tree.rows(tree.lines(rel), r"dname\b", stop=r"assert_table_length")
    tree.set_line(rel, rows[row], '\tdname "%s"' % mon.name, why="species name")

    rel = "data/pokemon/cries.asm"
    rows = Tree.rows(tree.lines(rel), r"mon_cry\b", stop=r"assert_table_length")
    tree.set_line(rel, rows[row],
                  "\tmon_cry %s, $%02X, $%02X ; %s" % (mon.cry_base, mon.cry_pitch, mon.cry_length, mon.label),
                  why="cry")

    rel = "data/pokemon/dex_order.asm"
    rows = Tree.rows(tree.lines(rel), r"db\b", stop=r"assert_table_length")
    tree.set_line(rel, rows[row], "\tdb %s" % mon.dex_const, why="index to dex number")

    rel = "data/pokemon/dex_entries.asm"
    rows = Tree.rows(tree.lines(rel), r"dw\b", stop=r"assert_table_length")
    tree.set_line(rel, rows[row], "\tdw %sDexEntry" % mon.label, why="dex entry pointer")
    tree.drop_block(rel, "%sDexEntry:" % mon.label, r"^[A-Za-z_][A-Za-z0-9_]*DexEntry:")
    tree.append_block(rel, dex_entry_asm(mon), why="%sDexEntry" % mon.label)

    rel = "data/pokemon/evos_moves.asm"
    rows = Tree.rows(tree.lines(rel), r"dw\b", stop=r"assert_table_length")
    tree.set_line(rel, rows[row], "\tdw %sEvosMoves" % mon.label, why="evos and moves pointer")
    tree.drop_block(rel, "%sEvosMoves:" % mon.label, r"^[A-Za-z_][A-Za-z0-9_]*EvosMoves:")
    tree.append_block(rel, evos_moves_asm(mon), why="%sEvosMoves" % mon.label)

    rel = "data/pokemon/dex_text.asm"
    tree.drop_block(rel, "_%sDexEntry::" % mon.label, r"^_[A-Za-z_][A-Za-z0-9_]*DexEntry::")
    tree.append_block(rel, dex_text_asm(mon), why="_%sDexEntry" % mon.label)


def patch_dex_indexed_tables(tree, pk, mon, dex_number, adding):
    """BaseStats, MonsterPalettes and MonPartyData are indexed by dex number."""
    stats_rel = "data/pokemon/base_stats/%s.asm" % mon.file_base
    tree.add_file(stats_rel, base_stats_asm(mon, mon.dex_const), why="base stats, types, learnset, tmhm")

    rel = "data/pokemon/base_stats.asm"
    lines = tree.lines(rel)
    include = 'INCLUDE "%s"' % stats_rel
    if adding:
        # BaseStats runs dex 1..150 and stops: Mew (dex 151) is fetched from its
        # own bank by GetMonHeader, so its row is simply missing. Adding dex 152
        # needs that hole filled or our row would land on Mew's slot. Mew's own
        # base stats file is the natural filler.
        assert_line = tree.find(rel, r"assert_table_length", "the BaseStats table assert")
        mew_include = 'INCLUDE "data/pokemon/base_stats/mew.asm"'
        insert = []
        if not any(line.startswith(mew_include) for line in lines):
            insert.append(mew_include + " ; fills Mew's dex 151 slot (read from its own bank)")
        if not any(line.startswith(include) for line in lines):
            insert.append(include)
        if insert:
            tree.insert(rel, assert_line, insert, why="base stats include, in dex order")
        want_assert = "\tassert_table_length NUM_POKEMON"
        if lines[tree.find(rel, r"assert_table_length")].strip() != want_assert.strip():
            tree.set_line(rel, tree.find(rel, r"assert_table_length"), want_assert,
                          why="Mew's slot is filled now, so no discount")
    else:
        old = pk.base_stats_files.get(mon.dex_const)
        if not old:
            raise InjectError("could not find the base stats file for %s" % mon.dex_const)
        i = tree.find(rel, re.escape('INCLUDE "%s"' % old), "the %s include" % old)
        tree.set_line(rel, i, include, why="swap the replaced species' base stats")

    # MonsterPalettes is offset by one because its first row is MissingNo.
    rel = "data/pokemon/palettes.asm"
    rows = Tree.rows(tree.lines(rel), r"db\s+PAL_", stop=r"assert_table_length")
    entry = "\tdb %-12s ; %s" % (mon.palette, mon.name)
    if dex_number < len(rows):
        tree.set_line(rel, rows[dex_number], entry, why="palette")
    else:
        tree.insert(rel, rows[-1] + 1, [entry], why="palette for dex %d" % dex_number)

    rel = "data/pokemon/menu_icons.asm"
    rows = Tree.rows(tree.lines(rel), r"nybble\b", stop=r"end_nybble_array")
    entry = "\tnybble %-14s ; %s" % (mon.icon, mon.label)
    if dex_number - 1 < len(rows):
        tree.set_line(rel, rows[dex_number - 1], entry, why="party menu icon")
    else:
        tree.insert(rel, rows[-1] + 1, [entry], why="party menu icon for dex %d" % dex_number)


def patch_pics(tree, mon):
    front_rel = "gfx/pokemon/front/%s.png" % mon.file_base
    back_rel = "gfx/pokemon/back/%sb.png" % mon.file_base
    tree.add_copy(front_rel, mon.front_png, why="front sprite, built to .2bpp then .pic by the Makefile")
    tree.add_copy(back_rel, mon.back_png, why="back sprite")

    rel = "gfx/pics.asm"
    lines = tree.lines(rel)
    front_label = "%sPicFront::" % mon.label
    if not any(line.startswith(front_label) for line in lines):
        block = []
        if PIC_SECTION not in lines:
            block += [PIC_SECTION, ""]
        block += [
            '%-21s INCBIN "%s.pic"' % (front_label, "gfx/pokemon/front/" + mon.file_base),
            '%-21s INCBIN "%s.pic"' % ("%sPicBack::" % mon.label, "gfx/pokemon/back/" + mon.file_base + "b"),
        ]
        tree.append_block(rel, block, why="floating pic section (no retail Pics bank has room)")

    # Teach the index based bank picker about the new species.
    rel = "home/pics.asm"
    lines = tree.lines(rel)
    if not any("BANK(%sPicFront)" % mon.label in line for line in lines):
        anchor = tree.find(rel, r"^\tcp FOSSIL_KABUTOPS", "the pic bank special cases")
        tree.insert(rel, anchor - 1, [
            "\tld a, b",
            "\tcp %s ; injected: %s" % (mon.const, mon.name),
            "\tld a, BANK(%sPicFront)" % mon.label,
            "\tjr z, .GotBank",
        ], why="pic bank special case (7 bytes of ROM0)")


def patch_wild(tree, mon, games):
    for entry in mon.wild:
        rel = "data/wild/maps/%s.asm" % entry["map"]
        lines = tree.lines(rel)
        kind = entry["kind"]
        start = tree.find(rel, r"def_%s_wildmons" % kind, "the %s table of %s" % (kind, entry["map"]))
        rate = int(re.search(r"def_%s_wildmons\s+(\d+)" % kind, lines[start]).group(1))
        if rate == 0:
            raise InjectError(
                "%s has no %s encounters (encounter rate 0), so there are no slots to replace. "
                "Pick another map or another kind." % (entry["map"], kind)
            )
        end = tree.find(rel, r"end_%s_wildmons" % kind)
        rows, cond = [], None
        for i in range(start + 1, end):
            s = lines[i].strip()
            m = re.match(r"IF DEF\(_(RED|BLUE)\)", s)
            if m:
                cond = m.group(1).lower()
                continue
            if s == "ENDC":
                cond = None
                continue
            if re.match(r"db\s+\d+\s*,\s*[A-Z0-9_]+", s):
                rows.append((i, cond))
        targets = set()
        for game in games:
            seq = [i for i, c in rows if c in (None, game)]
            if len(seq) != 10:
                raise InjectError(
                    "%s's %s table has %d slots for %s, expected 10 (pokered may have changed)"
                    % (entry["map"], kind, len(seq), game)
                )
            targets.update(seq[s] for s in entry["slots"])
        for i in sorted(targets):
            tree.set_line(rel, i, "\tdb %2d, %s" % (entry["level"], mon.const),
                          why="%s %s slot" % (entry["map"], kind))


def inject(tree, pk, mon, replace, games):
    adding = replace is None
    if adding:
        index = patch_species_constant(tree, pk, mon)
        dex_number, num_pokemon = patch_dex_constant(tree, pk, mon)
    else:
        index = pk.index_of(replace)
        if index is None:
            raise InjectError(
                "--replace: unknown species %s.%s" % (replace, suggest(replace, pk.species_by_name))
            )
        if replace in UNREPLACEABLE:
            raise InjectError("--replace %s is not supported: %s" % (replace, UNREPLACEABLE[replace]))
        rows = Tree.rows(tree.lines("data/pokemon/dex_order.asm"), r"db\b", stop=r"assert_table_length")
        m = re.search(r"db\s+(DEX_[A-Z0-9_]+)", tree.lines("data/pokemon/dex_order.asm")[rows[index - 1]])
        if not m:
            raise InjectError("--replace %s: that index has no Pokedex number" % replace)
        mon.dex_const = m.group(1)
        mon.const = replace
        dex_number = pk.dex_by_name[mon.dex_const]
        num_pokemon = pk.num_pokemon()

    patch_indexed_tables(tree, pk, mon, index)
    patch_dex_indexed_tables(tree, pk, mon, dex_number, adding)
    patch_pics(tree, mon)
    patch_wild(tree, mon, games)
    return index, dex_number, num_pokemon


# ---------------------------------------------------------------------------
# git and make
# ---------------------------------------------------------------------------

def git(root, *args, check=True):
    result = subprocess.run(["git", "-C", str(root)] + list(args),
                            capture_output=True, text=True)
    if check and result.returncode != 0:
        raise InjectError("git %s failed: %s" % (" ".join(args), result.stderr.strip()))
    return result.stdout


def is_build_artifact(path):
    return path.endswith(BUILD_ARTIFACT_SUFFIXES) or path.startswith("tools/")


def check_clean(root, reset):
    if not (root / ".git").exists() and not (root / ".git").is_file():
        print("note: %s is not a git checkout, skipping the clean tree check" % root)
        return
    if reset:
        git(root, "checkout", "--", ".")
        git(root, "clean", "-fdx")
        print("reset: pokered restored to HEAD")
        return
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                            capture_output=True, text=True)
    if status.returncode != 0:
        print("note: could not read git status in %s (%s), skipping the clean tree check"
              % (root, status.stderr.strip().splitlines()[-1] if status.stderr.strip() else "unknown"))
        return
    dirty = []
    for line in status.stdout.splitlines():
        path = line[3:].strip().strip('"')
        if not is_build_artifact(path):
            dirty.append(line.rstrip())
    if dirty:
        raise InjectError(
            "the pokered submodule has uncommitted changes:\n  %s\n"
            "Re-run with --reset to restore it to HEAD first (that discards those changes), "
            "or commit them yourself." % "\n  ".join(dirty[:20])
        )


def build(root, games, out_dir, mon):
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for game in games:
        rom = "pokeblue.gbc" if game == "blue" else "pokered.gbc"
        print("\nbuilding %s ..." % rom)
        result = subprocess.run(["make", "-j", rom], cwd=str(root))
        if result.returncode != 0:
            raise InjectError(
                "make %s failed. The output above is rgbasm's or rgblink's. A "
                '"section overflows" error means the bank ran out of room; a '
                '"redefined symbol" error means a name in mon.json clashes with a '
                "pokered constant." % rom
            )
        for src, ext in ((rom, ".gbc"), (rom.replace(".gbc", ".sym"), ".sym")):
            src_path = root / src
            if src_path.is_file():
                dest = out_dir / ("%s_%s%s" % (mon.file_base, game, ext))
                shutil.copyfile(src_path, dest)
                made.append(dest)
    return made


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Inject a custom Pokemon species into pokered and build the ROM.")
    p.add_argument("spec", help="path to a mon.json spec")
    p.add_argument("--game", choices=("blue", "red", "both"), default="blue",
                   help="which ROM to build (default: blue)")
    p.add_argument("--pokered", default="pokered", help="path to the pokered checkout (default: ./pokered)")
    p.add_argument("--out", default="out", help="where to put the ROM and sym file (default: ./out)")
    p.add_argument("--replace", metavar="SPECIES",
                   help="overwrite an existing species (for example PIDGEY) instead of adding a new one")
    p.add_argument("--reset", action="store_true",
                   help="restore the pokered checkout to HEAD before injecting")
    p.add_argument("--dry-run", action="store_true", help="print the planned edits and stop")
    p.add_argument("--diff", action="store_true", help="with --dry-run, also print a unified diff")
    p.add_argument("--no-build", action="store_true", help="patch pokered but do not run make")
    return p.parse_args(argv)


def run(argv):
    args = parse_args(argv)
    games = ("blue", "red") if args.game == "both" else (args.game,)

    spec_path = Path(args.spec).resolve()
    if not spec_path.is_file():
        raise InjectError("no such spec file: %s" % spec_path)
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InjectError("%s is not valid JSON: %s" % (spec_path, exc))
    if not isinstance(spec, dict):
        raise InjectError("%s should contain a JSON object" % spec_path)

    root = Path(args.pokered).resolve()
    if not root.is_dir():
        raise InjectError(
            "no pokered checkout at %s. Run: git submodule update --init" % root)

    if not args.dry_run:
        check_clean(root, args.reset)
    elif args.reset:
        print("note: --dry-run ignores --reset, nothing is restored")

    tree = Tree(root)
    pk = Pokered(tree)
    replace = args.replace.strip().upper() if args.replace else None
    mon = Mon(spec, spec_path, pk, replace)

    front_size = validate_sprite(mon.front_png, "front")
    validate_sprite(mon.back_png, "back")

    index, dex_number, num_pokemon = inject(tree, pk, mon, replace, games)

    print("%s: %s" % ("replacing " + replace if replace else "adding", mon.name))
    print("  species constant   %s (internal index $%02X)" % (mon.const, index))
    if mon.const_auto_renamed:
        print("                     (renamed from %s, which pokered already uses for another "
              "constant)" % mon.const[:-4])
    print("  pokedex number     %s = %d" % (mon.dex_const, dex_number))
    print("  types              %s / %s" % (mon.types[0], mon.types[1]))
    print("  front sprite       %dx%d" % (front_size, front_size))
    print("  wild encounters    %s" % (", ".join(
        "%s %s slots %s at level %d" % (w["map"], w["kind"],
                                        ",".join(str(s) for s in w["slots"]), w["level"])
        for w in mon.wild) or "none"))
    print("\nplanned edits to %s:" % root)
    tree.print_plan(show_diff=args.diff)

    if dex_number > 152:
        print("\nwarning: dex number %d makes wPokedexOwned and wPokedexSeen one byte "
              "longer, which shifts the save layout. Old saves will not load." % dex_number)

    if args.dry_run:
        print("\ndry run: nothing was written")
        return 0

    changed = sum(1 for rel, lines in tree._lines.items()
                  if "\n".join(lines) != tree._orig[rel])
    tree.commit()
    print("\npatched %d files, wrote %d new files, copied %d sprites"
          % (changed, len(tree.new_files), len(tree.copies)))

    if args.no_build:
        print("--no-build: stopping before make")
        return 0

    made = build(root, games, Path(args.out).resolve(), mon)
    print()
    for path in made:
        print("  %s" % path)
    print("\nDone. Load the .gbc in an emulator, walk into the grass and meet %s." % mon.name)
    return 0


def main(argv=None):
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except InjectError as exc:
        print("\nerror: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
