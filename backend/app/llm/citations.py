"""Citation-marker parsing shared by chat answers and notes.

Models write passage citations in several styles: ``[S1]``, ``[S1, S2]``, and (notably the gpt-oss
models) full-width ``【S1】``. Every style is parsed so each marker can be validated against the
passages actually provided, then rewritten in one canonical form.
"""

from __future__ import annotations

import re
from collections.abc import Callable

CITE_RE = re.compile(r"[\[【［]\s*(S\d{1,3}(?:\s*[,;]\s*S\d{1,3})*)\s*[\]】］]")
_ONE = re.compile(r"S\d{1,3}")


def rewrite_citations(text: str, render: Callable[[str], str]) -> str:
    """Replace every citation group; ``render(marker)`` returns the replacement ("" drops it).

    Identical renderings inside one group collapse (two passages from one source -> a single ``[1]``).
    """
    return CITE_RE.sub(lambda m: "".join(dict.fromkeys(render(x) for x in _ONE.findall(m.group(1)))), text)


def cited_markers(text: str) -> list[str]:
    return list(dict.fromkeys(x for m in CITE_RE.finditer(text) for x in _ONE.findall(m.group(1))))
