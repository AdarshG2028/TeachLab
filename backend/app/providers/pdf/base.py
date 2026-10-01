"""PDF provider interface.

Documents are described as a provider-neutral ``DocumentSpec`` (metadata plus
Markdown sections with $math$), so a hosted renderer (e.g. PDFMonkey, a
headless-Chromium service) can be added as another implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class DocumentMeta(BaseModel):
    title: str
    subtitle: str = ""
    subject: str = ""
    class_name: str = ""
    teacher: str = ""
    institution: str = ""
    date: str = ""
    duration: str = ""
    total_marks: str = ""
    extra: dict[str, str] = Field(default_factory=dict)
    instructions: list[str] = Field(default_factory=list)
    footer_note: str = ""


class DocumentSection(BaseModel):
    heading: str = ""
    markdown: str = ""
    page_break_before: bool = False
    level: int = 1


class DocumentSpec(BaseModel):
    meta: DocumentMeta
    sections: list[DocumentSection]
    show_header_block: bool = True
    show_toc: bool = False


class PDFProvider(ABC):
    name: str = "base"

    @abstractmethod
    def render(self, spec: DocumentSpec) -> bytes:
        """Render synchronously (callers run it in a thread)."""
