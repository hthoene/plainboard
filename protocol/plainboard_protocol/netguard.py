"""Addresses on the public internet only: what PlainBoard fetches for an agent from a URL it was given (a product's
website, #128) must not reach PlainBoard itself, the runner, the local network or a cloud's metadata service."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class NotPublic(ValueError):
    pass


def public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve(host: str, port: int) -> list[str]:
    """The host's addresses; all of them have to be public (NotPublic else)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise NotPublic(f"{host} cannot be resolved") from exc
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses or not all(public_ip(a) for a in addresses):
        raise NotPublic(f"{host} is not a public address")
    return addresses


def check_url(url: str) -> tuple[str, int]:
    """(host, port) of an http(s) address on a public host; NotPublic for anything else (blocking: DNS)."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotPublic("Only http(s) addresses")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise NotPublic("Invalid port") from exc
    if parts.username or parts.password:
        raise NotPublic("No credentials in the address")
    host = parts.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise NotPublic(f"{host} is not a public address")
    resolve(host, port)
    return host, port
