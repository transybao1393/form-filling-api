"""
font_utils.py — Unicode-aware font registration for ReportLab.

Problem: ReportLab's built-in Type1 fonts (Helvetica, Times-Roman, etc.) only
cover Latin-1 (ISO-8859-1). Any text with characters outside that range —
Vietnamese, Arabic, CJK, accented Eastern-European, etc. — will be silently
dropped or corrupted when drawn with those fonts.

Solution:
  1. Bundle DejaVu Sans TTF files in fonts/ (committed to the repo, so Docker
     builds also work without a system font dependency).
  2. Expose `resolve_font(text, base_font)` which returns the best available
     font name for the given text:
       - If text is pure Latin-1 → return base_font unchanged (Helvetica etc.)
       - Otherwise → register DejaVuSans (once) and return "DejaVuSans"
  3. Expose `register_unicode_fonts()` for callers that want to pre-register
     all variants up front.

Usage in form_filler.py:
    from font_utils import resolve_font
    font = resolve_font(value, font_name)   # font_name = "Helvetica" default
    c.setFont(font, size)
    c.drawString(x, y, value)
"""

from __future__ import annotations

import os
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_FONTS_DIR = _HERE / "fonts"

# Bundled DejaVu Sans paths (relative to project root)
_DEJAVU_REGULAR = _FONTS_DIR / "DejaVuSans.ttf"
_DEJAVU_BOLD    = _FONTS_DIR / "DejaVuSans-Bold.ttf"
_DEJAVU_OBLIQUE = _FONTS_DIR / "DejaVuSans-Oblique.ttf"

# System-font fallback search list (used when fonts/ dir is missing)
_SYSTEM_FALLBACKS: list[str] = [
    # macOS
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux (common distros)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    # Conda / pip matplotlib bundle
    *(
        str(p)
        for p in Path("/opt/homebrew").glob(
            "**/matplotlib/mpl-data/fonts/ttf/DejaVuSans.ttf"
        )
        if p.exists()
    ),
]

# Internal state — track which font names have been registered
_registered: set[str] = set()

# ReportLab font-name constants
FONT_UNICODE          = "DejaVuSans"
FONT_UNICODE_BOLD     = "DejaVuSans-Bold"
FONT_UNICODE_OBLIQUE  = "DejaVuSans-Oblique"


# ---------------------------------------------------------------------------
# Registration helpers
# ---------------------------------------------------------------------------

def _find_font_file(bundled: Path, system_fallbacks: list[str]) -> str | None:
    """Return the first usable path for a font file."""
    if bundled.exists():
        return str(bundled)
    for path in system_fallbacks:
        if os.path.exists(path):
            return path
    return None


def register_unicode_fonts() -> bool:
    """
    Register DejaVu Sans (regular, bold, oblique) with ReportLab.

    Returns True if at least the regular variant was registered successfully.
    Safe to call multiple times — subsequent calls are no-ops.
    """
    if FONT_UNICODE in _registered:
        return True

    regular_path = _find_font_file(_DEJAVU_REGULAR, _SYSTEM_FALLBACKS)
    if not regular_path:
        return False  # No Unicode font available

    try:
        pdfmetrics.registerFont(TTFont(FONT_UNICODE, regular_path))
        _registered.add(FONT_UNICODE)
    except Exception as exc:
        print(f"[font_utils] WARNING: could not register {FONT_UNICODE}: {exc}")
        return False

    # Bold variant
    bold_path = _find_font_file(_DEJAVU_BOLD, [])
    if bold_path:
        try:
            pdfmetrics.registerFont(TTFont(FONT_UNICODE_BOLD, bold_path))
            _registered.add(FONT_UNICODE_BOLD)
        except Exception:
            pass

    # Oblique (italic) variant
    oblique_path = _find_font_file(_DEJAVU_OBLIQUE, [])
    if oblique_path:
        try:
            pdfmetrics.registerFont(TTFont(FONT_UNICODE_OBLIQUE, oblique_path))
            _registered.add(FONT_UNICODE_OBLIQUE)
        except Exception:
            pass

    return True


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _needs_unicode(text: str) -> bool:
    """
    Return True if `text` contains any character outside Latin-1 (ISO-8859-1).

    Latin-1 covers U+0000–U+00FF.  Vietnamese, CJK, Arabic, etc. all live
    above U+00FF, so this single check is sufficient.
    """
    return any(ord(ch) > 0x00FF for ch in text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_font(text: str, base_font: str = "Helvetica") -> str:
    """
    Return the best ReportLab font name for rendering `text`.

    - If text is pure Latin-1 → return `base_font` unchanged.
    - If text contains non-Latin-1 characters → register DejaVuSans (once)
      and return "DejaVuSans".  Falls back to `base_font` if no Unicode font
      can be found (with a one-time warning).

    Parameters
    ----------
    text:       The string that will be drawn.
    base_font:  Preferred font when no Unicode is needed (default: Helvetica).
    """
    if not _needs_unicode(text):
        return base_font

    if register_unicode_fonts():
        return FONT_UNICODE

    # Last resort: warn once and fall back to base_font (glyphs may be wrong)
    _warn_once(base_font)
    return base_font


_warned: set[str] = set()


def _warn_once(base_font: str) -> None:
    if base_font not in _warned:
        _warned.add(base_font)
        print(
            f"[font_utils] WARNING: Unicode font not available; falling back to "
            f"'{base_font}'. Non-Latin characters may not render correctly.\n"
            f"  → Add DejaVuSans.ttf to the fonts/ directory to fix this."
        )


def resolve_bold(text: str, base_bold: str = "Helvetica-Bold") -> str:
    """Like resolve_font but for bold variants."""
    if not _needs_unicode(text):
        return base_bold
    if register_unicode_fonts() and FONT_UNICODE_BOLD in _registered:
        return FONT_UNICODE_BOLD
    return base_bold


def resolve_oblique(text: str, base_oblique: str = "Helvetica-Oblique") -> str:
    """Like resolve_font but for oblique/italic variants."""
    if not _needs_unicode(text):
        return base_oblique
    if register_unicode_fonts() and FONT_UNICODE_OBLIQUE in _registered:
        return FONT_UNICODE_OBLIQUE
    return base_oblique
