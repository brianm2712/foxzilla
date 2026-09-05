"""
Egress control for the hosted Foxzilla.

A public service that opens connections to hosts its visitors name is, by
default, a way to reach anything the *server* can reach. Point it at
127.0.0.1, the LAN, a tailnet, or a cloud metadata endpoint and it will
happily fetch what the visitor could not. That is the whole risk of hosting
this, so it is handled here rather than sprinkled through the backends.

Two rules:

  1. Resolve the name first and reject it if *any* answer lands in a blocked
     range. Checking the name is useless - `evil.example` can resolve to
     127.0.0.1 as easily as anything else.

  2. Hand the caller the resolved address, and make the caller connect to
     that. Re-resolving at connect time reopens the door: a name can answer
     with a public address for the check and a private one a moment later
     (DNS rebinding).
"""
from __future__ import annotations

import ipaddress
import socket

# Everything that is not the public internet. The tailnet range matters most
# here: this host is on one, so 100.64/10 would otherwise expose every machine
# the user owns.
BLOCKED_V4 = [
    ipaddress.ip_network("0.0.0.0/8"),        # "this network"
    ipaddress.ip_network("10.0.0.0/8"),       # RFC1918
    ipaddress.ip_network("100.64.0.0/10"),    # CGNAT — Tailscale lives here
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("169.254.0.0/16"),   # link-local, incl. cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),    # RFC1918
    ipaddress.ip_network("192.0.0.0/24"),     # IETF protocol assignments
    ipaddress.ip_network("192.168.0.0/16"),   # RFC1918
    ipaddress.ip_network("198.18.0.0/15"),    # benchmarking
    ipaddress.ip_network("224.0.0.0/4"),      # multicast
    ipaddress.ip_network("240.0.0.0/4"),      # reserved, incl. broadcast
]
BLOCKED_V6 = [
    ipaddress.ip_network("::/128"),           # unspecified
    ipaddress.ip_network("::1/128"),          # loopback
    ipaddress.ip_network("fc00::/7"),         # unique local
    ipaddress.ip_network("fe80::/10"),        # link-local
    ipaddress.ip_network("ff00::/8"),         # multicast
    ipaddress.ip_network("::ffff:0:0/96"),    # v4-mapped — checked as v4 below
    ipaddress.ip_network("64:ff9b::/96"),     # NAT64
]

# Ports that are nothing to do with file transfer and everything to do with
# reaching something else on the far side.
ALLOWED_PORTS = {20, 21, 22, 80, 443, 989, 990, 2222, 8080, 8443}

MAX_ADDRESSES = 8


class Refused(Exception):
    """The target is not somewhere this service is willing to connect."""


def _blocked_reason(ip: ipaddress._BaseAddress) -> str | None:
    # A v4-mapped v6 address (::ffff:10.0.0.1) is really a v4 address, and
    # must be judged as one or it slips past the v4 list entirely.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    nets = BLOCKED_V4 if isinstance(ip, ipaddress.IPv4Address) else BLOCKED_V6
    for net in nets:
        if ip in net:
            return f"{ip} is in {net}, which this service will not connect to"
    # Belt and braces: catch anything the explicit lists missed.
    if not ip.is_global:
        return f"{ip} is not a public address"
    return None


def check_address(addr: str) -> None:
    """Raise Refused if this literal address is somewhere we won't go."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        raise Refused(f"{addr!r} is not an IP address")
    reason = _blocked_reason(ip)
    if reason:
        raise Refused(reason)


def resolve(host: str, port: int) -> list[str]:
    """
    Resolve `host`, refuse it if any answer is private, and return the
    addresses the caller should connect to.

    Every answer is checked, not just the first: a name that returns one
    public and one private address is a rebinding attempt wearing a hat.
    """
    if not host or len(host) > 253:
        raise Refused("no host given")
    if port not in ALLOWED_PORTS:
        raise Refused(f"port {port} is not allowed "
                      f"(permitted: {', '.join(map(str, sorted(ALLOWED_PORTS)))})")

    host = host.strip().strip("[]")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise Refused(f"cannot resolve {host!r}: {e.strerror or e}")

    addrs: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise Refused(f"{host!r} resolved to nothing")
    if len(addrs) > MAX_ADDRESSES:
        addrs = addrs[:MAX_ADDRESSES]

    for addr in addrs:
        check_address(addr)          # raises on the first private answer
    return addrs


def safe_target(host: str, port: int) -> str:
    """
    The address to actually connect to.

    Callers must use this rather than the hostname, or the check above is
    advisory only.
    """
    return resolve(host, port)[0]
