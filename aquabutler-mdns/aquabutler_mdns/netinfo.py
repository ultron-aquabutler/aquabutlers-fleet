"""Current residential-LAN address resolution.

The `ip` TXT record carries the device's **current** address on the
customer's LAN. Under DHCP that address changes — the advertisement
must follow it, or a technician reads a stale address off the mDNS
record and chases a ghost (exactly what the field guide §1 warns
against).

The address is resolved from the **active** interface — the same
interface the shared transport source names. That is deliberate: the
unit may have both `eth0` and `wlan0` up, but only one is the
supervisor's path out (policy §5, "last-connected wins"). Advertising
the address of an interface the unit is not using would send the
technician to the wrong host.

If the active interface cannot be determined, we fall back to the
address of any interface that has a usable LAN address, so the
technician still gets *an* address rather than nothing. The chosen
address always comes from a real interface — never a guess.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import subprocess
from dataclasses import dataclass


log = logging.getLogger(__name__)


# `ip -j -4 addr show` returns JSON; we avoid a JSON dependency by
# parsing the plain-text form instead, which is stable and simple.
#
#   2: eth0    inet 192.168.1.42/24 brd 192.168.1.255 scope global eth0
#       valid_lft forever preferred_lft forever
_ADDR_RE = re.compile(
    r"^\s*(?P<ifindex>\d+):\s+(?P<iface>[^:\s]+):\s*(?P<flags>.*)$"
)
_INET_RE = re.compile(r"\binet\s+(?P<addr>[0-9.]+)(?:/(?P<prefix>\d+))?")

# Interfaces that never carry the residential-LAN address.
_SKIP_IFACES = frozenset({"lo", "docker0", "balena0", "tun0", "tap0", "wg0"})


@dataclass(frozen=True)
class InterfaceAddress:
    """An IPv4 address on a named interface."""

    iface: str
    address: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.iface}={self.address}"


def parse_ip_addr_output(output: str) -> list[InterfaceAddress]:
    """Parse `ip -4 addr show` output into interface/address pairs.

    Only global-scope IPv4 addresses are returned — link-local
    (169.254.x.x) and loopback addresses are useless to a technician
    on the customer's LAN.
    """
    results: list[InterfaceAddress] = []
    current_iface: str | None = None

    for line in (output or "").splitlines():
        header = _ADDR_RE.match(line)
        if header:
            current_iface = header.group("iface")
            continue
        if current_iface is None:
            continue
        m = _INET_RE.search(line)
        if not m:
            continue
        raw = m.group("addr")
        try:
            addr = ipaddress.IPv4Address(raw)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            continue
        results.append(InterfaceAddress(iface=current_iface, address=str(addr)))

    return results


def list_interface_addresses(
    runner=subprocess.run,
    timeout: float = 2.0,
) -> list[InterfaceAddress]:
    """List the global IPv4 addresses on the host's interfaces."""
    cmd = ["ip", "-4", "addr", "show"]
    try:
        proc = runner(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("ip addr show failed: %s", exc)
        return []
    if proc.returncode != 0:
        log.debug("ip addr show rc=%s", proc.returncode)
        return []
    return parse_ip_addr_output(proc.stdout or "")


def current_lan_ip(
    active_iface: str | None = None,
    runner=subprocess.run,
    timeout: float = 2.0,
) -> str:
    """Resolve the device's current residential-LAN IPv4 address.

    `active_iface` is the interface the supervisor is actually using
    (from the shared transport source). Its address is preferred. If
    that interface has no usable address — it may have just come up,
    or lost its lease — any other interface's global address is used
    as a fallback, so the technician still gets a usable answer.

    Returns an empty string when no interface has a usable address;
    the caller then omits the `ip` TXT key.
    """
    addresses = list_interface_addresses(runner=runner, timeout=timeout)
    if not addresses:
        return ""

    if active_iface:
        for entry in addresses:
            if entry.iface == active_iface:
                return entry.address

    # Fall back to any usable address, preferring wired interfaces
    # (the CM4's Ethernet port is the normal install) and skipping
    # virtual interfaces.
    candidates = [a for a in addresses if a.iface not in _SKIP_IFACES]
    if not candidates:
        candidates = addresses
    if not candidates:
        return ""

    candidates.sort(key=lambda a: (not a.iface.startswith("eth"), a.iface))
    return candidates[0].address


__all__ = [
    "InterfaceAddress",
    "current_lan_ip",
    "list_interface_addresses",
    "parse_ip_addr_output",
]
