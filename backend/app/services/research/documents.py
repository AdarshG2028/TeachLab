"""Turn raw documents (HTML, PDF, text, uploads) into normalized text."""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass

from app.core.errors import ValidationFailed

UPLOAD_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
}


@dataclass
class NormalizedDocument:
    title: str
    text: str
    content_type: str
    published_at: str | None = None
    site_name: str = ""


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f]", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_html(html: str | bytes, url: str | None = None) -> NormalizedDocument:
    import trafilatura

    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    text = trafilatura.extract(html, url=url, include_tables=True, include_comments=False, output_format="markdown",
                               favor_recall=True) or ""
    meta = trafilatura.extract_metadata(html, default_url=url)
    title = (meta.title if meta and meta.title else "") or ""
    return NormalizedDocument(
        title=title,
        text=normalize_text(text),
        content_type="text/html",
        published_at=(meta.date if meta and meta.date else None),
        site_name=(meta.sitename if meta and meta.sitename else "") or "",
    )


def extract_pdf(data: bytes, *, max_pages: int = 300) -> NormalizedDocument:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    if not data.startswith(b"%PDF-"):
        raise ValidationFailed("File is not a valid PDF")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValidationFailed("Encrypted PDFs are not supported")
        pages = []
        for i, page in enumerate(reader.pages):
            if i >= max_pages:
                break
            pages.append(page.extract_text() or "")
        title = ""
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title)
    except PdfReadError as exc:
        raise ValidationFailed(f"Could not read PDF: {exc}") from exc
    text = normalize_text("\n\n".join(pages))
    if not text:
        raise ValidationFailed("PDF contains no extractable text (scanned PDFs need OCR, which is not supported)")
    return NormalizedDocument(title=title, text=text, content_type="application/pdf")


def extract_document(data: bytes, content_type: str, url: str | None = None) -> NormalizedDocument:
    if content_type == "application/pdf":
        return extract_pdf(data)
    if content_type in ("text/html", "application/xhtml+xml"):
        return extract_html(data, url)
    if content_type in ("text/plain", "text/markdown"):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        text = normalize_text(text)
        first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), "")
        return NormalizedDocument(title=first[:200], text=text, content_type=content_type)
    raise ValidationFailed(f"Unsupported content type: {content_type}")


def validate_upload(filename: str, data: bytes, max_bytes: int) -> str:
    """Return the content type for an upload, or raise."""
    if len(data) == 0:
        raise ValidationFailed("Empty file")
    if len(data) > max_bytes:
        raise ValidationFailed("File exceeds size limit")
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    ctype = UPLOAD_TYPES.get(ext)
    if not ctype:
        raise ValidationFailed(f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(UPLOAD_TYPES))}")
    if ctype == "application/pdf" and not data.startswith(b"%PDF-"):
        raise ValidationFailed("File content is not a PDF")
    if ctype != "application/pdf" and b"\x00" in data[:4096]:
        raise ValidationFailed("Binary content is not allowed for text uploads")
    return ctype
