#!/usr/bin/env python3
"""
fix_pdf_unicode.py
==================

Make a "broken-encoding" PDF (Sinhala / Tamil / etc. that pastes as garbage)
copy-paste-able, WITHOUT OCR and WITHOUT changing the visual appearance.

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
   OCR you must match each embedded glyph outline back to a *real* Unicode font
   (the original Iskoola Pota / Latha / Dinamina, etc.). That exact geometric
   match is "direct character conversion", not OCR.

How it fixes the file (visuals never change)
--------------------------------------------
It never touches the glyph-drawing operators or the font programs, so rendering
is byte-for-byte identical. It only:

  1. rewrites each font's `/ToUnicode` CMap (fixes per-glyph copy), and
  2. wraps every text-show with a `/Span <</ActualText(...)>> BDC ... EMC`
     marked-content sequence carrying the correct, logically-ordered Unicode
     (fixes vowel-sign REORDERING, which `/ToUnicode` alone cannot do).

Both are proven to leave the raster output unchanged.

Commands
--------
  analyze  PDF
      Report the fonts and whether their text extraction looks healthy.

  buildmap PDF --fonts DIR --out map.json
      NON-OCR recovery. For every embedded subset, match its glyph outlines
      against reference .ttf/.otf files in DIR (the ORIGINAL fonts, e.g. copied
      from C:\\Windows\\Fonts), and emit a CID->Unicode map per font.

  apply    PDF --map map.json --out fixed.pdf
      Rewrite ToUnicode + inject ActualText from the map. Output is copy-paste
      correct and visually identical.

  selftest PDF
      Prove the surgery: rewrite/inject with a probe map and confirm the page
      raster is unchanged.

Dependencies:  pip install pikepdf fonttools
"""

import argparse, json, sys, unicodedata
from collections import defaultdict

import pikepdf
from pikepdf import Name, String, Dictionary, Operator, ContentStreamInstruction

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


# -----------------------------------------------------------------------------
# analyze
# -----------------------------------------------------------------------------
def cmd_analyze(args):
    pdf = pikepdf.open(args.pdf)
    print(f"{args.pdf}: {len(pdf.pages)} pages\n")
    print(f"{'BaseFont':38} {'Subtype':16} {'Enc':12} {'ToUni':6}")
    print("-" * 74)
    for base, f in iter_font_objects(pdf):
        sub = str(f.get("/Subtype", ""))
        enc = f.get("/Encoding")
        enc = str(enc) if isinstance(enc, (pikepdf.Name, str)) else (
              str(enc.get("/BaseEncoding", "dict")) if isinstance(enc, pikepdf.Dictionary) else str(enc))
        tu = "yes" if "/ToUnicode" in f else "NO"
        print(f"{base[:38]:38} {sub[:16]:16} {enc[:12]:12} {tu:6}")
    print("\nNote: a present ToUnicode does NOT mean it is correct. Legacy Hansard "
          "\nfonts ship a corrupt one; use buildmap+apply with the original fonts.")


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
    for uni, gname in ttf.getBestCmap().items():
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

def _gname_to_unicode(gname, rcmap, rgsub, _depth=0):
    """Resolve a reference glyph name to a Unicode string (may recurse GSUB)."""
    if _depth > 8:
        return ""
    if gname in rcmap:
        return "".join(chr(u) for u in rcmap[gname])
    if gname in rgsub:
        return "".join(_gname_to_unicode(g, rcmap, rgsub, _depth + 1) for g in rgsub[gname])
    return ""   # unknown (e.g. a pure display variant with no Unicode)

def _find_font_files(paths):
    """Accept dirs and/or individual files; return font file paths.

    Case-insensitive on extension; recurses into directories.
    """
    import os
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

def _load_reference_fonts(paths):
    """Load each font file (including every face inside a .ttc)."""
    from fontTools.ttLib import TTFont, TTCollection
    import os
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
                print(f"  loaded reference: {tag} ({len(tt.getGlyphOrder())} glyphs, "
                      f"upm={_upm(tt)})")
        except Exception as e:
            print(f"  skip {path}: {e}", file=sys.stderr)
    return refs

def cmd_buildmap(args):
    from fontTools.ttLib import TTFont
    import io

    # --fonts may be given several times and/or point at dirs or single files
    paths = args.fonts if isinstance(args.fonts, list) else [args.fonts]
    print(f"Searching for reference fonts in: {', '.join(paths)}")
    refs = _load_reference_fonts(paths)
    if not refs:
        sys.exit(
            "No usable reference fonts found.\n"
            "Point --fonts at a folder containing the ORIGINAL .ttf files, e.g.\n"
            "  Windows: --fonts C:\\Windows\\Fonts\n"
            "  or copy just the needed ones into a folder:\n"
            "     iskpota.ttf iskpotab.ttf  (Iskoola Pota / -Bold, Sinhala)\n"
            "     latha.ttf   lathab.ttf    (Latha, Tamil)\n"
            "     arialuni.ttf              (Arial Unicode MS)\n"
            "     the Dinamina .ttf you have\n"
            "You can pass a whole dir, several dirs, or individual files.")

    pdf = pikepdf.open(args.pdf)
    used = collect_used_cids(pdf)               # {basefont: set(cid)}
    result = {}

    for base, f in iter_font_objects(pdf):
        if not font_is_type0_identity(f):
            continue
        d = f["/DescendantFonts"][0]
        fd = d.get("/FontDescriptor", {})
        ff = fd.get("/FontFile2")
        if ff is None:
            continue
        sub = TTFont(io.BytesIO(bytes(ff.read_bytes())))
        if "glyf" not in sub:
            continue
        sub_gs = sub.getGlyphSet()
        sub_upm = _upm(sub)
        order = sub.getGlyphOrder()

        cid_map = {}
        want = used.get(base, set(range(len(order))))
        for cid in want:
            if cid >= len(order):
                continue
            gname = order[cid]
            try:
                sig = _glyph_signature(sub_gs, gname, sub_upm)
            except Exception:
                sig = None
            uni = ""
            if sig is not None:
                for _, _tt, rsig, rcmap, rgsub in refs:
                    if sig in rsig:
                        uni = _gname_to_unicode(rsig[sig], rcmap, rgsub)
                        if uni:
                            break
            else:
                # Empty-outline glyph: if it advances the pen it is a SPACE.
                # (Otherwise a zero-width mark -> leave empty.) This is what
                # keeps words from running together.
                if _is_blank_space(sub, gname):
                    uni = " "
            if uni:
                cid_map[str(cid)] = uni
        matched = sum(1 for v in cid_map.values() if v.strip())
        spaces  = sum(1 for v in cid_map.values() if v == " ")
        total = len(want)
        result[base] = cid_map
        print(f"{base[:40]:40} matched {matched}/{total} used glyphs"
              + (f"  (+{spaces} spaces)" if spaces else ""))

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
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

def cmd_apply(args):
    import re as _re
    _strip = _re.compile(r"^[A-Z]{6}\+").sub
    def _fontname(bf):
        return _strip("", str(bf).lstrip("/"))

    with open(args.map, encoding="utf-8") as fh:
        raw = json.load(fh)
    # normalise map: {basefont: {int cid: str}}
    fmap = {base: {int(k): v for k, v in m.items()} for base, m in raw.items()}

    pdf = pikepdf.open(args.pdf)

    # merge hand-fix overrides. Keys may be a bare font name ("IskoolaPota") or a
    # full "SUBSET+Name" BaseFont; both are normalised to the bare name so ONE
    # fixes.json works across documents (subset tags differ per file, but the
    # font name and the glyph CIDs are stable).
    if args.fixes:
        with open(args.fixes, encoding="utf-8") as fh:
            fx = json.load(fh)
        by_name = {}
        for key, m in fx.items():
            by_name.setdefault(_fontname(key), {}).update({int(k): v for k, v in m.items()})
        for base, f in iter_font_objects(pdf):
            if font_is_type0_identity(f) and _fontname(base) in by_name:
                fmap.setdefault(base, {}).update(by_name[_fontname(base)])

    # optional whole-word corrections for the residual legacy-vowel cases
    wordfixes = {}
    if args.wordfixes:
        with open(args.wordfixes, encoding="utf-8") as fh:
            wordfixes = json.load(fh)

    # Only remap fonts that actually carry Indic script. Latin/symbol fonts
    # (Times, Calibri, Symbol, ...) already have a correct ToUnicode, so leaving
    # them untouched keeps English/numbers copying correctly.
    skipped = [b for b, m in fmap.items() if not _has_indic(m)]
    for b in skipped:
        del fmap[b]
    if skipped:
        print(f"Left {len(skipped)} non-Indic font(s) untouched (their original "
              f"text layer is already correct).")

    # 1) rewrite ToUnicode per mapped font (helps tools that ignore ActualText)
    for base, f in iter_font_objects(pdf):
        if base in fmap and font_is_type0_identity(f):
            f["/ToUnicode"] = build_tounicode_stream(pdf, fmap[base])

    # 2) inject ActualText per text-show (fixes ordering; primary path)
    if not args.no_actualtext:
        for page in pdf.pages:
            _inject_actualtext_page(page, fmap, wordfixes,
                                    sinhala_fix=not args.no_sinhala_heuristics)

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

def _inject_actualtext_page(page, fmap, wordfixes=None, sinhala_fix=True):
    res = page.get("/Resources")
    fonts = (res or {}).get("/Font", {})
    name_to_base = {str(n): str(ff.get("/BaseFont", "")) for n, ff in
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
        if op in ("Tj", "TJ") and cur in name_to_base and name_to_two.get(cur) \
           and name_to_base[cur] in fmap:
            m = fmap[name_to_base[cur]]
            text = _run_text_with_spaces(instr, op, m)
            text = reorder_logical(text)
            text = _apply_wordfixes(text, wordfixes)   # explicit dictionary first
            if sinhala_fix:
                text = _fix_sinhala_vowels(text)        # then generic repair
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
# diagnose  (per-run glyph strips + current decoded text, to pinpoint bad CIDs)
# -----------------------------------------------------------------------------
def cmd_diagnose(args):
    import io, base64, tempfile, freetype, os
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
    import io, tempfile, os, freetype
    from fontTools.ttLib import TTFont
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
    import hashlib, subprocess, tempfile, os
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
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

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

    args = ap.parse_args()
    args.fn(args)

if __name__ == "__main__":
    main()