"""
yunisri
=======

Make broken-encoding Sri-Lankan (Sinhala / Tamil) PDFs copy-paste-able without
OCR and without changing how they look.

    import yunisri
    yunisri.fix_pdf("hansard.pdf")             # -> writes "output.pdf"
    yunisri.fix_pdf("hansard.pdf", "clean.pdf")

One call handles both common failure modes automatically:
  * mode 1 -- Type0/Identity-H subset fonts with corrupt ToUnicode (recovered by
    matching glyph outlines against the bundled reference fonts);
  * mode 2 -- legacy FM/DL fonts that store Sinhala as Latin bytes (recovered by
    transliteration; see yunisri.legacy).

The reference fonts and correction dictionaries ship inside the package, so you
never pass a fonts directory.
"""

from .fix_pdf_unicode import fix_pdf, build_map, apply_map, apply_legacy
from . import legacy

__all__ = ["fix_pdf", "build_map", "apply_map", "apply_legacy", "legacy"]
__version__ = "0.1.0"