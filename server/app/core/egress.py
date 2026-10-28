"""Outbound address policy for configurable providers (V2 design §6.3).

Checks run on save, test, activate and connect. ``resolve`` returns one
validated IP that the caller must connect to directly (no second lookup),
while TLS SNI/certificate checks keep using the original host name.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

_LOCAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    '127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '::1/128', 'fc00::/7'))
_SECURE = {'asr': 'wss', 'llm': 'https'}
_INSECURE = {'asr': 'ws', 'llm': 'http'}
_WORKSPACE = re.compile(r'ws-[A-Za-z0-9]+')

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


class EgressDenied(ValueError):
    """Address rejected by policy; the message never contains the address."""


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    local: bool


def redact_host(text: str) -> str:
    """Mask Bailian workspace ids (``ws-…``) in hosts, URLs and log lines."""
    return _WORKSPACE.sub('ws-…', text)


def _ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_local_ip(address) -> bool:
    return any(address in network for network in _LOCAL_NETWORKS)


def _is_public_ip(address) -> bool:
    # is_global already excludes loopback, private, link-local, CGNAT and
    # documentation ranges; multicast/reserved are rejected explicitly.
    return address.is_global and not (address.is_multicast or address.is_reserved)


async def _system_resolver(host: str, port: int) -> Sequence[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


class EgressPolicy:
    def __init__(self, mode: str = 'deny-public', allowlist: Sequence[str] = (), *,
                 resolver: Resolver | None = None):
        if mode not in {'deny-public', 'allowlist'}:
            raise ValueError('egress_policy must be deny-public or allowlist')
        self.mode = mode
        self.allowlist = tuple(entry.strip().lower().rstrip('.') for entry in allowlist)
        self._resolver = resolver or _system_resolver

    def _allowlisted(self, host: str) -> bool:
        if self.mode != 'allowlist':
            return False
        for entry in self.allowlist:
            if entry.startswith('*.'):
                if host.endswith(entry[1:]) and host != entry[2:]:
                    return True
            elif host == entry:
                return True
        return False

    def check_url(self, url: str, *, kind: str) -> Target:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise EgressDenied('invalid address') from exc
        if parts.username is not None or parts.password is not None or not parts.hostname:
            raise EgressDenied('invalid address')
        host = parts.hostname.lower().rstrip('.')
        address = _ip(host)
        local = host == 'localhost' or (address is not None and _is_local_ip(address))
        if parts.scheme != _SECURE[kind] and not (local and parts.scheme == _INSECURE[kind]):
            raise EgressDenied('scheme not allowed')
        default_port = 443 if parts.scheme in {'wss', 'https'} else 80
        port = port or default_port
        if local:
            return Target(parts.scheme, host, port, True)
        if not self._allowlisted(host):
            raise EgressDenied('host not allowlisted')
        if address is not None and not _is_public_ip(address):
            raise EgressDenied('address not public')
        if port != 443:
            raise EgressDenied('port not allowed')
        return Target(parts.scheme, host, port, False)

    async def resolve(self, target: Target) -> str:
        """Resolve once and validate every answer; return the IP to pin."""
        literal = _ip(target.host)
        if literal is not None:
            answers = [target.host]
        else:
            try:
                answers = list(await self._resolver(target.host, target.port))
            except OSError as exc:
                raise EgressDenied('resolution failed') from exc
        if not answers:
            raise EgressDenied('resolution failed')
        for answer in answers:
            address = _ip(answer)
            if address is None:
                raise EgressDenied('resolution failed')
            if target.local:
                if not _is_local_ip(address):
                    raise EgressDenied('local host resolved outside local ranges')
            elif not _is_public_ip(address):
                raise EgressDenied('public host resolved to non-public address')
        return answers[0]
