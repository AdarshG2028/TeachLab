"""Deterministic, heading-aware text chunking with token targets and overlap."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.ids import sha256_text, stable_id
from app.core.tokens import count_tokens

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


@dataclass
class TextChunk:
    id: str
    idx: int
    text: str
    start_char: int
    end_char: int
    token_count: int
    heading: str


def _paragraphs(text: str) -> list[tuple[int, int, str]]:
    """Paragraph spans (start, end, text) with character offsets into ``text``."""
    spans = []
    for m in re.finditer(r"[^\n](?:.|\n(?!\n))*", text):
        spans.append((m.start(), m.end(), m.group(0)))
    return spans


def _split_long(start: int, para: str, target: int) -> list[tuple[int, int, str]]:
    """Split an oversized paragraph on sentence boundaries (falls back to words)."""
    spans: list[tuple[int, int]] = []  # sentence (or word-window) spans within para
    cursor = 0
    for sentence in _SENTENCE.split(para):
        pos = para.find(sentence, cursor)
        cursor = pos + len(sentence)
        if count_tokens(sentence) > target:  # pathological sentence: split into word windows
            words = list(re.finditer(r"\S+", sentence))
            step = max(1, target * 2 // 3)
            for i in range(0, len(words), step):
                window = words[i : i + step]
                spans.append((pos + window[0].start(), pos + window[-1].end()))
        else:
            spans.append((pos, pos + len(sentence)))

    pieces: list[tuple[int, int, str]] = []
    cur_s: int | None = None
    cur_e = 0
    for s, e in spans:
        if cur_s is not None and count_tokens(para[cur_s:e]) > target:
            pieces.append((start + cur_s, start + cur_e, para[cur_s:cur_e]))
            cur_s = None
        if cur_s is None:
            cur_s = s
        cur_e = e
    if cur_s is not None:
        pieces.append((start + cur_s, start + cur_e, para[cur_s:cur_e]))
    return pieces


def chunk_text(source_id: str, text: str, *, target_tokens: int = 350, overlap_tokens: int = 50) -> list[TextChunk]:
    units: list[tuple[int, int, str, str]] = []  # start, end, text, heading
    heading = ""
    for start, end, para in _paragraphs(text):
        m = _HEADING.match(para.strip())
        if m:
            heading = m.group(2).strip()
        if count_tokens(para) > target_tokens:
            for s, e, piece in _split_long(start, para, target_tokens):
                units.append((s, e, piece, heading))
        else:
            units.append((start, end, para, heading))

    chunks: list[TextChunk] = []
    i = 0
    while i < len(units):
        j, tokens = i, 0
        while j < len(units) and (tokens == 0 or tokens + count_tokens(units[j][2]) <= target_tokens):
            tokens += count_tokens(units[j][2])
            j += 1
        start, end = units[i][0], units[j - 1][1]
        body = text[start:end]
        idx = len(chunks)
        chunks.append(TextChunk(
            id=stable_id("chunk", source_id, idx, sha256_text(body)),
            idx=idx, text=body, start_char=start, end_char=end, token_count=count_tokens(body), heading=units[i][3],
        ))
        if j >= len(units):
            break
        # overlap: step back over trailing units totalling <= overlap_tokens, but always advance
        back, k = 0, j
        while k - 1 > i and back + count_tokens(units[k - 1][2]) <= overlap_tokens:
            k -= 1
            back += count_tokens(units[k][2])
        i = k
    return chunks
