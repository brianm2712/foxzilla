#!/usr/bin/env python3
"""
The egress guard is the only thing standing between a hosted Foxzilla and
"fetch anything on my network for me", so it gets tested harder than the rest.
"""
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import egress

ok = True


def check(label, cond, extra=""):
    global ok
    print(("  PASS " if cond else "  FAIL ") + label + (f"   {extra}" if extra else ""))
    ok = ok and bool(cond)


def refuses(addr):
    try:
        egress.check_address(addr)
        return False
    except egress.Refused:
        return True


print("\nthe places that matter most")
for addr, why in [
    ("127.0.0.1", "loopback"),
    ("127.0.0.53", "loopback resolver"),
    ("0.0.0.0", "unspecified"),
    ("10.1.2.3", "RFC1918"),
    ("172.16.5.4", "RFC1918"),
    ("172.31.255.254", "RFC1918 upper edge"),
    ("192.168.68.54", "this LAN — proxmox itself"),
    ("100.102.227.73", "the tailnet — proxmox"),
    ("100.66.97.29", "the tailnet — talos"),
    ("100.64.0.1", "tailnet lower edge"),
    ("100.127.255.254", "tailnet upper edge"),
    ("169.254.169.254", "cloud metadata"),
    ("169.254.1.1", "link-local"),
    ("224.0.0.1", "multicast"),
    ("255.255.255.255", "broadcast"),
]:
    check(f"refuses {addr:<16} ({why})", refuses(addr))

print("\nIPv6, including the v4-mapped trick")
for addr, why in [
    ("::1", "loopback"),
    ("fe80::1", "link-local"),
    ("fd00::1", "unique local"),
    ("::ffff:127.0.0.1", "v4-mapped loopback"),
    ("::ffff:10.0.0.1", "v4-mapped RFC1918"),
    ("::ffff:100.102.227.73", "v4-mapped tailnet"),
]:
    check(f"refuses {addr:<22} ({why})", refuses(addr))

print("\npublic addresses are allowed through")
for addr in ["1.1.1.1", "8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"]:
    check(f"allows {addr}", not refuses(addr))

print("\nports")
try:
    egress.resolve("one.one.one.one", 22)
    check("allows a file-transfer port (22)", True)
except egress.Refused as e:
    check("allows a file-transfer port (22)", False, str(e))
for port, why in [(25, "SMTP"), (3306, "MySQL"), (6379, "Redis"),
                  (11211, "memcached"), (8096, "Jellyfin"), (8091, "the SIEM")]:
    try:
        egress.resolve("one.one.one.one", port)
        check(f"refuses port {port} ({why})", False, "was allowed")
    except egress.Refused:
        check(f"refuses port {port} ({why})", True)

print("\nresolution: the name is not trusted, the answer is")
try:
    addrs = egress.resolve("localhost", 22)
    check("refuses a name that resolves to loopback", False, f"allowed {addrs}")
except egress.Refused as e:
    check("refuses a name that resolves to loopback", True, str(e)[:50])

# A name is only as safe as every address it returns.
real = socket.getaddrinfo
def mixed(host, port, *a, **k):
    if host == "sneaky.test":
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]
    return real(host, port, *a, **k)
socket.getaddrinfo = mixed
try:
    egress.resolve("sneaky.test", 22)
    check("refuses a name with one public and one private answer", False, "allowed")
except egress.Refused as e:
    check("refuses a name with one public and one private answer", True, str(e)[:46])
socket.getaddrinfo = real

print("\nthe caller is handed an address, not a name (defeats rebinding)")
try:
    target = egress.safe_target("one.one.one.one", 443)
    egress.check_address(target)           # must itself be a valid public IP
    check("safe_target returns a checked address", True, target)
except Exception as e:
    check("safe_target returns a checked address", False, str(e))

print("\nrubbish input")
for host in ["", " ", "x" * 300, "not a host name at all"]:
    try:
        egress.resolve(host, 22)
        check(f"refuses {host[:22]!r}", False, "allowed")
    except egress.Refused:
        check(f"refuses {host[:22]!r}", True)

print(f"\n{'ALL PASS' if ok else '*** FAILURES ***'}")
sys.exit(0 if ok else 1)
