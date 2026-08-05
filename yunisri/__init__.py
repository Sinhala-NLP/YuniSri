"""
yunisri
=======

Make broken-encoding Sri-Lankan (Sinhala / Tamil) PDFs copy-paste-able without
OCR and without changing how they look.

    import yunisri
    yunisri.fix_pdf("hansard.pdf")             # -> writes "output.pdf"
    yunisri.fix_pdf("hansard.pdf", "clean.pdf")

The reference fonts and correction dictionaries ship inside the package, so you
never pass a fonts directory.
"""

from .fix_pdf_unicode import fix_pdf, build_map, apply_map

__all__ = ["fix_pdf", "build_map", "apply_map"]
__version__ = "0.1.0"