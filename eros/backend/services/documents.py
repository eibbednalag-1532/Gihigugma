"""Reads uploaded files and splits them into chunks.

Supported: PDF, DOCX, TXT (plus Markdown, CSV, JSON, HTML read as plain text).
Everything runs locally; no file leaves this computer.

Pipeline: validate -> extract -> clean -> chunk. Pages that yield no text (for
example scanned images) are counted and reported, never silently passed off as read.
"""
from __future__ import annotations

import io
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

TEXT_EXT = {".txt", ".md", ".csv", ".json"}
SUPPORTED_EXT = TEXT_EXT | {".pdf", ".docx", ".html", ".htm"}

OLE_MAGIC = b"\xd0\xcf\x11\xe0"      # legacy Office files (.doc)


class DocumentError(Exception):
    """A problem with an uploaded file that the user can understand."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass
class Extraction:
    pages: list[tuple[int | None, str]]   # (page number or None, cleaned text), only pages with text
    page_count: int | None                # total pages in the file, when the format has pages
    empty_pages: int                      # pages with no readable text (e.g. scanned images)


# ---------- validation ----------
def _validate(ext: str, data: bytes) -> None:
    """Check that the bytes match the extension before handing them to a parser."""
    if ext == ".doc" or (ext == ".docx" and data[:4] == OLE_MAGIC):
        raise DocumentError("unsupported_type",
                            "Old Word .doc files aren't supported. Open it in Word and save as .docx.", 415)
    if ext not in SUPPORTED_EXT:
        raise DocumentError("unsupported_type", f"'{ext or 'This file type'}' isn't supported. "
                            "Use PDF, DOCX, or TXT.", 415)
    if ext == ".pdf" and b"%PDF-" not in data[:1024]:
        raise DocumentError("unreadable_file", "This file isn't a valid PDF.")
    if ext == ".docx" and data[:2] != b"PK":
        raise DocumentError("unreadable_file", "This file isn't a valid DOCX document.")
    if ext in TEXT_EXT | {".html", ".htm"}:
        head = data[:8192]
        is_utf16 = head[:2] in (b"\xff\xfe", b"\xfe\xff")
        if b"\x00" in head and not is_utf16:
            raise DocumentError("unreadable_file", "This looks like a binary file, not text.")


# ---------- extraction ----------
def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeError:
            continue
    return data.decode("cp1252", errors="replace")


def _read_pdf(data: bytes) -> list[tuple[int, str]]:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise DocumentError("pdf_encrypted", "This PDF is password-protected. Remove the password and try again.")
        total = len(reader.pages)
    except DocumentError:
        raise
    except Exception:
        raise DocumentError("unreadable_file", "This PDF couldn't be read. It may be damaged.") from None

    pages = []
    for number in range(1, total + 1):
        try:
            text = reader.pages[number - 1].extract_text() or ""
        except Exception:
            text = ""     # one broken page shouldn't lose the rest; it is reported as unreadable
        pages.append((number, text))
    return pages


def _read_docx(data: bytes) -> str:
    import docx
    from docx.table import Table
    try:
        document = docx.Document(io.BytesIO(data))
        parts = []
        for block in document.iter_inner_content():      # paragraphs and tables in reading order
            if isinstance(block, Table):
                for row in block.rows:
                    cells = []
                    for cell in row.cells:
                        text = cell.text.strip()
                        if text and (not cells or cells[-1] != text):   # merged cells repeat their text
                            cells.append(text)
                    if cells:
                        parts.append(" | ".join(cells))
            else:
                parts.append(block.text)
        return "\n\n".join(parts)
    except Exception:
        raise DocumentError("unreadable_file", "This DOCX couldn't be read. It may be damaged.") from None


class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


# ---------- cleaning ----------
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​﻿]")


def clean_text(text: str) -> str:
    """Normalise extracted text without changing its wording."""
    text = unicodedata.normalize("NFKC", text)             # ligatures (ﬁ -> fi), full-width forms
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("­", "")
    text = _CONTROL.sub("", text)
    text = re.sub(r"([a-z])-\n([a-z])", r"\1\2", text)      # re-join words hyphenated across lines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_running_lines(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Remove headers/footers that repeat on most pages (titles, 'Page 3 of 10')."""
    if len(pages) < 4:
        return pages

    def key(line: str) -> str:
        return re.sub(r"\d+", "#", line.strip().lower())

    edges = Counter()
    for _, text in pages:
        lines = [ln for ln in text.split("\n") if ln.strip()]
        for line in {key(ln) for ln in lines[:2] + lines[-2:]}:      # top and bottom of the page
            if len(line) <= 80:
                edges[line] += 1
    repeated = {line for line, n in edges.items() if n >= max(3, 0.6 * len(pages))}
    if not repeated:
        return pages

    out = []
    for number, text in pages:
        lines = text.split("\n")
        while lines and (not lines[0].strip() or key(lines[0]) in repeated):
            lines.pop(0)
        while lines and (not lines[-1].strip() or key(lines[-1]) in repeated):
            lines.pop()
        out.append((number, "\n".join(lines)))
    return out


def extract_text(filename: str, data: bytes) -> Extraction:
    ext = Path(filename).suffix.lower()
    _validate(ext, data)

    page_count = None
    if ext == ".pdf":
        raw = _read_pdf(data)
        page_count = len(raw)
        cleaned = _strip_running_lines([(n, clean_text(t)) for n, t in raw])
    elif ext == ".docx":
        cleaned = [(None, clean_text(_read_docx(data)))]
    elif ext in (".html", ".htm"):
        parser = _TextOnly()
        parser.feed(_decode(data))
        cleaned = [(None, clean_text("".join(parser.parts)))]
    else:
        cleaned = [(None, clean_text(_decode(data)))]

    pages = [(n, t) for n, t in cleaned if t.strip()]
    if not pages:
        raise DocumentError(
            "no_text",
            "No readable text was found in this file. If it is a scanned or image-only document it needs OCR, "
            "which Gihigugma doesn't do yet.", 422)
    empty = (page_count - len(pages)) if page_count else 0
    return Extraction(pages=pages, page_count=page_count, empty_pages=empty)


# ---------- chunking ----------
def _split_long(paragraph: str, size: int) -> list[str]:
    parts = []
    while len(paragraph) > size:
        cut = paragraph.rfind(". ", 0, size)
        if cut < size // 2:
            cut = paragraph.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size - 1
        parts.append(paragraph[:cut + 1].strip())
        paragraph = paragraph[cut + 1:].strip()
    if paragraph:
        parts.append(paragraph)
    return parts


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Pack paragraphs into chunks of about `size` characters, overlapping by ~`overlap`."""
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(para.split())
        if para:
            units.extend(_split_long(para, size))

    chunks: list[str] = []
    cur = ""
    for unit in units:
        if cur and len(cur) + 1 + len(unit) > size:
            chunks.append(cur)
            tail = cur[-overlap:] if overlap > 0 else ""
            if " " in tail:
                tail = tail[tail.index(" ") + 1:]    # start the overlap on a word boundary
            cur = f"{tail} {unit}".strip() if tail else unit
        else:
            cur = f"{cur} {unit}" if cur else unit
    if cur:
        chunks.append(cur)
    return chunks


def chunk_pages(pages: list[tuple[int | None, str]], size: int, overlap: int) -> list[dict]:
    """Chunks that remember their page (chunks never span pages, so citations stay exact)."""
    out = []
    for page, text in pages:
        for piece in chunk_text(text, size, overlap):
            out.append({"page": page, "text": piece})
    return out
