"""Active-transport detection — the device's single source of truth.

This module is the mDNS container's half of the shared transport
contract. Its logic is deliberately identical to
`rotary_display.net.transport` in the AquaButler-Server repo
(`src/rotary_display/rotary_display/net/transport.py`), which is the
other half. Both containers run host-networked on the same device and
therefore see the same kernel routing table, so both independently
compute the same answer.

The two copies are kept in step by
`tests/test_transport.py::test_transport_implementation_matches_the_rotary_display`,
which compares this implementation against the rotary display's copy
when both are available on the same machine. If they ever disagree,
that test fails — which is the point. Do not "fix" a divergence by
editing one side only.

Why this matters: a technician comparing the rotary display's
`LINK: ETH` line against the mDNS `transport` TXT record must never
see a contradiction. The policy is `docs/Ethernet-deployment-policy.md`
§5 in `ultron-aquabutler/AquaButler-Server` (PR #35); the technician
procedure is `docs/runbooks/residential-lan-field-guide.md` §5.

The AquaButler policy is **last-connected wins**: whichever transport
the host last connected on is the one the supervisor actually uses.
There is no fixed Ethernet-over-WiFi preference.

That makes "which transport is active?" a *runtime* fact, not a
configuration fact — and it is NOT the same question as "does eth0 have
a negotiated link?". The ETH_LEDG / ETH_LEDY LEDs on the carrier answer
the link question; this module answers the transport question.

Two sources, in priority order:

1. **Local route query** (`ip route get 1.1.1.1`) — the authoritative
   answer. It names the interface the kernel would actually use to
   reach the internet, which is exactly the interface the supervisor's
   connections ride. This is the same command the technician field
   guide tells people to run by hand.

2. **Environment override** (`AQUABUTLER_TRANSPORT`) — an explicit
   operator action for bench testing, or for a container that cannot
   see the host's routing table. It is never an inference.

Interface → transport mapping:

| interface        | transport |
|------------------|-----------|
| `eth0`           | `ETH`     |
| `wlan0`/`wlan1`  | `WIFI`    |
| `wwan0`/`usb0`   | `LTE`     |
| anything else    | `UNKNOWN` |

The mapping is deliberately conservative: an interface we don't
recognise is UNKNOWN rather than a guess.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from enum import Enum


log = logging.getLogger(__name__)


# Probe destination — a public anycast resolver, used only to ask the
# kernel "which interface would you use". No traffic is sent to it.
ROUTE_PROBE_TARGET = "1.1.1.1"

# `ip route get 1.1.1.1` prints e.g.:
#   1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.42 uid 0
#   1.1.1.1 dev wlan0 src 10.0.0.5 uid 0
# The `dev <iface>` token is the answer. When there is no route at all
# the command exits non-zero and prints "RTNETLINK answers: Network
# is unreachable" on stderr.
#
# The interface name is matched tightly: Linux interface names are
# letters, digits, `.`, `_` and `-`, and they cannot contain whitespace.
# A looser pattern would happily match the words "no dev token here"
# and produce an interface named "token" — which would then be
# reported as an unknown interface rather than "no interface", a
# distinction that matters when diagnosing a unit that advertises no
# address.
_ROUTE_DEV_RE = re.compile(r"\bdev\s+([A-Za-z0-9][A-Za-z0-9._-]*)")


class Transport(str, Enum):
    """The transport the supervisor is actually using."""

    ETH = "ETH"
    WIFI = "WIFI"
    LTE = "LTE"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_str(cls, value: str | None) -> "Transport":
        """Parse a transport name from an untrusted string.

        Accepts the display's upper-case forms (`ETH`), the mDNS TXT
        form (`eth`), and interface names (`eth0`, `wlan0`). Returns
        UNKNOWN for anything unrecognised rather than guessing.
        """
        if not value:
            return cls.UNKNOWN
        v = value.strip().lower()
        if not v:
            return cls.UNKNOWN
        # Interface names first — "eth0" must not match "eth" by
        # accident, but it does map to the same transport.
        mapped = transport_for_interface(v)
        if mapped is not None:
            return mapped
        if v in ("eth", "ethernet"):
            return cls.ETH
        if v in ("wifi", "wi-fi", "wlan", "wireless"):
            return cls.WIFI
        if v in ("lte", "cellular", "wwan", "usb"):
            return cls.LTE
        return cls.UNKNOWN

    @property
    def is_known(self) -> bool:
        return self is not Transport.UNKNOWN


# Interface-name → transport. Exact matches against this map.
_INTERFACE_MAP: dict[str, Transport] = {
    "eth0": Transport.ETH,
    "eth1": Transport.ETH,
    "enp1s0": Transport.ETH,
    "enx": Transport.ETH,  # predictable-name prefix, see below
    "wlan0": Transport.WIFI,
    "wlan1": Transport.WIFI,
    "wlp2s0": Transport.WIFI,
    "wwan0": Transport.LTE,
    "wwan1": Transport.LTE,
    "usb0": Transport.LTE,
}


def transport_for_interface(iface: str | None) -> Transport | None:
    """Map a Linux interface name to a transport, or None if unknown.

    Handles predictable interface names (`enx001122334455`, `wlp3s0`)
    by prefix-matching the `enx` / `enp` / `wlp` / `wwan` families.
    """
    if not iface:
        return None
    name = iface.strip().lower()
    if name in _INTERFACE_MAP:
        return _INTERFACE_MAP[name]
    # Predictable names: enx<mac>, enp<bus>, wlp<bus>, wwan<n>.
    if name.startswith(("enx", "enp", "eno", "ens")):
        return Transport.ETH
    if name.startswith("wlp"):
        return Transport.WIFI
    if name.startswith("wwan"):
        return Transport.LTE
    return None


@dataclass(frozen=True)
class TransportReading:
    """One observation of the active transport.

    `transport` is UNKNOWN when no source could answer. `source`
    records *where* the answer came from so an operator reading the
    logs can tell a real reading from a default.

    `interface` is the *name* of the interface the kernel would use
    (`eth0`, `wlan0`). It is the finer-grained fact than `transport`:
    the transport says what kind of link, the interface says which
    one. The mDNS container needs the interface name to advertise
    the address of the interface actually in use — advertising the
    address of a different interface would send a technician to the
    wrong host. It is empty when the transport came from the
    environment override (which names a transport, not an interface)
    or when nothing could answer.
    """

    transport: Transport
    source: str  # "route" | "env" | "none"
    interface: str = ""

    @property
    def is_known(self) -> bool:
        return self.transport.is_known


UNKNOWN_READING = TransportReading(transport=Transport.UNKNOWN, source="none")


def parse_route_interface(output: str) -> str:
    """Extract the interface name from `ip route get` output.

    Returns an empty string when the output does not name a `dev
    <iface>` token. This is the pure, unit-tested boundary between
    the subprocess call and the transport logic.
    """
    if not output:
        return ""
    m = _ROUTE_DEV_RE.search(output)
    if not m:
        return ""
    return m.group(1)


def parse_route_output(output: str) -> Transport:
    """Extract the transport from `ip route get` output.

    Returns UNKNOWN when the output does not name a `dev <iface>` token
    we recognise. This function is pure — it is the unit-tested
    boundary between the subprocess call and the transport logic.
    """
    iface = parse_route_interface(output)
    if not iface:
        return Transport.UNKNOWN
    mapped = transport_for_interface(iface)
    return mapped if mapped is not None else Transport.UNKNOWN


def query_active_transport(
    runner=subprocess.run,
    target: str = ROUTE_PROBE_TARGET,
    timeout: float = 2.0,
) -> TransportReading:
    """Ask the kernel which interface it would use to reach `target`.

    `runner` is injectable so the tests never shell out. The default
    is `subprocess.run`.

    Returns UNKNOWN_READING when the query fails for any reason
    (no `ip` binary, no route, permission denied, timeout) — the
    caller must never fall back to a guess.
    """
    cmd = ["ip", "route", "get", target]
    try:
        proc = runner(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # FileNotFoundError (no `ip`), TimeoutExpired, etc.
        log.debug("route query failed: %s", exc)
        return UNKNOWN_READING

    # subprocess.run with check=False never raises on non-zero exit;
    # a non-zero exit means "no route" for our purposes.
    if proc.returncode != 0:
        log.debug("route query rc=%s: no route to %s", proc.returncode, target)
        return UNKNOWN_READING

    transport = parse_route_output(proc.stdout or "")
    if not transport.is_known:
        log.debug("route query returned unrecognised interface: %r", proc.stdout)
        return UNKNOWN_READING
    return TransportReading(
        transport=transport,
        source="route",
        interface=parse_route_interface(proc.stdout or ""),
    )


def current_transport(
    runner=subprocess.run,
    target: str = ROUTE_PROBE_TARGET,
    timeout: float = 2.0,
) -> Transport:
    """Convenience wrapper returning just the `Transport`."""
    return query_active_transport(runner=runner, target=target, timeout=timeout).transport


def current_transport_reading(
    runner=subprocess.run,
    target: str = ROUTE_PROBE_TARGET,
    timeout: float = 2.0,
) -> TransportReading:
    """The full reading — transport *and* the interface it came from.

    This is what the mDNS container uses: it needs the interface name
    to advertise the address of the interface actually in use.
    """
    return query_active_transport(runner=runner, target=target, timeout=timeout)


def transport_env_override() -> Transport | None:
    """Read an operator override from the environment, if set.

    `AQUABUTLER_TRANSPORT=eth|wifi|lte` forces the reported transport.
    This exists for bench testing and for the (unlikely) case where the
    container cannot see the host's routing table. It is an explicit
    operator action, not an inference — so it does not violate the
    "must not guess" rule.
    """
    raw = os.environ.get("AQUABUTLER_TRANSPORT")
    if not raw:
        return None
    t = Transport.from_str(raw)
    return t if t.is_known else None


# The value form the mDNS TXT record uses (the policy names `eth` /
# `wifi`). The display uses the upper-case form.
MDNS_TRANSPORT_VALUES: dict[Transport, str] = {
    Transport.ETH: "eth",
    Transport.WIFI: "wifi",
    Transport.LTE: "lte",
}


def mdns_transport_value(transport: Transport | str | None) -> str:
    """Map a transport to the mDNS TXT value.

    An unknown transport returns an empty string — the caller must
    then omit the `transport` key entirely rather than advertise a
    value it cannot vouch for.
    """
    if transport is None:
        return ""
    if isinstance(transport, str):
        try:
            transport = Transport(transport)
        except ValueError:
            try:
                transport = Transport.from_str(transport)
            except ValueError:
                return ""
    return MDNS_TRANSPORT_VALUES.get(transport, "")


__all__ = [
    "Transport",
    "TransportReading",
    "UNKNOWN_READING",
    "ROUTE_PROBE_TARGET",
    "parse_route_interface",
    "parse_route_output",
    "query_active_transport",
    "current_transport",
    "current_transport_reading",
    "transport_for_interface",
    "transport_env_override",
    "MDNS_TRANSPORT_VALUES",
    "mdns_transport_value",
]
