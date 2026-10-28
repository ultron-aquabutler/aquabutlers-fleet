"""Device identity resolution for the `_aquabutler._tcp` advertisement.

The TXT payload carries **identity only**. mDNS is unauthenticated and
broadcast to every host on the segment, so anything secret that lands
here leaks to the customer's neighbours. `tests/test_no_secrets.py`
enforces that mechanically — see `SECRET_FORBIDDEN_PATTERNS`.

Sources, in priority order:

| Field | Source | Why |
|---|---|---|
| `unit_id` | `AQUABUTLER_UNIT_ID` → `BALENA_DEVICE_UUID` | The balena device UUID is the stable per-device handle the policy §1 names. It survives re-flash, unlike the address. |
| `customer_id` | `/mnt/data/customer_id` | The provisioner (`aquabutlers-provisioner`, `src/customerId.ts`) persists the customer ID on the device's persistent volume at exactly this path. |
| `sw_version` | `AQUABUTLER_SW_VERSION` → image label | The mDNS container's own version. |
| `ip` | live address of the active interface | The device's *current* residential-LAN address — the whole point of the advertisement. |
| `transport` | `aquabutler_mdns.transport` | The shared transport source (same answer the rotary display shows). |

The `unit_id` is used as the mDNS host name: `<unit_id>.local`.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .transport import (
    Transport,
    current_transport_reading,
    mdns_transport_value,
    transport_env_override,
)


log = logging.getLogger(__name__)


# The advertised DNS-SD service type.
SERVICE_TYPE = "_aquabutler._tcp"

# The port the device's own local UI (dashPanel HTTP) listens on.
SERVICE_PORT = 5150

# Where the provisioner persists the customer ID on the device.
DEFAULT_CUSTOMER_ID_PATH = "/mnt/data/customer_id"

# The balenaOS supervisor injects the device UUID as BALENA_DEVICE_UUID
# (and RESIN_DEVICE_UUID for backwards compatibility).
DEVICE_UUID_ENV_VARS = ("AQUABUTLER_UNIT_ID", "BALENA_DEVICE_UUID", "RESIN_DEVICE_UUID")

# The exact set of TXT keys this advertisement may carry. A unit test
# asserts the emitted payload's key set equals this, and that nothing
# else can sneak in.
EXPECTED_TXT_KEYS = frozenset({"unit_id", "customer_id", "sw_version", "transport", "ip"})

# A unit_id must be a safe DNS label and a safe TXT value: letters,
# digits and dashes. A raw balena UUID is 64 hex chars — legal as a
# DNS label but unreadable in a terminal. `short_unit_id` truncates
# it to a readable prefix; the full value stays in the `unit_id` TXT
# record so identity is never lost.
_UNIT_ID_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

# The readable short form used for the `<unit_id>.local` host name.
SHORT_UNIT_ID_LEN = 12


def is_valid_unit_id(value: str | None) -> bool:
    """True when `value` is usable as an mDNS host name label."""
    if not value:
        return False
    return bool(_UNIT_ID_RE.match(value))


def short_unit_id(value: str | None) -> str:
    """Derive the readable `<unit_id>.local` label from a unit ID.

    A balena device UUID is 64 hex characters. That is a legal DNS
    label but nobody can type it on a technician's laptop, so the
    host name uses a 12-character prefix. The *full* value is still
    carried in the `unit_id` TXT record.

    The prefix is hex-only-safe for a UUID; for a short human-set ID
    (`aqb-0007`) the value is returned unchanged.
    """
    if not value:
        return ""
    v = value.strip()
    if len(v) <= SHORT_UNIT_ID_LEN:
        return v
    return v[:SHORT_UNIT_ID_LEN]


def read_customer_id(path: str | os.PathLike[str] = DEFAULT_CUSTOMER_ID_PATH) -> str:
    """Read the customer ID from the device's persistent volume.

    Returns an empty string when the file is missing or unreadable —
    an unprovisioned unit simply omits the `customer_id` TXT key
    rather than advertising a wrong one.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.debug("customer id unavailable at %s: %s", path, exc)
        return ""
    return raw.strip()


def resolve_unit_id(env: Mapping[str, str] | None = None) -> str:
    """Resolve the unit's stable identity.

    `AQUABUTLER_UNIT_ID` wins (an operator can set it per device in
    the balena dashboard); otherwise the balena device UUID is used.
    """
    e = os.environ if env is None else env
    for name in DEVICE_UUID_ENV_VARS:
        raw = e.get(name)
        if raw and raw.strip():
            return raw.strip()
    return ""


def resolve_sw_version(env: Mapping[str, str] | None = None) -> str:
    """Resolve the advertised software version.

    `AQUABUTLER_SW_VERSION` is set in the image (Dockerfile `ENV`) and
    can be overridden per device. An empty value means the key is
    omitted from the payload.
    """
    e = os.environ if env is None else env
    return (e.get("AQUABUTLER_SW_VERSION") or "").strip()


def resolve_transport(
    env: Mapping[str, str] | None = None,
) -> tuple[str, Transport, str]:
    """Resolve the active transport, its mDNS TXT form, and the interface.

    Returns `(txt_value, transport, interface)`. The TXT value is
    empty when the transport is unknown — the caller then omits the
    `transport` key rather than advertising a value it cannot vouch
    for.

    The interface is the name of the interface the kernel would
    actually use (`eth0`, `wlan0`). It is empty when the transport
    came from the operator override (which names a transport, not an
    interface) or when nothing could answer.

    Precedence: the `AQUABUTLER_TRANSPORT` operator override, then the
    local route query. Both come from the shared transport source, so
    this agrees with the rotary display.
    """
    e = os.environ if env is None else env
    override = transport_env_override()
    if override is not None and override.is_known:
        return mdns_transport_value(override), override, ""

    reading = current_transport_reading()
    return mdns_transport_value(reading.transport), reading.transport, reading.interface


@dataclass
class Advertisement:
    """One resolved snapshot of the advertisement's payload."""

    unit_id: str = ""
    customer_id: str = ""
    sw_version: str = ""
    transport: str = ""
    ip: str = ""

    def txt_records(self) -> list[tuple[str, str]]:
        """The TXT key/value pairs, in the canonical order.

        Keys with an empty value are **omitted** — the payload never
        advertises a value it could not resolve. The resulting key set
        is always a subset of `EXPECTED_TXT_KEYS`.
        """
        pairs = [
            ("unit_id", self.unit_id),
            ("customer_id", self.customer_id),
            ("sw_version", self.sw_version),
            ("transport", self.transport),
            ("ip", self.ip),
        ]
        return [(k, v) for k, v in pairs if v != ""]

    def txt_keys(self) -> set[str]:
        return {k for k, _ in self.txt_records()}


__all__ = [
    "Advertisement",
    "DEFAULT_CUSTOMER_ID_PATH",
    "DEVICE_UUID_ENV_VARS",
    "EXPECTED_TXT_KEYS",
    "SERVICE_PORT",
    "SERVICE_TYPE",
    "SHORT_UNIT_ID_LEN",
    "is_valid_unit_id",
    "mdns_transport_value",
    "read_customer_id",
    "resolve_sw_version",
    "resolve_transport",
    "resolve_unit_id",
    "short_unit_id",
]
