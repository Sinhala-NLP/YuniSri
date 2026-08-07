#!/usr/bin/env python3
"""
yunisri.fix_pdf_unicode
=======================

Make a "broken-encoding" PDF (Sinhala / Tamil / etc. that pastes as garbage)
copy-paste-able, WITHOUT OCR and WITHOUT changing the visual appearance.

Quick start
-----------
    import yunisri
    yunisri.fix_pdf("hansard.pdf")            # -> writes output.pdf
    yunisri.fix_pdf("hansard.pdf", "clean.pdf")

The reference fonts (Iskoola Pota, Latha, Dinamina, Arial Unicode MS ...) are
bundled inside the package, so you do NOT pass a fonts directory. The optional
per-glyph and per-word correction dictionaries (fixes.json / wordfixes.json)
that ship with the package are applied automatically too.

Why this is needed
------------------
Many Sri-Lankan government PDFs (e.g. Hansard) embed *subsetted* legacy fonts
and ship a corrupt `/ToUnicode` map. The glyphs draw correctly, but the text
you copy is wrong. Inside the file:

  * the embedded subset's `cmap` covers only Latin,
  * glyph names are opaque ("glyph00123"),
  * `GSUB`/`GPOS` are stripped,
  * `CIDToGIDMap` is Identity (so CID == GID).

=> The correct letters survive ONLY as glyph outlines. To recover them without
   OCR you must match each embedded glyph outline back to a *real* Unicode font.
   That exact geometric match is "direct character conversion", not OCR.

How it fixes the file (visuals never change)
--------------------------------------------
It never touches the glyph-drawing operators or the font programs, so rendering
is byte-for-byte identical. It only:

  1. rewrites each font's `/ToUnicode` CMap (fixes per-glyph copy), and
  2. wraps every text-show with a `/Span <</ActualText(...)>> BDC ... EMC`
     marked-content sequence carrying the correct, logically-ordered Unicode
     (fixes vowel-sign REORDERING, which `/ToUnicode` alone cannot do).

CLI
---
    yunisri fix       PDF [--out output.pdf]        # one-shot, bundled fonts
    yunisri analyze   PDF
    yunisri buildmap  PDF --fonts DIR [--out map.json]
    yunisri apply     PDF --map map.json [--out fixed.pdf]
    yunisri selftest  PDF
    yunisri report    PDF [--map map.json]
    yunisri diagnose  PDF --page N

Dependencies:  pip install pikepdf fonttools
(the report/diagnose commands additionally need pillow + freetype-py)
"""

import argparse, json, os, sys, unicodedata
import importlib.resources as _ir
from collections import defaultdict

import pikepdf
from pikepdf import Name, String, Dictionary, Operator, ContentStreamInstruction

# Package name, used to locate bundled fonts/*.json even when this module is run
# directly as a script (where __package__ is empty).
_PKG = __package__ or "yunisri"

try:                               # legacy FM/DL simple-font recovery (mode 2)
    from . import legacy as _legacy
except ImportError:                # running as a bare script
    import legacy as _legacy

try:                               # 2006 GDI/MSTT high-byte fonts (mode 3)
    from . import gdi as _gdi
except ImportError:
    import gdi as _gdi

__all__ = ["fix_pdf", "build_map", "apply_map", "apply_legacy", "apply_gdi"]


# -----------------------------------------------------------------------------
# bundled-resource loaders  (fonts folder + fixes/wordfixes are shipped in-pkg)
# -----------------------------------------------------------------------------
def _bundled_fonts_dir():
    """Absolute path to the fonts/ directory shipped inside the package."""
    return str(_ir.files(_PKG) / "fonts")

def _bundled_json(name):
    """Load a JSON file shipped inside the package, or None if absent."""
    try:
        p = _ir.files(_PKG) / name
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        pass
    return None


# -----------------------------------------------------------------------------
# subset-tag stripping ("ABCDEF+IskoolaPota" -> "IskoolaPota")
# -----------------------------------------------------------------------------
import re as _re
_STRIP_SUBSET = _re.compile(r"^[A-Z]{6}\+").sub
def _fontname(bf):
    return _STRIP_SUBSET("", str(bf).lstrip("/"))


# -----------------------------------------------------------------------------
# Indic reordering
# -----------------------------------------------------------------------------
# In these legacy encodings, the LEFT-side ("pre-base") vowel signs are stored
# as their own glyph placed BEFORE the consonant (visual order). Unicode wants
# them AFTER the consonant (logical order). ToUnicode is per-glyph and cannot
# reorder across glyphs; ActualText (a whole run) can, so we reorder here.

SINHALA_PREBASE = {"\u0DD9", "\u0DDA", "\u0DDB"}           # ෙ ේ ෛ
# two-part signs whose visual left-part is the ෙ shape:
SINHALA_TWOPART = {"\u0DDC", "\u0DDD", "\u0DDE"}           # ො ෝ ෞ
TAMIL_PREBASE   = {"\u0BC6", "\u0BC7", "\u0BC8"}           # ெ ே ை
TAMIL_TWOPART   = {"\u0BCA", "\u0BCB", "\u0BCC"}           # ொ ோ ௌ

def _is_sinhala_consonant(ch):  return "\u0D9A" <= ch <= "\u0DC6"
def _is_tamil_consonant(ch):    return "\u0B95" <= ch <= "\u0BB9"

def reorder_logical(text):
    """Move standalone pre-base vowel signs to just after their base consonant.

    Handles the common case where the pre-base sign is a separate character that
    landed before the consonant. Two-part signs decomposed into a leading
    pre-base component are recombined. Anything it doesn't recognise is passed
    through untouched.
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in SINHALA_PREBASE or ch in SINHALA_TWOPART:
            cons = _consume_consonant(text, i + 1, _is_sinhala_consonant)
            if cons is not None:
                base, j = cons
                out.append(base)          # consonant (+ its virama cluster) first
                out.append(ch)            # then the reordered vowel sign
                i = j
                continue
        if ch in TAMIL_PREBASE or ch in TAMIL_TWOPART:
            cons = _consume_consonant(text, i + 1, _is_tamil_consonant)
            if cons is not None:
                base, j = cons
                out.append(base)
                out.append(ch)
                i = j
                continue
        out.append(ch)
        i += 1
    return unicodedata.normalize("NFC", "".join(out))

def _consume_consonant(text, i, is_cons):
    """Return (consonant-cluster-string, next_index) starting at i, or None."""
    if i >= len(text) or not is_cons(text[i]):
        return None
    start = i
    i += 1
    # absorb a trailing virama+consonant cluster so the sign lands after it
    while i + 1 < len(text) and text[i] in ("\u0DCA", "\u0BCD") and is_cons(text[i + 1]):
        i += 2
    return text[start:i], i


# -----------------------------------------------------------------------------
# ToUnicode CMap writer
# -----------------------------------------------------------------------------
def build_tounicode_stream(pdf, cid_to_uni):
    """Return a pikepdf Stream holding a /ToUnicode CMap for {int cid: str}."""
    L = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
         "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
         "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
         "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange"]
    items = sorted(cid_to_uni.items())
    for k in range(0, len(items), 100):                    # bfchar blocks <=100
        chunk = items[k:k + 100]
        L.append(f"{len(chunk)} beginbfchar")
        for cid, u in chunk:
            hexu = "".join(f"{ord(c):04X}" for c in u) or "FFFD"
            L.append(f"<{cid:04X}> <{hexu}>")
        L.append("endbfchar")
    L += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return pdf.make_stream("\n".join(L).encode("latin-1"))

def build_tounicode_stream_simple(pdf, byte_to_uni):
    """/ToUnicode CMap for a SIMPLE (1-byte code) font: {int code: str}.

    Used for legacy FM/DL fonts. This gives per-glyph copy in visual order for
    tools that ignore ActualText; the injected ActualText carries the correctly
    reordered text and is the primary fix.
    """
    L = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
         "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
         "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
         "1 begincodespacerange", "<00> <FF>", "endcodespacerange"]
    items = sorted(byte_to_uni.items())
    for k in range(0, len(items), 100):
        chunk = items[k:k + 100]
        L.append(f"{len(chunk)} beginbfchar")
        for code, u in chunk:
            hexu = "".join(f"{ord(c):04X}" for c in u) or "FFFD"
            L.append(f"<{code:02X}> <{hexu}>")
        L.append("endbfchar")
    L += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return pdf.make_stream("\n".join(L).encode("latin-1"))


# -----------------------------------------------------------------------------
# Font inventory
# -----------------------------------------------------------------------------
def iter_font_objects(pdf):
    """Yield (basefont_str, font_dict) for every unique font object in the PDF."""
    seen = set()
    def scan(resources):
        if resources is None or "/Font" not in resources:
            return
        for _, f in resources["/Font"].items():
            key = f.objgen if hasattr(f, "objgen") else id(f)
            if key in seen:
                continue
            seen.add(key)
            base = str(f.get("/BaseFont", "/Unknown"))
            yield base, f
    for page in pdf.pages:
        yield from scan(page.get("/Resources"))
        # also look inside form XObjects
        xobjs = (page.get("/Resources") or {}).get("/XObject", {})
        for _, xo in (xobjs.items() if xobjs else []):
            yield from scan(xo.get("/Resources"))

def font_is_type0_identity(f):
    return (str(f.get("/Subtype")) == "/Type0"
            and str(f.get("/Encoding")) == "/Identity-H")

def _objkey(f):
    """A stable per-OBJECT key. A single PDF can embed several distinct subsets
    under the *same* BaseFont name (e.g. two 'ABCDEE+Iskoola Pota' objects with
    different glyph orders); keying maps by name would let one clobber the other,
    so we key by the PDF object id instead."""
    og = getattr(f, "objgen", None)
    if og and og != (0, 0):
        return ("obj", int(og[0]), int(og[1]))
    return ("id", id(f))


# -----------------------------------------------------------------------------
# analyze
# -----------------------------------------------------------------------------
def cmd_analyze(args):
    pdf = pikepdf.open(args.pdf)
    print(f"{args.pdf}: {len(pdf.pages)} pages\n")
    print(f"{'BaseFont':38} {'Subtype':16} {'Enc':12} {'ToUni':6} {'Mode':10}")
    print("-" * 86)
    n_type0 = n_legacy = 0
    for base, f in iter_font_objects(pdf):
        sub = str(f.get("/Subtype", ""))
        enc = f.get("/Encoding")
        enc = str(enc) if isinstance(enc, (pikepdf.Name, str)) else (
              str(enc.get("/BaseEncoding", "dict")) if isinstance(enc, pikepdf.Dictionary) else str(enc))
        tu = "yes" if "/ToUnicode" in f else "NO"
        if font_is_type0_identity(f):
            mode = "1:outline"; n_type0 += 1
        elif font_is_legacy_simple(f):
            mode = "2:legacy"; n_legacy += 1
        else:
            mode = "-"
        print(f"{base[:38]:38} {sub[:16]:16} {enc[:12]:12} {tu:6} {mode:10}")
    print()
    if n_type0:
        print(f"  mode 1 (Type0/Identity-H outline recovery): {n_type0} font(s)")
    if n_legacy:
        print(f"  mode 2 (legacy FM/DL transliteration):       {n_legacy} font(s)")
    if not (n_type0 or n_legacy):
        print("  no recoverable Sinhala/Tamil fonts detected.")
    print("\nNote: a present ToUnicode does NOT mean it is correct. Both a corrupt "
          "\nHansard ToUnicode and a legacy WinAnsi one copy as garbage; fix_pdf "
          "\nhandles whichever mode(s) apply.")


# -----------------------------------------------------------------------------
# buildmap  (non-OCR: glyph-outline matching against reference fonts)
# -----------------------------------------------------------------------------
_SIG_EM = 1000     # normalise every glyph to a 1000-unit em
_SIG_Q  = 2        # quantise coords to this grid (absorbs tiny version diffs)

def _glyph_signature(glyph_set, name, upm):
    """A hashable, scale/position-normalised signature of a glyph's outline.

    Coordinates are translated to the origin and scaled to a common em, then
    lightly quantised. This makes the match independent of units-per-em and
    tolerant of sub-pixel rounding differences between font *versions*, while
    the pen-op sequence + point pattern keep it discriminative.
    """
    from fontTools.pens.recordingPen import RecordingPen
    pen = RecordingPen()
    glyph_set[name].draw(pen)
    pts = []
    for _, args_ in pen.value:
        for p in args_:
            if isinstance(p, tuple) and len(p) == 2:
                pts.append(p)
    if not pts:
        return None
    minx = min(x for x, _ in pts); miny = min(y for _, y in pts)
    s = _SIG_EM / (upm or 1000)
    def q(v):
        return int(round(v * s / _SIG_Q)) * _SIG_Q
    norm = tuple((q(x - minx), q(y - miny)) for x, y in pts)
    ops = tuple(op for op, _ in pen.value)
    return (ops, norm)

def _upm(ttf):
    try:
        return ttf["head"].unitsPerEm
    except Exception:
        return 1000

def _is_blank_space(ttf, gname):
    """True if the glyph has no outline but advances the pen (i.e. a space)."""
    try:
        aw, _ = ttf["hmtx"][gname]
        if aw <= 0:
            return False
        g = ttf["glyf"][gname]
        return getattr(g, "numberOfContours", 0) == 0
    except Exception:
        return False

def _reference_signatures(ttf):
    """Map outline-signature -> reference glyph name, for a reference TTFont."""
    gs = ttf.getGlyphSet()
    upm = _upm(ttf)
    sigs = {}
    for name in ttf.getGlyphOrder():
        try:
            sig = _glyph_signature(gs, name, upm)
        except Exception:
            sig = None
        if sig is not None:
            sigs.setdefault(sig, name)
    return sigs

def _reverse_cmap(ttf):
    rev = defaultdict(list)
    try:
        cmap = ttf.getBestCmap()
    except Exception:
        cmap = None
    for uni, gname in (cmap or {}).items():
        rev[gname].append(uni)
    return rev

def _reverse_gsub(ttf):
    """Best-effort reverse GSUB: output glyph -> tuple(input glyph names).

    Covers single (type 1), multiple (2), alternate (3) and ligature (4) subs,
    which is enough to decompose most conjunct / positional Indic glyphs back
    to their base glyph sequence.
    """
    out = {}
    if "GSUB" not in ttf:
        return out
    gsub = ttf["GSUB"].table
    if not gsub.LookupList:
        return out
    for lookup in gsub.LookupList.Lookup:
        for st in lookup.SubTable:
            t = lookup.LookupType
            try:
                if t == 1:  # single
                    for i, o in st.mapping.items():
                        out.setdefault(o, (i,))
                elif t == 2:  # multiple
                    for i, seq in st.mapping.items():
                        # reverse is 1->many; store the many as key -> ... skip
                        pass
                elif t == 3:  # alternate
                    for i, alts in st.alternates.items():
                        for a in alts:
                            out.setdefault(a, (i,))
                elif t == 4:  # ligature
                    for first, ligs in st.ligatures.items():
                        for lig in ligs:
                            out.setdefault(lig.LigGlyph, tuple([first] + list(lig.Component)))
            except Exception:
                continue
    return out

_UNINAME = _re.compile(r"^u(?:ni)?([0-9A-Fa-f]{4,6})$")
def _unicode_from_glyphname(gname):
    """Recover Unicode from a glyph NAME (uniXXXX / uXXXXXX / concatenated
    uniXXXXYYYY ligature names). Subsetters that keep real glyph names leave the
    Unicode readable here even when the cmap entry was dropped. Returns ''."""
    m = _UNINAME.match(gname)
    if m:
        cp = int(m.group(1), 16)
        return chr(cp) if cp <= 0x10FFFF else ""
    if gname.startswith("uni") and len(gname) > 7 and (len(gname) - 3) % 4 == 0:
        try:
            return "".join(chr(int(gname[3 + i:7 + i], 16))
                           for i in range(0, len(gname) - 3, 4))
        except ValueError:
            return ""
    return ""

def _gname_to_unicode(gname, rcmap, rgsub, _depth=0):
    """Resolve a reference glyph name to a Unicode string (may recurse GSUB)."""
    if _depth > 8:
        return ""
    u = _unicode_from_glyphname(gname)          # try the name itself first
    if u:
        return u
    if gname in rcmap:
        return "".join(chr(u) for u in rcmap[gname])
    if gname in rgsub:
        return "".join(_gname_to_unicode(g, rcmap, rgsub, _depth + 1) for g in rgsub[gname])
    return ""   # unknown (e.g. a pure display variant with no Unicode)


# -----------------------------------------------------------------------------
# self-recovery: build {gid: unicode} straight from an EMBEDDED subset, using
# its own glyph names + cmap + GSUB, with structural inference for the combining
# marks whose cmap entry the subsetter stripped. This makes Type0/Identity-H
# files recoverable WITHOUT any external reference fonts.
# -----------------------------------------------------------------------------
_SINH_MARKS = (0x0DCA, 0x0DCF, 0x0DD0, 0x0DD1, 0x0DD2, 0x0DD3, 0x0DD4, 0x0DD6,
               0x0DD8, 0x0DD9, 0x0DDA, 0x0DDB, 0x0DDC, 0x0DDD, 0x0DDE, 0x0DDF)
_TAM_MARKS  = (0x0BBE, 0x0BBF, 0x0BC0, 0x0BC1, 0x0BC2, 0x0BC6, 0x0BC7, 0x0BC8,
               0x0BCA, 0x0BCB, 0x0BCC, 0x0BCD)

def _embedded_selfmap(ttf, verbose_tag=None):
    """{gid: unicode} recovered from the embedded font itself, or {} if the font
    carries no Sinhala/Tamil signal (caller then relies on reference matching).

    The subsetter keeps glyph names, cmap for base letters, and the full GSUB,
    but often drops the cmap entries for the dependent vowel signs and ZWJ. Those
    survive only as opaque leaf glyphs inside GSUB ligatures. We identify them:
      * ZWJ  -> the unresolved leaf that most often follows the virama in a
                conjunct component list;
      * the dependent-vowel leaves -> the remaining post-base unresolved leaves,
                zipped in glyph-id order onto the cmap-missing marks in codepoint
                order (Windows' subsetter assigns new gids in codepoint order, so
                the two line up). Passing real reference fonts resolves these by
                exact outline instead and is definitive.
    """
    from collections import Counter
    order = ttf.getGlyphOrder()
    cmap = ttf.getBestCmap() or {}
    rc = _reverse_cmap(ttf)
    rgsub = _reverse_gsub(ttf)

    is_sinh = any(0x0D80 <= u <= 0x0DFF for u in cmap)
    is_tam  = any(0x0B80 <= u <= 0x0BFF for u in cmap)
    if not (is_sinh or is_tam):
        return {}
    virama = "\u0DCA" if is_sinh else "\u0BCD"
    cand_marks = _SINH_MARKS if is_sinh else _TAM_MARKS
    missing = sorted(u for u in cand_marks if u not in cmap)

    def named(g):
        return _unicode_from_glyphname(g)
    def base_resolvable(g):
        return bool(named(g)) or g in rc or g in rgsub

    first_pos = set()
    after_virama = Counter()
    post_marks = Counter()
    for comps in rgsub.values():
        for i, c in enumerate(comps):
            if base_resolvable(c):
                continue
            if i == 0:
                first_pos.add(c)
            elif named(comps[i - 1]) == virama:
                after_virama[c] += 1
            else:
                post_marks[c] += 1

    leaf = {}
    if after_virama:
        leaf[after_virama.most_common(1)[0][0]] = "\u200d"     # ZWJ
    vowels = sorted({g for g in post_marks if g not in leaf and g not in first_pos},
                    key=order.index)
    for g, u in zip(vowels, missing):
        leaf[g] = chr(u)

    def resolve(g, depth=0):
        if depth > 12:
            return ""
        if g in leaf:
            return leaf[g]
        u = named(g)
        if u:
            return u
        if g in rc:
            return "".join(chr(x) for x in rc[g])
        if g in rgsub:
            return "".join(resolve(x, depth + 1) for x in rgsub[g])
        return ""

    g2u = {}
    for gid, gname in enumerate(order):
        u = resolve(gname)
        if u:
            g2u[gid] = u
    if verbose_tag:
        print(f"  {verbose_tag}: self-map {len(g2u)}/{len(order)} glyphs "
              f"(inferred marks: ZWJ={'yes' if after_virama else 'no'}, "
              f"vowels={len([g for g in leaf if leaf[g]!=chr(0x200d)])})")
    return g2u

def _find_font_files(paths):
    """Accept dirs and/or individual files; return font file paths.

    Case-insensitive on extension; recurses into directories.
    """
    exts = (".ttf", ".otf", ".ttc")
    found = []
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.isfile(p):
            if p.lower().endswith(exts):
                found.append(p)
        elif os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for fn in files:
                    if fn.lower().endswith(exts):
                        found.append(os.path.join(root, fn))
        else:
            print(f"  (not found: {p})", file=sys.stderr)
    return sorted(set(found))

def _load_reference_fonts(paths, verbose=True):
    """Load each font file (including every face inside a .ttc)."""
    from fontTools.ttLib import TTFont, TTCollection
    refs = []
    files = _find_font_files(paths)
    for path in files:
        try:
            if path.lower().endswith(".ttc"):
                col = TTCollection(path)
                faces = list(col.fonts)
            else:
                faces = [TTFont(path)]
            for i, tt in enumerate(faces):
                if "glyf" not in tt:          # need outlines to match
                    continue
                tag = os.path.basename(path) + (f"#{i}" if len(faces) > 1 else "")
                refs.append((tag, tt, _reference_signatures(tt),
                             _reverse_cmap(tt), _reverse_gsub(tt)))
                if verbose:
                    print(f"  loaded reference: {tag} ({len(tt.getGlyphOrder())} glyphs, "
                          f"upm={_upm(tt)})")
        except Exception as e:
            print(f"  skip {path}: {e}", file=sys.stderr)
    return refs

def build_map(pdf, refs, verbose=False):
    """Match every embedded subset's glyph outlines against reference fonts.

    Returns {basefont_str: {int cid: unicode_str}} for each Type0/Identity-H
    font. This is the in-memory, no-I/O core behind the `buildmap` command.
    """
    from fontTools.ttLib import TTFont
    import io

    used = collect_used_cids(pdf)               # {basefont: set(cid)}
    result = {}

    # First pass: load every Type0 subset, compute its embedded self-map, and
    # index self-mapped glyph OUTLINES so a sibling subset of the SAME font that
    # had its cmap stripped can still be recovered (same outlines, known Unicode).
    subs = []
    sibling_sig = {}
    for base, f in iter_font_objects(pdf):
        if not font_is_type0_identity(f):
            continue
        d = f["/DescendantFonts"][0]
        ff = d.get("/FontDescriptor", {}).get("/FontFile2")
        if ff is None:
            continue
        try:
            sub = TTFont(io.BytesIO(bytes(ff.read_bytes())))
        except Exception:
            continue
        if "glyf" not in sub:
            continue
        selfmap = _embedded_selfmap(sub, verbose_tag=(base[:40] if verbose else None))
        subs.append((base, f, sub, selfmap))
        if selfmap:
            gs = sub.getGlyphSet(); upm = _upm(sub); order = sub.getGlyphOrder()
            for gid, u in selfmap.items():
                if not u.strip():
                    continue
                try:
                    sig = _glyph_signature(gs, order[gid], upm)
                except Exception:
                    sig = None
                if sig is not None:
                    sibling_sig.setdefault(sig, u)

    # Second pass: resolve each font's used glyphs.
    for base, f, sub, selfmap in subs:
        sub_gs = sub.getGlyphSet()
        sub_upm = _upm(sub)
        order = sub.getGlyphOrder()

        cid_map = {}
        want = used.get(base, set(range(len(order))))
        for cid in want:
            if cid >= len(order):
                continue
            gname = order[cid]
            uni = selfmap.get(cid, "")
            if not uni:
                # outline match: reference fonts first (exact, definitive), then
                # self-mapped sibling subsets of the same font.
                try:
                    sig = _glyph_signature(sub_gs, gname, sub_upm)
                except Exception:
                    sig = None
                if sig is not None:
                    for _, _tt, rsig, rcmap, rgsub in refs:
                        if sig in rsig:
                            uni = _gname_to_unicode(rsig[sig], rcmap, rgsub)
                            if uni:
                                break
                    if not uni and sig in sibling_sig:
                        uni = sibling_sig[sig]
                elif cid not in selfmap and _is_blank_space(sub, gname):
                    uni = " "     # blank-outline glyph that advances -> space
            if uni:
                cid_map[cid] = uni
        if verbose:
            matched = sum(1 for v in cid_map.values() if v.strip())
            spaces  = sum(1 for v in cid_map.values() if v == " ")
            total = len(want)
            print(f"{base[:40]:40} matched {matched}/{total} used glyphs"
                  + (f"  (+{spaces} spaces)" if spaces else ""))
        result[_objkey(f)] = cid_map
    return result

def cmd_buildmap(args):
    paths = args.fonts if isinstance(args.fonts, list) else [args.fonts]
    print(f"Searching for reference fonts in: {', '.join(paths)}")
    refs = _load_reference_fonts(paths)
    if not refs:
        print("No reference fonts found; building the map from the embedded "
              "fonts themselves (self-recovery). Pass --fonts to also resolve any "
              "glyphs the embedded subsets can't name by exact outline.")

    pdf = pikepdf.open(args.pdf)
    result = build_map(pdf, refs, verbose=True)          # keyed by object
    # serialise by BaseFont name for a portable map.json; if a name has several
    # distinct subsets, keep the richest so the file stays usable.
    key_to_base = {_objkey(f): base for base, f in iter_font_objects(pdf)
                   if font_is_type0_identity(f)}
    out = {}
    for k, m in result.items():
        base = key_to_base.get(k, str(k))
        if len(m) >= len(out.get(base, {})):
            out[base] = {str(c): v for c, v in m.items()}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"\nWrote {args.out}. Any remaining unmatched glyphs are display-only "
          "\nvariants; add the exact font version or hand-edit map.json to fix them.")


# -----------------------------------------------------------------------------
# shared: decode which CIDs each font actually shows
# -----------------------------------------------------------------------------
def collect_used_cids(pdf):
    used = defaultdict(set)
    for page in pdf.pages:
        res = page.get("/Resources")
        fonts = (res or {}).get("/Font", {})
        name_to_base = {str(n): str(ff.get("/BaseFont", "")) for n, ff in
                        (fonts.items() if fonts else [])}
        name_to_two = {str(n): font_is_type0_identity(ff) for n, ff in
                       (fonts.items() if fonts else [])}
        cur = None
        try:
            ops = list(pikepdf.parse_content_stream(page))
        except Exception:
            continue
        for instr in ops:
            op = str(instr.operator)
            if op == "Tf":
                cur = str(instr.operands[0])
            elif op in ("Tj", "TJ") and cur in name_to_base and name_to_two.get(cur):
                base = name_to_base[cur]
                for cid in _cids_from_operand(instr, op):
                    used[base].add(cid)
    return used

def _cids_from_operand(instr, op):
    cids = []
    def from_bytes(b):
        for i in range(0, len(b) - 1, 2):
            cids.append((b[i] << 8) | b[i + 1])
    if op == "Tj":
        from_bytes(bytes(instr.operands[0]))
    else:  # TJ
        for el in instr.operands[0]:
            if isinstance(el, pikepdf.String):
                from_bytes(bytes(el))
    return cids


# -----------------------------------------------------------------------------
# apply
# -----------------------------------------------------------------------------
def _has_indic(cid_map):
    """True if any recovered char is Sinhala (U+0D80-0DFF) or Tamil (U+0B80-0BFF)."""
    for v in cid_map.values():
        for ch in v:
            if "\u0B80" <= ch <= "\u0BFF" or "\u0D80" <= ch <= "\u0DFF":
                return True
    return False

def apply_map(pdf, fmap, fixes=None, wordfixes=None,
              sinhala_fix=True, actualtext=True, verbose=False):
    """Rewrite ToUnicode + inject ActualText into an open pikepdf, in place.

    `fmap`      : {basefont: {cid(int|str): unicode}}   (from build_map)
    `fixes`     : optional {fontkey: {cid: unicode}} hand-fix overrides.
                  Keys may be bare names ("IskoolaPota") or full "SUBSET+Name";
                  both normalise to the bare name so ONE fixes.json works across
                  documents (subset tags differ per file, glyph CIDs are stable).
    `wordfixes` : optional {broken_word: correct_word} whole-word overrides.
    """
    # Normalise incoming map to be keyed by OBJECT. build_map already returns
    # per-object keys; an external map.json (from the CLI) is keyed by BaseFont
    # name, so map each such name onto every object of that name.
    obj_map = {}          # objkey -> {int cid: str}
    name_by_key = {}      # objkey -> basefont str
    by_name_json = {}
    for k, m in fmap.items():
        if isinstance(k, str):
            by_name_json[k] = {int(c): v for c, v in m.items()}
    for base, f in iter_font_objects(pdf):
        if not font_is_type0_identity(f):
            continue
        k = _objkey(f)
        name_by_key[k] = base
        if k in fmap:                                   # per-object (build_map)
            obj_map[k] = {int(c): v for c, v in fmap[k].items()}
        elif base in by_name_json:                      # exact name (JSON)
            obj_map[k] = dict(by_name_json[base])
        else:                                           # subset-stripped name
            for nm, jm in by_name_json.items():
                if _fontname(nm) == _fontname(base):
                    obj_map[k] = dict(jm); break

    # merge hand-fix overrides (by normalised font name -> all matching objects)
    if fixes:
        by_name = {}
        for key, m in fixes.items():
            by_name.setdefault(_fontname(key), {}).update({int(k): v for k, v in m.items()})
        for k, base in name_by_key.items():
            if _fontname(base) in by_name:
                obj_map.setdefault(k, {}).update(by_name[_fontname(base)])

    wordfixes = wordfixes or {}

    # Only remap fonts that actually carry Indic script. Latin/symbol fonts
    # (Times, Calibri, Symbol, ...) already have a correct ToUnicode, so leaving
    # them untouched keeps English/numbers copying correctly.
    skipped = [k for k, m in obj_map.items() if not _has_indic(m)]
    for k in skipped:
        del obj_map[k]
    if skipped and verbose:
        print(f"Left {len(skipped)} non-Indic font(s) untouched (their original "
              f"text layer is already correct).")

    # 1) rewrite ToUnicode per mapped font (helps tools that ignore ActualText)
    for base, f in iter_font_objects(pdf):
        k = _objkey(f)
        if k in obj_map and font_is_type0_identity(f):
            f["/ToUnicode"] = build_tounicode_stream(pdf, obj_map[k])

    # 2) inject ActualText per text-show (fixes ordering; primary path)
    if actualtext:
        for page in pdf.pages:
            _inject_actualtext_page(page, obj_map, wordfixes, sinhala_fix=sinhala_fix)

def cmd_apply(args):
    with open(args.map, encoding="utf-8") as fh:
        fmap = json.load(fh)

    fixes = None
    if args.fixes:
        with open(args.fixes, encoding="utf-8") as fh:
            fixes = json.load(fh)

    wordfixes = None
    if args.wordfixes:
        with open(args.wordfixes, encoding="utf-8") as fh:
            wordfixes = json.load(fh)

    pdf = pikepdf.open(args.pdf)
    apply_map(pdf, fmap, fixes=fixes, wordfixes=wordfixes,
              sinhala_fix=not args.no_sinhala_heuristics,
              actualtext=not args.no_actualtext, verbose=True)
    pdf.save(args.out)
    print(f"Wrote {args.out}  (visuals identical; text now copy-paste correct)")


_SINH_TAM_WORD = None
# --- Sinhala legacy split-vowel repair (fixes the ේ/ෝ scramble generically) ---
import re as _re_mod
_SC = "[\u0D9A-\u0DC6]"                       # Sinhala consonant
_RE_EE_ADJ = _re_mod.compile("\u0DD9([\u0DDD\u0DDA])")            # ෙෝ/ෙේ -> ෝ/ේ
_RE_V_C_E  = _re_mod.compile("(" + _SC + ")\u0DCA(" + _SC + ")\u0DD9")   # C්Cෙ
_RE_E_C_OO = _re_mod.compile("(" + _SC + ")\u0DD9(" + _SC + ")\u0DDD")   # ෙCෝ
_RE_ZWJ_EE = _re_mod.compile("(" + _SC + ")\u0DDA\u200d(" + _SC + ")\u0DCA")  # Cේ‍C්
_RE_ZWJ_OO = _re_mod.compile("(" + _SC + ")\u0DDD\u200d(" + _SC + ")\u0DCA")  # Cෝ‍C්
_RE_V_C_OC = _re_mod.compile("(" + _SC + ")\u0DCA(" + _SC + ")\u0DDC")   # C්Cො

def _fix_sinhala_vowels(text):
    """Repair the split pre-base vowel scramble (ේ/ෝ/ො) that legacy Iskoola Pota
    produces. Safe: skips geminate conjuncts (ත්ත, ස්ස…) and touches nothing
    that isn't already scrambled."""
    text = _RE_EE_ADJ.sub(r"\1", text)
    def _vce(m):
        c1, c2 = m.group(1), m.group(2)
        return m.group(0) if c1 == c2 else c1 + "\u0DDA" + c2   # C්Cෙ -> Cේ C
    text = _RE_V_C_E.sub(_vce, text)
    text = _RE_E_C_OO.sub("\\1\u0DDD\\2", text)                 # ෙCෝ -> ෝ C
    text = _RE_ZWJ_EE.sub("\\1\u0DCA\u200d\\2\u0DDA", text)     # Cේ‍C් -> C්‍Cේ  (ශ්‍රේ)
    text = _RE_ZWJ_OO.sub("\\1\u0DCA\u200d\\2\u0DDD", text)     # Cෝ‍C් -> C්‍Cෝ
    def _vco(m):
        c1, c2 = m.group(1), m.group(2)
        if c1 == c2 or c1 == "\u0DB1":                          # skip geminate + න්
            return m.group(0)
        return c1 + "\u0DDA" + c2 + "\u0DCF"                    # C්Cො -> Cේ Cා (සේවා)
    text = _RE_V_C_OC.sub(_vco, text)
    return text

def _apply_wordfixes(text, wordfixes):
    if not wordfixes:
        return text
    global _SINH_TAM_WORD
    if _SINH_TAM_WORD is None:
        import re as _re
        _SINH_TAM_WORD = _re.compile(r"[\u0B80-\u0BFF\u0D80-\u0DFF\u200d]+")
    return _SINH_TAM_WORD.sub(lambda m: wordfixes.get(m.group(0), m.group(0)), text)

def _inject_actualtext_page(page, obj_map, wordfixes=None, sinhala_fix=True):
    res = page.get("/Resources")
    fonts = (res or {}).get("/Font", {})
    name_to_key = {str(n): _objkey(ff) for n, ff in
                   (fonts.items() if fonts else [])}
    name_to_two = {str(n): font_is_type0_identity(ff) for n, ff in
                   (fonts.items() if fonts else [])}
    try:
        ops = list(pikepdf.parse_content_stream(page))
    except Exception:
        return
    cur = None
    out = []
    changed = False
    for instr in ops:
        op = str(instr.operator)
        if op == "Tf":
            cur = str(instr.operands[0])
            out.append(instr); continue
        if op in ("Tj", "TJ") and name_to_two.get(cur) \
           and name_to_key.get(cur) in obj_map:
            m = obj_map[name_to_key[cur]]
            text = _run_text_with_spaces(instr, op, m)
            text = reorder_logical(text)
            text = _apply_wordfixes(text, wordfixes)   # explicit dictionary first
            if sinhala_fix:
                text = _fix_sinhala_vowels(text)        # then generic repair
                text = _apply_wordfixes(text, wordfixes)  # catch final-form keys
            if text:
                props = Dictionary(ActualText=String(text))
                out.append(ContentStreamInstruction([Name("/Span"), props], Operator("BDC")))
                out.append(instr)
                out.append(ContentStreamInstruction([], Operator("EMC")))
                changed = True
                continue
        out.append(instr)
    if changed:
        page.Contents.write(pikepdf.unparse_content_stream(out))

# A negative TJ adjustment larger (in magnitude) than this many thousandths of
# an em means a real inter-word gap, so emit a space if one isn't there already.
_GAP_THOUSANDTHS = 100

def _run_text_with_spaces(instr, op, m):
    """Logical text for a show op, mapping CIDs and inserting spaces for gaps."""
    def cids(b):
        return [(b[i] << 8) | b[i + 1] for i in range(0, len(b) - 1, 2)]
    parts = []
    if op == "Tj":
        for c in cids(bytes(instr.operands[0])):
            parts.append(m.get(c, ""))
    else:  # TJ: list of strings and kerning numbers
        for el in instr.operands[0]:
            if isinstance(el, pikepdf.String):
                for c in cids(bytes(el)):
                    parts.append(m.get(c, ""))
            else:
                if float(el) < -_GAP_THOUSANDTHS:
                    parts.append(" ")
    # join, collapsing accidental double spaces
    outc = []
    for p in parts:
        if p == " " and (not outc or outc[-1].endswith(" ")):
            continue
        outc.append(p)
    return "".join(outc)


# -----------------------------------------------------------------------------
# LEGACY FM/DL simple-font recovery   (recovery mode 2)
# -----------------------------------------------------------------------------
# The other common broken-Sinhala PDF: simple 1-byte TrueType/Type1 fonts
# (FMAbhaya, FMSamantha, DL-*, ...) that store Sinhala as Latin bytes. No
# outline puzzle -- the bytes ARE the letters in a fixed legacy layout, so
# recovery is transliteration (see legacy.py) rather than outline matching.
import re as _re_leg
_strip_subset_leg = _re_leg.compile(r"^[A-Z]{6}\+").sub
def _legacy_fontname(b):
    return _strip_subset_leg("", str(b).lstrip("/"))

def font_is_legacy_simple(f):
    """True if f is a simple (1-byte) TrueType/Type1 legacy Sinhala font."""
    if str(f.get("/Subtype")) not in ("/TrueType", "/Type1"):
        return False
    return _legacy.basefont_is_legacy(f.get("/BaseFont", ""))

def _legacy_decode(b):
    """Show-operand bytes of a simple font -> the legacy (Latin) string."""
    try:
        return b.decode("cp1252")
    except Exception:
        return b.decode("latin-1", "replace")

def _legacy_run_text(instr, op):
    """A show op's legacy text, inserting spaces for big TJ inter-word gaps."""
    parts = []
    if op == "Tj":
        parts.append(_legacy_decode(bytes(instr.operands[0])))
    else:
        for el in instr.operands[0]:
            if isinstance(el, pikepdf.String):
                parts.append(_legacy_decode(bytes(el)))
            elif float(el) < -_GAP_THOUSANDTHS:
                parts.append(" ")
    return "".join(parts)

def collect_used_bytes_legacy(pdf):
    """{basefont: set(int code)} actually shown by each legacy simple font."""
    used = defaultdict(set)
    for page in pdf.pages:
        res = page.get("/Resources"); fonts = (res or {}).get("/Font", {})
        nb = {str(n): str(ff.get("/BaseFont", "")) for n, ff in
              (fonts.items() if fonts else [])}
        leg = {str(n): font_is_legacy_simple(ff) for n, ff in
               (fonts.items() if fonts else [])}
        cur = None
        try:
            ops = list(pikepdf.parse_content_stream(page))
        except Exception:
            continue
        for instr in ops:
            o = str(instr.operator)
            if o == "Tf":
                cur = str(instr.operands[0])
            elif o in ("Tj", "TJ") and leg.get(cur):
                for ch in _legacy_run_text(instr, o):
                    if ord(ch) < 256:
                        used[nb[cur]].add(ord(ch))
    return used

def _inject_actualtext_legacy_page(page, legacy_bases, overrides=None, wordfixes=None):
    res = page.get("/Resources"); fonts = (res or {}).get("/Font", {})
    nb = {str(n): str(ff.get("/BaseFont", "")) for n, ff in
          (fonts.items() if fonts else [])}
    leg = {str(n): font_is_legacy_simple(ff) for n, ff in
           (fonts.items() if fonts else [])}
    try:
        ops = list(pikepdf.parse_content_stream(page))
    except Exception:
        return
    cur = None; out = []; changed = False
    for instr in ops:
        o = str(instr.operator)
        if o == "Tf":
            cur = str(instr.operands[0]); out.append(instr); continue
        if o in ("Tj", "TJ") and leg.get(cur) and nb.get(cur) in legacy_bases:
            legacy = _legacy_run_text(instr, o)
            text = _legacy.transliterate(legacy, overrides)
            text = _apply_wordfixes(text, wordfixes)
            if text and text != legacy:
                props = Dictionary(ActualText=String(text))
                out.append(ContentStreamInstruction([Name("/Span"), props], Operator("BDC")))
                out.append(instr)
                out.append(ContentStreamInstruction([], Operator("EMC")))
                changed = True; continue
        out.append(instr)
    if changed:
        page.Contents.write(pikepdf.unparse_content_stream(out))

def apply_legacy(pdf, overrides=None, wordfixes=None, actualtext=True, verbose=False):
    """Recover text from legacy FM/DL simple fonts, in place. Returns the number
    of legacy fonts handled (0 if the PDF has none). Non-legacy fonts (real
    Latin fonts, Type0 fonts) are left completely untouched.
    """
    legacy_bases = {b for b, f in iter_font_objects(pdf) if font_is_legacy_simple(f)}
    if not legacy_bases:
        return 0
    if verbose:
        print("Legacy FM/DL fonts: "
              + ", ".join(sorted(_legacy_fontname(b) for b in legacy_bases)))

    # 1) per-glyph ToUnicode (visual order) so even ActualText-unaware tools improve
    used = collect_used_bytes_legacy(pdf)
    for base, f in iter_font_objects(pdf):
        if base not in legacy_bases:
            continue
        codes = used.get(base, set(range(32, 256)))
        b2u = {}
        for code in codes:
            ch = bytes([code]).decode("cp1252", "replace")
            u = _legacy.transliterate(ch, overrides)      # per-glyph, no reorder
            if u and u != ch:
                b2u[code] = u
        if b2u:
            f["/ToUnicode"] = build_tounicode_stream_simple(pdf, b2u)

    # 2) ActualText per run -- the primary fix (correct, reordered text)
    if actualtext:
        for page in pdf.pages:
            _inject_actualtext_legacy_page(page, legacy_bases, overrides, wordfixes)
    return len(legacy_bases)


# -----------------------------------------------------------------------------
# GDI/MSTT simple-font recovery   (recovery mode 3)
# -----------------------------------------------------------------------------
# 2006-era Hansard PDFs: subsetted /Type1 fonts (BaseFont like "MSTT31c3d8")
# with a custom /Differences encoding of code-named glyphs (/G7F ...) and NO
# /ToUnicode. No cmap/GSUB/uni-names, so they can't self-recover; recovery is a
# byte->Unicode table + the FM reorder (see gdi.py). We only touch runs this
# table actually resolves, so Tamil / bold MSTT faces are left untouched.
_GDI_MIN_COVERAGE = 0.6      # a run must be >=60% resolvable to be rewritten

def font_is_gdi_simple(f):
    """True for a Windows-GDI 'MSTTxxxx' subset font: a simple Type1/TrueType
    font with no /ToUnicode and a custom encoding (the byte->Unicode table in
    gdi.py plus the per-run coverage guard decide what actually gets rewritten,
    so this can be permissive without risking non-Sinhala fonts)."""
    if str(f.get("/Subtype")) not in ("/TrueType", "/Type1"):
        return False
    if "/ToUnicode" in f:
        return False
    base = str(f.get("/BaseFont", ""))
    if "MSTT" in base:
        return True
    enc = f.get("/Encoding")
    if isinstance(enc, pikepdf.Dictionary) and "/Differences" in enc:
        for x in enc["/Differences"]:
            if not isinstance(x, int) and str(x).startswith("/G"):
                return True
    return False

def _gdi_run_bytes(instr, op):
    """A show op's raw bytes plus spaces for big TJ inter-word gaps (as text)."""
    parts = []
    def emit(b):
        parts.append(_gdi.transliterate(b))
    if op == "Tj":
        emit(bytes(instr.operands[0]))
    else:
        for el in instr.operands[0]:
            if isinstance(el, pikepdf.String):
                emit(bytes(el))
            elif float(el) < -_GAP_THOUSANDTHS:
                parts.append(" ")
    return "".join(parts)

def _gdi_raw_bytes(instr, op):
    b = b""
    if op == "Tj":
        b = bytes(instr.operands[0])
    else:
        for el in instr.operands[0]:
            if isinstance(el, pikepdf.String):
                b += bytes(el)
    return b

def _inject_actualtext_gdi_page(page, gdi_bases, wordfixes=None):
    res = page.get("/Resources"); fonts = (res or {}).get("/Font", {})
    nb = {str(n): str(ff.get("/BaseFont", "")) for n, ff in (fonts.items() if fonts else [])}
    isg = {str(n): font_is_gdi_simple(ff) for n, ff in (fonts.items() if fonts else [])}
    try:
        ops = list(pikepdf.parse_content_stream(page))
    except Exception:
        return
    cur = None; out = []; changed = False
    for instr in ops:
        o = str(instr.operator)
        if o == "Tf":
            cur = str(instr.operands[0]); out.append(instr); continue
        if o in ("Tj", "TJ") and isg.get(cur) and nb.get(cur) in gdi_bases:
            raw = _gdi_raw_bytes(instr, o)
            if raw.strip() and _gdi.coverage(raw) >= _GDI_MIN_COVERAGE:
                text = _gdi_run_bytes(instr, o)
                text = _apply_wordfixes(text, wordfixes)
                if text:
                    props = Dictionary(ActualText=String(text))
                    out.append(ContentStreamInstruction([Name("/Span"), props], Operator("BDC")))
                    out.append(instr)
                    out.append(ContentStreamInstruction([], Operator("EMC")))
                    changed = True; continue
        out.append(instr)
    if changed:
        page.Contents.write(pikepdf.unparse_content_stream(out))

def apply_gdi(pdf, wordfixes=None, actualtext=True, verbose=False):
    """Recover text from 2006 GDI/MSTT subset fonts, in place. Returns the number
    of such fonts whose runs were rewritten (0 if none apply).

    The gdi.py table is validated for the Sinhala MSTT face only. The Tamil and
    bold MSTT faces in the same documents reuse the same byte codes, so a byte-
    coverage test can't tell them apart -- decoding Tamil with the Sinhala table
    yields Sinhala-looking junk. So a font qualifies only if its *decoded* text
    actually reads as Sinhala (hits several common Sinhala words); everything
    else is left untouched."""
    # accumulate each candidate font's decoded text, then keep only the ones
    # that read as Sinhala.
    per_font = {}
    for page in pdf.pages:
        res = page.get("/Resources"); fonts = (res or {}).get("/Font", {})
        nb = {str(n): str(ff.get("/BaseFont", "")) for n, ff in (fonts.items() if fonts else [])}
        isg = {str(n): font_is_gdi_simple(ff) for n, ff in (fonts.items() if fonts else [])}
        cur = None
        try:
            ops = list(pikepdf.parse_content_stream(page))
        except Exception:
            continue
        for instr in ops:
            o = str(instr.operator)
            if o == "Tf":
                cur = str(instr.operands[0])
            elif o in ("Tj", "TJ") and isg.get(cur):
                raw = _gdi_raw_bytes(instr, o)
                if raw.strip() and _gdi.coverage(raw) >= _GDI_MIN_COVERAGE:
                    per_font.setdefault(nb.get(cur), []).append(_gdi.transliterate(raw))

    qualifying = {b for b, chunks in per_font.items()
                  if b and _text_reads_as_sinhala("".join(chunks))}
    if not qualifying:
        return 0
    if verbose:
        print("GDI/MSTT Sinhala fonts recovered: "
              + ", ".join(sorted(_fontname(b) for b in qualifying if b)))
    if actualtext:
        for page in pdf.pages:
            _inject_actualtext_gdi_page(page, qualifying, wordfixes)
    return len(qualifying)

# Real Sinhala decoded with the gdi.py table has almost no replacement chars and
# its dependent marks nearly always follow a consonant; Tamil/bold faces decoded
# with the same table use many bytes the table lacks (-> replacement chars) and
# produce ill-formed mark sequences. These thresholds separate the two cleanly.
_GDI_MAX_REPL = 0.12
_GDI_MIN_MARKFRAC = 0.55
_SINH_CONS = "\u0d9a-\u0dc6"
_SINH_MARK = "\u0dca-\u0ddf\u0d82\u0d83"
_RE_MARK = _re.compile("[" + _SINH_MARK + "]")
_RE_GOODMARK = _re.compile("(?<=[" + _SINH_CONS + "\u200d])[" + _SINH_MARK + "]")

def _text_reads_as_sinhala(text):
    """True if decoded text is really Sinhala (not Tamil/bold via the wrong
    table): few replacement chars, enough Sinhala letters, and dependent marks
    that mostly sit on a consonant."""
    sinh = sum(1 for c in text if "\u0d80" <= c <= "\u0dff")
    if sinh < 4:
        return False
    if text.count("\ufffd") / max(len(text), 1) > _GDI_MAX_REPL:
        return False
    marks = _RE_MARK.findall(text)
    if marks and len(_RE_GOODMARK.findall(text)) / len(marks) < _GDI_MIN_MARKFRAC:
        return False
    return True


# -----------------------------------------------------------------------------
# PUBLIC API  --  the one call most users need
# -----------------------------------------------------------------------------
def fix_pdf(input_pdf, output_pdf="output.pdf", *,
            fonts_dir=None, fixes="bundled", wordfixes="bundled",
            legacy_fixes="bundled",
            sinhala_heuristics=True, actualtext=True, verbose=False):
    """Recover copy-paste-correct text in a broken-encoding Sri-Lankan PDF.

    Handles both common failure modes automatically, in one pass:
      * mode 1 (Type0/Identity-H, "Hansard-style"): subsetted fonts with corrupt
        ToUnicode where the correct letters survive only as glyph outlines --
        recovered by matching outlines against the bundled reference fonts;
      * mode 2 (legacy FM/DL simple fonts): FMAbhaya/FMSamantha/DL-* embedded as
        simple TrueType/Type1 fonts that store Sinhala as Latin bytes -- recovered
        by transliteration (see legacy.py).

    Whichever modes apply to the file are run; the rendered pages are unchanged,
    only the invisible text layer is corrected. A file with neither kind of font
    is written back unchanged.

    Parameters
    ----------
    input_pdf   : path to the source PDF.
    output_pdf  : where to write the fixed PDF (default "output.pdf").
    fonts_dir   : override the bundled reference fonts. Accepts a directory,
                  a list of dirs/files, or None (use the packaged fonts/).
    fixes       : per-glyph override dict, a path to a JSON file, "bundled"
                  (use the packaged fixes.json, the default), or None.
    wordfixes   : whole-word override dict, a path to a JSON file, "bundled"
                  (use the packaged wordfixes.json, the default), or None.
    legacy_fixes : extra legacy-sequence -> Unicode overrides for mode 2, as a
                  dict, a JSON path, "bundled" (packaged legacy_fixes.json if
                  present, else ignored), or None. Use this to add font-specific
                  FM ligatures the built-in table doesn't know yet.
    sinhala_heuristics : apply the generic Sinhala split-vowel repair (default True).
    actualtext  : inject ActualText for reordering (default True). If False,
                  only ToUnicode is rewritten.
    verbose     : print per-font match statistics.

    Returns
    -------
    The path to the written output PDF.
    """
    # resolve correction dictionaries once (dict | path | "bundled" | None)
    fixes = _resolve_corrections(fixes, "fixes.json")
    wordfixes = _resolve_corrections(wordfixes, "wordfixes.json")
    legacy_overrides = _resolve_corrections(legacy_fixes, "legacy_fixes.json")

    pdf = pikepdf.open(input_pdf)

    # --- mode 1: Type0 / Identity-H outline recovery (Hansard-style) ----------
    # Only load reference fonts if the file actually has such fonts, so a purely
    # legacy-font PDF doesn't need them.
    has_type0 = any(font_is_type0_identity(f) for _, f in iter_font_objects(pdf))
    if has_type0:
        if fonts_dir is None:
            paths = [_bundled_fonts_dir()]
        elif isinstance(fonts_dir, (list, tuple)):
            paths = list(fonts_dir)
        else:
            paths = [fonts_dir]
        refs = _load_reference_fonts(paths, verbose=verbose)
        # Reference fonts are now OPTIONAL: most Type0 files can be recovered
        # from the embedded subset itself (glyph names + cmap + GSUB, with
        # structural inference for stripped combining marks). Reference fonts,
        # when available, only fill glyphs the embedded font can't name and are
        # resolved by exact outline. So we no longer hard-fail without them.
        if not refs and verbose:
            print("No reference fonts found; recovering from the embedded fonts "
                  "themselves. Pass fonts_dir= (e.g. C:\\Windows\\Fonts) to resolve "
                  "any glyphs the embedded subset can't name by exact outline.")
        fmap = build_map(pdf, refs, verbose=verbose)
        apply_map(pdf, fmap, fixes=fixes, wordfixes=wordfixes,
                  sinhala_fix=sinhala_heuristics, actualtext=actualtext, verbose=verbose)

    # --- mode 2: legacy FM/DL simple-font recovery ----------------------------
    n_legacy = apply_legacy(pdf, overrides=legacy_overrides, wordfixes=wordfixes,
                            actualtext=actualtext, verbose=verbose)

    # --- mode 3: 2006 GDI/MSTT high-byte simple-font recovery ------------------
    n_gdi = apply_gdi(pdf, wordfixes=wordfixes, actualtext=actualtext, verbose=verbose)

    if verbose and not has_type0 and not n_legacy and not n_gdi:
        print("No recoverable Sinhala/Tamil fonts found; output equals input.")

    pdf.save(output_pdf)
    return output_pdf

def _resolve_corrections(value, bundled_name):
    """Turn a dict / path / 'bundled' / None into a dict (or None)."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if value == "bundled":
        return _bundled_json(bundled_name)
    # otherwise treat as a filesystem path
    with open(value, encoding="utf-8") as fh:
        return json.load(fh)

def cmd_fix(args):
    out = fix_pdf(args.pdf, args.out,
                  fonts_dir=(args.fonts if args.fonts else None),
                  verbose=True)
    print(f"Wrote {out}  (visuals identical; text now copy-paste correct)")


# -----------------------------------------------------------------------------
# diagnose  (per-run glyph strips + current decoded text, to pinpoint bad CIDs)
# -----------------------------------------------------------------------------
def cmd_diagnose(args):
    import io, base64, tempfile, freetype
    from PIL import Image, ImageOps
    fmap = {}
    for src in (args.map, args.fixes):
        if src and os.path.exists(src):
            raw = json.load(open(src, encoding="utf-8"))
            for b, m in raw.items():
                fmap.setdefault(b, {}).update({int(k): v for k, v in m.items()})

    pdf = pikepdf.open(args.pdf)
    page = pdf.pages[args.page - 1]
    fonts = page.Resources["/Font"]
    nb = {str(n): str(f.get("/BaseFont", "")) for n, f in fonts.items()}
    two = {str(n): font_is_type0_identity(fonts[n]) for n in fonts}

    faces = {}
    def face_for(name):
        if name not in faces:
            d = fonts[name]["/DescendantFonts"][0]
            data = bytes(d["/FontDescriptor"]["/FontFile2"].read_bytes())
            tmp = tempfile.mktemp(suffix=".ttf"); open(tmp, "wb").write(data)
            fc = freetype.Face(tmp); fc.set_pixel_sizes(0, 48); faces[name] = fc
        return faces[name]

    def strip(name, cids):
        fc = face_for(name); imgs = []
        for c in cids:
            try:
                fc.load_glyph(c, 4); b = fc.glyph.bitmap
                g = (ImageOps.invert(Image.frombytes("L", (b.width, b.rows), bytes(b.buffer)))
                     if b.width and b.rows else Image.new("L", (14, 40), 255))
            except Exception:
                g = Image.new("L", (14, 40), 255)
            g = g.convert("RGB"); g.thumbnail((46, 46)); imgs.append((c, g))
        cell = 56
        im = Image.new("RGB", (max(1, len(imgs)) * cell, 78), "white")
        from PIL import ImageDraw; dr = ImageDraw.Draw(im)
        for i, (c, g) in enumerate(imgs):
            im.paste(g, (i * cell + 4, 4)); dr.text((i * cell + 2, 58), str(c), fill="red")
        buf = io.BytesIO(); im.save(buf, "PNG"); return base64.b64encode(buf.getvalue()).decode()

    rows = []; cur = None
    for instr in pikepdf.parse_content_stream(page):
        op = str(instr.operator)
        if op == "Tf":
            cur = str(instr.operands[0])
        elif op in ("Tj", "TJ") and two.get(cur) and _has_indic(fmap.get(nb.get(cur, ""), {})):
            base = nb[cur]; m = fmap.get(base, {})
            cids = _cids_from_operand(instr, op)
            text = reorder_logical(_run_text_with_spaces(instr, op, m))
            if cids:
                rows.append((base, strip(cur, cids), text))

    html = ["<!doctype html><meta charset=utf-8><style>body{font-family:sans-serif;padding:1rem}",
            ".r{margin:.6rem 0;border-bottom:1px solid #eee;padding-bottom:.4rem}",
            ".t{font-size:1.4rem}</style>",
            f"<h2>Diagnose page {args.page}</h2><p>Each row: the glyphs (with CID under each) "
            "and the text they currently produce. Find a wrong word, read the CID(s), and tell "
            "me what it should be — I'll add it to fixes.json.</p>"]
    for base, b64, text in rows:
        html.append(f"<div class=r><div class=t>{text}</div>"
                    f"<img src='data:image/png;base64,{b64}'><br><small>{base}</small></div>")
    open(args.out, "w", encoding="utf-8").write("\n".join(html))
    print(f"Wrote {args.out}: {len(rows)} runs on page {args.page}.")


# -----------------------------------------------------------------------------
# report  (visual contact sheet of unmatched glyphs, to hand-complete the map)
# -----------------------------------------------------------------------------
def _parse_existing_tounicode(f):
    import re
    if "/ToUnicode" not in f:
        return {}
    data = f["/ToUnicode"].read_bytes().decode("latin-1")
    m = {}
    for a, b in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>(?!\s*<)", data):
        try:
            m[int(a, 16)] = "".join(chr(int(b[i:i+4], 16)) for i in range(0, len(b), 4))
        except Exception:
            pass
    return m

def _render_glyph_b64(face, gid, px=44):
    import io, base64
    from PIL import Image
    try:
        face.load_glyph(gid, 0x0)          # FT_LOAD_DEFAULT
        face.load_glyph(gid, 4)            # FT_LOAD_RENDER
    except Exception:
        return None
    bmp = face.glyph.bitmap
    if bmp.width == 0 or bmp.rows == 0:
        return None
    img = Image.frombytes("L", (bmp.width, bmp.rows), bytes(bmp.buffer))
    from PIL import ImageOps
    img = ImageOps.invert(img)
    canvas = Image.new("L", (px + 12, px + 12), 255)
    img.thumbnail((px, px))
    canvas.paste(img, ((canvas.width - img.width) // 2, (canvas.height - img.height) // 2))
    buf = io.BytesIO(); canvas.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()

def cmd_report(args):
    import tempfile, freetype
    fmap = {}
    if args.map and os.path.exists(args.map):
        raw = json.load(open(args.map, encoding="utf-8"))
        fmap = {b: {int(k): v for k, v in m.items()} for b, m in raw.items()}

    pdf = pikepdf.open(args.pdf)
    used = collect_used_cids(pdf)
    rows = []
    for base, f in iter_font_objects(pdf):
        if not font_is_type0_identity(f) or base not in used:
            continue
        m = fmap.get(base, {})
        unmatched = sorted(c for c in used[base] if not m.get(c, "").strip())
        if not unmatched:
            continue
        d = f["/DescendantFonts"][0]
        ff = d.get("/FontDescriptor", {}).get("/FontFile2")
        if ff is None:
            continue
        tmp = tempfile.mktemp(suffix=".ttf")
        open(tmp, "wb").write(bytes(ff.read_bytes()))
        face = freetype.Face(tmp); face.set_pixel_sizes(0, 40)
        hint = _parse_existing_tounicode(f)
        for cid in unmatched:
            b64 = _render_glyph_b64(face, cid)
            if b64 is None:            # blank/space etc. - not a real glyph to fix
                continue
            rows.append((base, cid, b64, hint.get(cid, "")))

    html = ["<!doctype html><meta charset=utf-8><style>",
            "body{font-family:sans-serif;padding:1rem}",
            "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:6px 10px;text-align:center}",
            "img{image-rendering:pixelated}code{color:#a00}</style>",
            "<h2>Unmatched glyphs to fix</h2>",
            "<p>For each row, read the glyph, then add <code>\"CID\": \"CORRECT_UNICODE\"</code> "
            "under that BaseFont in your map.json and re-run <code>apply</code>. "
            "The hint is the OLD broken text for that glyph (often close-ish).</p>",
            "<table><tr><th>BaseFont</th><th>CID</th><th>glyph</th><th>broken hint</th><th>correct?</th></tr>"]
    for base, cid, b64, hint in rows:
        html.append(f"<tr><td>{base}</td><td>{cid}</td>"
                    f"<td><img src='data:image/png;base64,{b64}'></td>"
                    f"<td>{hint}</td><td></td></tr>")
    html.append("</table>")
    open(args.out, "w", encoding="utf-8").write("\n".join(html))
    print(f"Wrote {args.out}: {len(rows)} unmatched glyphs to review.")
    from collections import Counter
    per = Counter(r[0] for r in rows)
    for b, n in per.most_common():
        print(f"  {b[:40]:40} {n} unmatched")


# -----------------------------------------------------------------------------
# selftest  (mechanism proof on the real file)
# -----------------------------------------------------------------------------
def cmd_selftest(args):
    import hashlib, subprocess, tempfile
    pdf = pikepdf.open(args.pdf)
    # probe: map first used cid of first Type0 font to a marker string
    used = collect_used_cids(pdf)
    target = None
    for base, f in iter_font_objects(pdf):
        if base in used and used[base] and font_is_type0_identity(f):
            target = (base, sorted(used[base])[0]); break
    if not target:
        sys.exit("No Type0 font with text found.")
    base, cid = target
    fmap = {base: {cid: "\u0DA4\u0DBB\u0DCA\u0DBD\u0DD2[PROBE]"}}
    for b, f in iter_font_objects(pdf):
        if b == base:
            f["/ToUnicode"] = build_tounicode_stream(pdf, fmap[base])
    for page in pdf.pages:
        _inject_actualtext_page(page, fmap)
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "probe.pdf"); pdf.save(out)
    def render(p, tag):
        subprocess.run(["pdftoppm", "-png", "-r", "100", "-f", "5", "-l", "5", p,
                        os.path.join(tmp, tag)], check=True)
        return hashlib.md5(open(os.path.join(tmp, tag + "-05.png"), "rb").read()).hexdigest()
    same = render(args.pdf, "o") == render(out, "m")
    txt = subprocess.run(["pdftotext", "-f", "1", "-l", str(len(pdf.pages)), out, "-"],
                         capture_output=True, text=True).stdout
    print(f"font={base} cid={cid}")
    print("render identical  :", same)
    print("probe copyable    :", "[PROBE]" in txt)
    print("PASS" if same and "[PROBE]" in txt else "FAIL")


# -----------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="yunisri", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    # one-shot, bundled fonts -> output.pdf
    fx = sub.add_parser("fix", help="recover text using the bundled fonts -> output.pdf")
    fx.add_argument("pdf")
    fx.add_argument("--out", default="output.pdf")
    fx.add_argument("--fonts", nargs="+", default=None,
                    help="override the bundled reference fonts (dir(s)/file(s))")
    fx.set_defaults(fn=cmd_fix)

    a = sub.add_parser("analyze"); a.add_argument("pdf"); a.set_defaults(fn=cmd_analyze)

    b = sub.add_parser("buildmap"); b.add_argument("pdf")
    b.add_argument("--fonts", required=True, nargs="+",
                   help="dir(s) and/or .ttf/.otf/.ttc file(s) with the ORIGINAL fonts")
    b.add_argument("--out", default="map.json"); b.set_defaults(fn=cmd_buildmap)

    c = sub.add_parser("apply"); c.add_argument("pdf")
    c.add_argument("--map", required=True); c.add_argument("--out", default="fixed.pdf")
    c.add_argument("--fixes", help="optional JSON of hand-fixed CID->Unicode overrides")
    c.add_argument("--wordfixes", help="optional JSON of {broken_word: correct_word} whole-word fixes")
    c.add_argument("--no-sinhala-heuristics", action="store_true",
                   help="disable the generic Sinhala ේ/ෝ split-vowel repair (on by default)")
    c.add_argument("--no-actualtext", action="store_true",
                   help="only rewrite ToUnicode (skip ActualText / reordering)")
    c.set_defaults(fn=cmd_apply)

    s = sub.add_parser("selftest"); s.add_argument("pdf"); s.set_defaults(fn=cmd_selftest)

    r = sub.add_parser("report"); r.add_argument("pdf")
    r.add_argument("--map", default="map.json")
    r.add_argument("--out", default="unmatched_glyphs.html")
    r.set_defaults(fn=cmd_report)

    dg = sub.add_parser("diagnose"); dg.add_argument("pdf")
    dg.add_argument("--page", type=int, required=True)
    dg.add_argument("--map", default="map.json")
    dg.add_argument("--fixes", default="fixes.json")
    dg.add_argument("--out", default="diagnose.html")
    dg.set_defaults(fn=cmd_diagnose)

    args = ap.parse_args(argv)
    args.fn(args)

if __name__ == "__main__":
    main()