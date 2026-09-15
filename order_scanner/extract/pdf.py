"""Fetch and text-extract announcement attachments (PDFs)."""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil

from ..config import CONFIG, PDF_CACHE
from .money import normalize_rupee_glyphs

log = logging.getLogger(__name__)


def cache_path(url: str):
    return PDF_CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".pdf")


def download_pdf(session, url: str) -> bytes | None:
    if not url or not CONFIG.download_pdfs:
        return None
    path = cache_path(url)
    if path.exists():
        return path.read_bytes()
    try:
        r = session.get(url, timeout=CONFIG.request_timeout, stream=True)
        r.raise_for_status()
        size = int(r.headers.get("content-length") or 0)
        if size and size > CONFIG.max_pdf_mb * 1024 * 1024:
            log.info("skipping oversized pdf (%.1f MB): %s", size / 1e6, url)
            return None
        content = r.content
        if not content.startswith(b"%PDF"):
            return None
        path.write_bytes(content)
        return content
    except Exception as exc:
        log.warning("pdf download failed %s: %s", url, exc)
        return None


# Below this many characters the PDF is treated as a scan: a letterhead or a
# stamp can carry a few words of real text over what is otherwise an image.
_SCANNED_BELOW = 200

# A text layer whose rupee sign came out as a stray glyph in front of a lakh or
# crore figure. Veerhealth's PDF maps "Rs10.07 Lacs" to "n 0.07 Lacs" -- the
# glyph swallows a digit, so no amount of cleanup recovers the figure, and the
# page has to be read again with OCR.
_RUPEE_DAMAGE = re.compile(
    r"(?:(?<!\d)[%~¢=?]|(?<![\w.,])[ntz])\s?\d[\d,]*(?:\.\d+)?\s*(?:lacs?|lakhs?|crores?)\b",
    re.IGNORECASE)


def rupee_damage(text: str | None) -> bool:
    return bool(text and _RUPEE_DAMAGE.search(text))


def pdf_to_text(data: bytes | None, max_pages: int = 8) -> str | None:
    """Text of the first `max_pages` pages. Order values live up front.

    OCR is used for scans (no text layer) and for text layers whose rupee
    figures are damaged. Stray rupee glyphs are then normalised either way.
    """
    if not data:
        return None
    text = _try_pdfplumber(data, max_pages) or _try_pymupdf(data, max_pages)
    layer = (text or "").strip()
    damaged = rupee_damage(layer)
    if CONFIG.ocr_pdfs and (len(layer) < _SCANNED_BELOW or damaged):
        ocr = (_try_ocr(data, max_pages=4) or "").strip()
        if len(ocr) > len(layer) or (damaged and len(ocr) >= 0.6 * len(layer)):
            text = ocr
    if not text:
        return None
    text = normalize_rupee_glyphs(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:40000]


def _try_pdfplumber(data: bytes, max_pages: int) -> str | None:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join(
                (p.extract_text() or "") for p in pdf.pages[:max_pages])
    except Exception as exc:
        log.debug("pdfplumber failed: %s", exc)
        return None


def _try_pymupdf(data: bytes, max_pages: int) -> str | None:
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=data, filetype="pdf")
        return "\n".join(doc[i].get_text() for i in range(min(max_pages, doc.page_count)))
    except Exception as exc:
        log.debug("pymupdf failed: %s", exc)
        return None


# Tesseract's default install location on Windows, used when it is not on PATH.
_TESSERACT_WINDOWS = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
# At 300 dpi Tesseract sometimes fuses the rupee sign into the figure
# ("Rs10.07" -> "710.07"); at 400 dpi it reads it as a separate symbol
# ("%10.07"), which normalize_rupee_glyphs then turns back into the rupee sign.
_OCR_DPI = 400


def _try_ocr(data: bytes, max_pages: int) -> str | None:
    """OCR the first pages. Pages are rendered by pdfplumber (pypdfium2), so
    this needs only pytesseract and the Tesseract binary, not PyMuPDF."""
    try:
        import pdfplumber
        import pytesseract
    except ImportError:
        return None
    if not shutil.which("tesseract"):
        if not os.path.exists(_TESSERACT_WINDOWS):
            return None
        pytesseract.pytesseract.tesseract_cmd = _TESSERACT_WINDOWS
    try:
        out = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages[:max_pages]:
                image = page.to_image(resolution=_OCR_DPI).original
                out.append(pytesseract.image_to_string(image, lang="eng"))
        return "\n".join(out)
    except Exception as exc:
        log.debug("ocr failed: %s", exc)
        return None
