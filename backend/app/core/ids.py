"""Stable identifier helpers.

Deterministic IDs make pipeline retries idempotent: re-running the same step
produces the same IDs, so upserts/MERGEs do not create duplicates.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

NAMESPACE = uuid.UUID("6f1c2b8e-3d4a-5b6c-8d7e-9f0a1b2c3d4e")

_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid", "ref"}


def new_id() -> str:
    return str(uuid.uuid4())


def stable_id(*parts: object) -> str:
    """UUIDv5 over the joined parts; same inputs always produce the same ID."""
    return str(uuid.uuid5(NAMESPACE, "\x1f".join(str(p) for p in parts)))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_url(url: str) -> str:
    """Normalize a URL for deduplication (scheme/host case, fragments, tracking params, trailing slash)."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _TRACKING_PARAMS))
    return urlunsplit((scheme, host + port, path, query, ""))


def url_hash(url: str) -> str:
    return sha256_text(canonical_url(url))


def normalize_key(text: str) -> str:
    """Normalization used for entity/alias matching: NFKC, casefold, strip punctuation, collapse spaces."""
    t = unicodedata.normalize("NFKC", text).casefold()
    t = re.sub(r"[‐-―]", "-", t)
    t = re.sub(r"[^\w\s\-+#.]", " ", t)
    t = re.sub(r"(?<!\w)\.|\.(?!\w)", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t
