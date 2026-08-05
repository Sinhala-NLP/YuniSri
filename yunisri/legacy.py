# -*- coding: utf-8 -*-
"""
legacy.py  --  recover Unicode from legacy "FM"/"DL" Sinhala fonts.

A second recovery mode for the OTHER common kind of broken Sinhala PDF: the one
that uses legacy 8-bit fonts (FMAbhaya, FMSamantha, FMGangane, FMMalithi,
FMAbabld, DL-* ...) embedded as *simple* /TrueType or /Type1 fonts with
/WinAnsiEncoding.

In those files the Sinhala is not encoded as Sinhala at all: each glyph is mapped
onto an ordinary Latin codepoint, so "විද්‍යාව" is literally stored as the bytes
"úoHdj". The font just paints a Sinhala shape for each Latin character. There is
no outline puzzle to solve (that's the Type0 case in fix_pdf_unicode.py) -- the
bytes ARE the letters, in a fixed legacy layout. So recovery is a transliteration
table plus reordering, not outline matching.

The conversion is two steps:
  1. longest-match substitution of legacy byte-sequences -> Unicode pieces, in
     the font's VISUAL order (pre-base vowel signs still sitting before the
     consonant, split vowels still in two parts);
  2. an FM-specific reorder/recompose pass that folds split vowels back together
     and moves pre-base vowels after their consonant cluster (rakaransaya /
     yansaya / touching conjuncts included).

The table below is seeded from real Grade-10 textbook content and the standard
FM layout. FM fonts carry a large number of per-ligature glyphs, so a given
document may use a byte this table doesn't know yet; those come through as the
raw Latin char and can be added here or via a legacy-overrides JSON.
"""

import re as _re

# -----------------------------------------------------------------------------
# Which simple fonts are legacy Sinhala fonts?
# -----------------------------------------------------------------------------
# Matched against the BaseFont with any 6-char subset tag already stripped.
LEGACY_FONT_PREFIXES = ("FM", "DL", "fm", "dl")

def basefont_is_legacy(name):
    """True if a (subset-stripped) BaseFont name looks like an FM/DL font."""
    n = str(name).lstrip("/")
    if "+" in n:
        n = n.split("+", 1)[1]
    return n.startswith(LEGACY_FONT_PREFIXES)


# -----------------------------------------------------------------------------
# Legacy -> Unicode substitution table (visual order)
# -----------------------------------------------------------------------------
# Notes on sentinels used in the *values*:
#   '\ue000'  DEERGHA  - the trailing stroke that either completes a pre-base
#                        vowel (ෙ+…+DEERGHA -> ේ) or, with no pre-base vowel in
#                        the cluster, acts as al-lakuna/virama (්). Resolved in
#                        _fm_reorder so one legacy byte can serve both roles, as
#                        it does in the fonts.
# Pre-base vowel signs are emitted as their real Unicode (ෙ ේ ෛ ො ෝ ෞ) and
# moved by the reorder pass. Two-part vowels are emitted split (ෙ … ා) and
# recombined there.
DEERGHA = "\ue000"

# Order matters only in that we build a longest-key-first matcher below.
FM_MAP = {
    # ---- independent vowels ----
    "w":  "අ",
    "wd": "ආ",
    "we": "ඇ",
    "wE": "ඈ",
    "b":  "ඉ",
    "B":  "ඊ",
    "W":  "උ",
    "Wq": "ඌ",
    "R":  "ඍ",
    "t":  "එ",
    "ta": "එ" + DEERGHA,   # ඒ (එ + deergha)
    "ft": "ඓ",
    "T":  "ඔ",
    "Ta": "ඔ" + DEERGHA,   # ඕ
    "ft!": "ඖ",

    # ---- consonants ----
    "l": "ක", "L": "ඛ", ".": "ග", "R": "ඝ", "X": "ඞ",
    "p": "ච", "P": "ඡ", "c": "ජ", "®": "ඣ", "\u00f1": "මි",  # ñ handled below too
    "g": "ට", "G": "ඨ", "v": "ඩ", "V": "ඪ", "K": "ණ",
    ";": "ත", ":": "ථ", "o": "ද", "O": "ධ", "k": "න",
    "m": "ප", "M": "ඵ", "n": "බ", "N": "භ", "u": "ම",
    "h": "ය", "r": "ර", ",": "ල", "j": "ව",
    "Y": "ශ", "I": "ෂ", "i": "ස", "y": "හ",
    "<": "ළ", "*": "ෆ",

    # ---- dependent vowel signs (post-base) ----
    "d": "ා",
    "s": "ි", "S": "ී",
    "q": "\u0dd4", "Q": "\u0dd6",   # ු  ූ
    "D": "\u0dd8",                    # ෘ
    "e": "ැ", "E": "ෑ",

    # ---- pre-base vowel signs (moved by reorder) ----
    "f": "ෙ",          # ෙ (also the front half of ො / ේ / ෝ)
    "F": "ෛ",          # ෛ
    "a": DEERGHA,       # deergha stroke (ේ completion / hal, per context)

    # ---- anusvara / visarga ----
    "x": "ං", "H": "\u0dca\u200d\u0dba",  # ය-anseh? actually yansaya below

    # ---- special conjunct strokes ----
    "%": "\u0dca\u200d\u0dbb",  # ්‍ර  rakaransaya
    # "H": yansaya set below (overwrite)
}

# The following are multi-byte / higher-plane entries confirmed from real text,
# added explicitly so they win as longest matches. Values in visual order.
FM_MAP.update({
    "H":  "\u0dca\u200d\u0dba",   # ්‍ය  yansaya   (විද්‍යාව, අධ්‍යාපන)
    "%":  "\u0dca\u200d\u0dbb",   # ්‍ර  rakaransaya (ශ්‍රී, ප්‍ර)
    "\u00be": "ර\u0dca",           # ¾  -> ර්  (repaya as ර+virama)  දෙපාර්ත…
    "\u00f8": "ද\u0dca\u200d\u0dbb",  # ø  -> ද්‍ර  මුද්‍රණය
    "\u00fa": "වි",                # ú  -> වි   විද්‍යාව
    "\u00f1": "මි",                # ñ  -> මි   හිමිකම්
    "\u00ef": "ම",                 # ï  -> ම    (takes a following vowel; a bare
                                   #             word-final ම් gets its hal from
                                   #             a trailing deergha byte)
    "\u00f5": "ව\u0dca",           # õ  -> ව්   සිව්වන
    "\u00df": "රි",                # ß  -> රි   ඇවිරිණි
    "\u00a8": "ලු",                # ¨  -> ලු   සියලු
    "=":  "\u0dd4",                # =  -> ු    (ත/ක-shaped u)  තුව
})

# More FM ligature bytes, each confirmed against real Grade-10 textbook body
# text (word in the comment). These are the per-consonant "+i" / precomposed
# forms and a few conjuncts that a running-text page needs.
FM_MAP.update({
    "\u201a": "\u0dab\u0dd2",       # ‚  -> ණි   (පණිවුඩය, තාක්ෂණික)
    "/":      "ර",                 # /  -> ර    (නිවැරදි, ...)
    "\u00c8": "ද\u0dd2",           # È  -> දි   (නිවැරදි, දියුණු)
    "\u00bf": "ළු",                # ¿  -> ළු   (මුළු)
    "\u00f9": "වී",                # ù  -> වී   (ගෙවී, ...ගැන්වීම)
    "\u00ff": "ද\u0dd4",           # ÿ  -> දු   (සිදුවූ, නුදුරු)
    "\u00d5": "ඟ",                 # Õ  -> ඟ    (සමඟ)
    "\u00ea": "ධ\u0dd2",           # ê  -> ධි   (බුද්ධිය)
    ">":      "ඝ",                 # >  -> ඝ    (ශීඝ්‍ර)
    "\u00ed": "බ",                 # í  -> බ    (තිබේ)
})


# -----------------------------------------------------------------------------
# longest-match substitution
# -----------------------------------------------------------------------------
_MAXLEN = max(len(k) for k in FM_MAP)

def _substitute(legacy, extra=None):
    """Greedy longest-match legacy->Unicode substitution (visual order)."""
    table = FM_MAP if not extra else {**FM_MAP, **extra}
    maxlen = max(_MAXLEN, *(len(k) for k in extra)) if extra else _MAXLEN
    out = []
    i = 0
    n = len(legacy)
    while i < n:
        hit = None
        for L in range(min(maxlen, n - i), 0, -1):
            seg = legacy[i:i + L]
            if seg in table:
                hit = (seg, table[seg])
                break
        if hit:
            out.append(hit[1])
            i += len(hit[0])
        else:
            out.append(legacy[i])   # pass through (Latin digit, punctuation, space)
            i += 1
    return "".join(out)


# -----------------------------------------------------------------------------
# FM reorder / recompose  (visual order -> logical Unicode)
# -----------------------------------------------------------------------------
_C = "\u0d9a-\u0dc6"                       # consonant range
# a consonant cluster: base consonant + optional rakar/yansaya + optional
# (virama+consonant) touching pieces
_CLUSTER = ("[" + _C + "]"
            "(?:\u0dca\u200d[" + _C + "])?"      # ්‍C  (conjunct)
            "(?:\u0dca\u200d[\u0dbb\u0dba])?"    # ්‍ර / ්‍ය already-inlined
            )

_PRE = "\ufdd0"   # temporary placeholder while relocating (unused sentinel)

def _fm_reorder(text):
    """Fold split vowels and move pre-base vowels after their consonant cluster.

    Works on the visual-order string produced by _substitute, where a pre-base
    ෙ (and its optional trailing ා / DEERGHA parts) still sits before the
    consonant cluster it belongs to.
    """
    C = "([" + _C + "](?:\u0dca\u200d[" + _C + "])?)"

    # 1) pre-base ෙ + cluster + back-part  ->  cluster + composed vowel
    #    ෙ C ා        -> C ො
    text = _re.sub("\u0dd9" + C + "\u0dcf", lambda m: m.group(1) + "\u0ddc", text)
    #    ො + deergha  -> ෝ
    text = text.replace("\u0ddc" + DEERGHA, "\u0ddd")
    #    ෙ C DEERGHA  -> C ේ
    text = _re.sub("\u0dd9" + C + DEERGHA, lambda m: m.group(1) + "\u0dda", text)
    #    ෙ C1 C2 DEERGHA -> C1 ේ C2 ්   (long-e on first consonant, hal on the
    #    next; e.g. …මේන්…, …තේන්…, where the deergha lands past the intervening
    #    consonant in legacy order)
    text = _re.sub("\u0dd9" + C + C + DEERGHA,
                   lambda m: m.group(1) + "\u0dda" + m.group(2) + "\u0dca", text)
    #    ෙ C          -> C ෙ   (plain e)
    text = _re.sub("\u0dd9" + C, lambda m: m.group(1) + "\u0dd9", text)
    #    ෛ C          -> C ෛ
    text = _re.sub("\u0ddb" + C, lambda m: m.group(1) + "\u0ddb", text)

    # 2) any DEERGHA still standing is an al-lakuna / virama
    text = text.replace(DEERGHA, "\u0dca")

    return text


def transliterate(legacy, extra=None):
    """Convert one legacy FM/DL string to logical Unicode Sinhala."""
    return _fm_reorder(_substitute(legacy, extra))


# convenience: is a decoded string worth remapping (does it contain FM signal)?
def looks_like_legacy_text(s):
    """Heuristic: legacy FM text is Latin-range bytes with the tell-tale mix of
    letters and symbols like ¾ ø ú ï. Pure ASCII digits/words are left alone."""
    return any(ch in s for ch in "\u00be\u00f8\u00fa\u00ef\u00f5\u00df\u00a8\u00f1")
