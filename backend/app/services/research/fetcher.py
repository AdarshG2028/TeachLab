"""SSRF-safe fetching of teacher-supplied URLs.

Protections:
- http/https only, no credentials in URL, default ports or explicit allow list.
- Host is resolved once; every resolved address must be globally routable
  (blocks loopback, private, link-local incl. cloud metadata 169.254.169.254,
  multicast, reserved).
- The connection is pinned to the validated IP (Host header + TLS SNI keep the
  original name), which defeats DNS-rebinding between check and connect.
- Redirects are followed manually (max 5) and each hop is re-validated.
- Response size is capped while streaming; content types are allow-listed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.core.errors import UnsafeURLError, ValidationFailed

ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown", "application/pdf")
ALLOWED_PORTS = {None, 80, 443, 8080, 8443}
USER_AGENT = "TeachLabResearchBot/0.1 (+educational research assistant)"


@dataclass
class FetchedDocument:
    url: str
    final_url: str
    content_type: str
    body: bytes


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not (addr.is_multicast or addr.is_reserved or addr.is_loopback or addr.is_link_local
                                   or addr.is_private or addr.is_unspecified)


async def resolve_public(url: str) -> tuple[str, str]:
    """Validate ``url`` and return (hostname, a validated public IP)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError("Only http(s) URLs are allowed")
    if parts.username or parts.password:
        raise UnsafeURLError("Credentials in URLs are not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    if parts.port not in ALLOWED_PORTS:
        raise UnsafeURLError("Port not allowed")
    if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith((".local", ".internal")):
        raise UnsafeURLError("Local hostnames are not allowed")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                                             type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"Could not resolve host: {host}") from exc
    ips = sorted({info[4][0] for info in infos})
    if not ips or not all(_is_public_ip(ip) for ip in ips):
        raise UnsafeURLError("URL resolves to a non-public address")
    return host, ips[0]


class SafeFetcher:
    def __init__(self, *, max_bytes: int, timeout: float, transport: httpx.AsyncBaseTransport | None = None,
                 resolver=resolve_public) -> None:
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.transport = transport
        self.resolver = resolver

    async def fetch(self, url: str) -> FetchedDocument:
        current = url
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, transport=self.transport) as client:
            for _ in range(6):
                host, ip = await self.resolver(current)
                parts = urlsplit(current)
                ip_host = f"[{ip}]" if ":" in ip else ip
                netloc = ip_host + (f":{parts.port}" if parts.port else "")
                pinned = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
                headers = {"Host": parts.netloc, "User-Agent": USER_AGENT, "Accept": ", ".join(ALLOWED_CONTENT_TYPES)}
                extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
                async with client.stream("GET", pinned, headers=headers, extensions=extensions) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise ValidationFailed("Redirect without location")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code >= 400:
                        raise ValidationFailed(f"Fetch failed with HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype not in ALLOWED_CONTENT_TYPES:
                        raise ValidationFailed(f"Unsupported content type: {ctype or 'unknown'}")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        raise ValidationFailed("Document exceeds size limit")
                    buf = bytearray()
                    async for piece in resp.aiter_bytes():
                        buf.extend(piece)
                        if len(buf) > self.max_bytes:
                            raise ValidationFailed("Document exceeds size limit")
                    return FetchedDocument(url=url, final_url=current, content_type=ctype, body=bytes(buf))
        raise ValidationFailed("Too many redirects")
