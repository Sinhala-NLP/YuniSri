[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) 
[![PyPI version](https://img.shields.io/pypi/v/yunisri?color=%236ecfbd&label=pypi%20package&style=flat-square)](https://pypi.org/project/yunisri/)
[![Downloads](https://pepy.tech/badge/yunisri)](https://pepy.tech/project/yunisri)

# YuniSri

Make broken-encoding Sri-Lankan (Sinhala / Tamil) PDFs (e.g. Hansard) **copy-paste-able
without changing how the pages look**.

Many Sri-Lankan government PDFs embed subsetted legacy fonts with a corrupt
`/ToUnicode` map: the glyphs draw correctly but the text you copy is garbage.
`yunisri` recovers the real Unicode by matching each embedded glyph *outline*
against the original reference fonts (bundled in the package), then rewrites the
`/ToUnicode` map and injects `/ActualText`. The glyph-drawing operators and font
programs are never touched, so the rendered pages are byte-for-byte identical.

## Install

```bash
pip install yunisri
```

## Usage (Python)

```python
import yunisri

# The one call you need — reference fonts are bundled, no fonts arg required.
yunisri.fix_pdf("hansard.pdf")                 # writes output.pdf
yunisri.fix_pdf("hansard.pdf", "clean.pdf")    # custom output name
```

`fix_pdf(input_pdf, output_pdf="output.pdf")` returns the output path. The
bundled `fixes.json` (per-glyph overrides) and `wordfixes.json` (whole-word
overrides) are applied automatically.

Optional keyword arguments:

| arg | default | meaning |
|---|---|---|
| `fonts_dir` | bundled | override the reference fonts (dir, list, or file) |
| `fixes` | `"bundled"` | dict, JSON path, `"bundled"`, or `None` |
| `wordfixes` | `"bundled"` | dict, JSON path, `"bundled"`, or `None` |
| `sinhala_heuristics` | `True` | generic Sinhala split-vowel repair |
| `actualtext` | `True` | inject ActualText (reordering); False = ToUnicode only |
| `verbose` | `False` | print per-font match statistics |

## Usage (CLI)

```bash
yunisri fix hansard.pdf                 # -> output.pdf (bundled fonts)
yunisri fix hansard.pdf --out clean.pdf

# advanced / power-user commands
yunisri analyze  hansard.pdf
yunisri buildmap hansard.pdf --fonts /path/to/original/fonts --out map.json
yunisri apply    hansard.pdf --map map.json --out fixed.pdf
yunisri selftest hansard.pdf
yunisri report   hansard.pdf --map map.json      # needs the diagnostics extra
yunisri diagnose hansard.pdf --page 5            # needs the diagnostics extra
```

The `report` and `diagnose` commands render glyph images and need extra deps:

```bash
pip install "yunisri[diagnostics]"
```

## How it works

1. **buildmap** — for every embedded subset, a normalised signature of each
   glyph outline is matched against the reference fonts to recover its Unicode
   (with reverse-`cmap` + reverse-`GSUB` to decompose conjuncts). Empty glyphs
   that advance the pen become spaces.
2. **apply** — rewrites each font's `/ToUnicode` CMap and wraps every text-show
   in `/Span <</ActualText(...)>> BDC … EMC`, carrying logically-ordered Unicode
   (pre-base vowel signs moved after their consonant). Non-Indic fonts are left
   untouched so English and numbers keep copying correctly.