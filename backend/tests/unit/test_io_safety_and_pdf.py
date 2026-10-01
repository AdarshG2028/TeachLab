"""SSRF protection, upload validation, document extraction and PDF rendering."""

from __future__ import annotations

import io

import httpx
import pytest
from pypdf import PdfReader

from app.core.errors import UnsafeURLError, ValidationFailed
from app.providers.pdf.base import DocumentMeta, DocumentSection, DocumentSpec
from app.providers.pdf.reportlab_provider import ReportLabPDFProvider
from app.services.research.documents import extract_document, validate_upload
from app.services.research.fetcher import SafeFetcher, resolve_public

pytestmark = pytest.mark.nodb


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin", "http://localhost:8000/", "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/", "http://[::1]/", "file:///etc/passwd", "ftp://example.com/x", "http://user:pw@example.com/",
    "http://example.com:22/", "http://printer.local/",
])
async def test_ssrf_guard_rejects_unsafe_targets(url):
    with pytest.raises(UnsafeURLError):
        await resolve_public(url)


async def test_fetcher_revalidates_redirects_and_caps_size():
    calls = []

    async def resolver(url):
        calls.append(url)
        if "internal" in url:
            raise UnsafeURLError("non-public")
        return "example.com", "93.184.216.34"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"  # connection pinned to the validated IP
        assert request.headers["host"] == "example.com"
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "http://internal.corp/secret"})
        if request.url.path == "/big":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 5000)
        if request.url.path == "/exe":
            return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"MZ")
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>ok</html>")

    f = SafeFetcher(max_bytes=1000, timeout=5, transport=httpx.MockTransport(handler), resolver=resolver)
    doc = await f.fetch("http://example.com/page")
    assert doc.body == b"<html>ok</html>"
    with pytest.raises(UnsafeURLError):
        await f.fetch("http://example.com/redirect")
    assert calls[-1] == "http://internal.corp/secret"
    with pytest.raises(ValidationFailed):
        await f.fetch("http://example.com/big")
    with pytest.raises(ValidationFailed):
        await f.fetch("http://example.com/exe")


def test_upload_validation():
    assert validate_upload("notes.md", b"# Hi\nthere", 1000) == "text/markdown"
    with pytest.raises(ValidationFailed):
        validate_upload("a.pdf", b"not a pdf", 1000)
    with pytest.raises(ValidationFailed):
        validate_upload("a.exe", b"MZ..", 1000)
    with pytest.raises(ValidationFailed):
        validate_upload("a.txt", b"x" * 2000, 1000)
    with pytest.raises(ValidationFailed):
        validate_upload("a.txt", b"\x00\x01binary", 1000)


def test_html_and_pdf_extraction():
    html = ("<html><head><title>Qubits</title></head><body><nav>menu</nav><article><h1>Qubits</h1>"
            + "<p>A qubit is represented using the Bloch sphere. " * 20 + "</p></article></body></html>")
    doc = extract_document(html.encode(), "text/html", "https://example.edu/q")
    assert "Bloch sphere" in doc.text and doc.title

    spec = DocumentSpec(meta=DocumentMeta(title="Source PDF"), sections=[
        DocumentSection(heading="Body", markdown="Temperature sensors measure temperature. " * 30)])
    pdf = ReportLabPDFProvider().render(spec)
    extracted = extract_document(pdf, "application/pdf")
    assert "Temperature sensors measure temperature" in extracted.text


def test_pdf_rendering_features():
    md = """### Definitions
A **qubit** is a two-level system with state $|\\psi\\rangle = \\alpha|0\\rangle + \\beta|1\\rangle$ where
$|\\alpha|^2 + |\\beta|^2 = 1$. Cost is $5 and $10 (not math).

$$\\sum_{i=1}^{n} i = \\frac{n(n+1)}{2}$$

$$\\begin{align} a &= b \\end{align}$$

| Term | Meaning |
|---|---|
| Superposition | Linear combination of states |
| Entanglement | Non-classical correlation — ünïcødé ✓ |

```python
def add(a, b):
    return a + b
```

```mermaid
graph TD; A-->B
```

1. First step
2. Second step
   - nested bullet

> A quoted perspective, according to historians.

[Source](https://example.edu/q) and Grüße, 日本語 text.
"""
    spec = DocumentSpec(meta=DocumentMeta(title="Quantum Notes", subject="Physics", class_name="Grade 11",
                                          teacher="Ms. Rivera", institution="City School", date="2026-10-01",
                                          duration="60 minutes", total_marks="40",
                                          instructions=["Answer all questions."]),
                        sections=[DocumentSection(heading=f"Section {i}", markdown=md, page_break_before=i > 0)
                                  for i in range(3)])
    data = ReportLabPDFProvider().render(spec)
    assert data.startswith(b"%PDF")
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) >= 3
    text = "\n".join(p.extract_text() for p in reader.pages)
    flat = " ".join(text.split())
    assert f"Page 1 of {len(reader.pages)}" in text
    assert "Superposition" in text and "def add" in text and "Ms. Rivera" in text
    assert "Cost is $5 and $10" in flat  # currency not treated as math
    assert "Grüße" in text
    assert "\\begin{align}" in text  # unsupported environment falls back to source, not dropped
    assert reader.metadata.title == "Quantum Notes"
