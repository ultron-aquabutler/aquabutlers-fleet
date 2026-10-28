"""aquabutler-mdns — the AquaButler device's mDNS advertiser.

Publishes the `_aquabutler._tcp` DNS-SD service on the customer's
residential LAN so a technician's laptop can find the unit and its
current address without the cloud. That is the LAN-local fallback for
the offline-first behaviour the architecture is built around
(`AquaButler-Hardware/docs/Architecture.md` §11).

Policy: `docs/Ethernet-deployment-policy.md` §2 in
`ultron-aquabutler/AquaButler-Server` (PR #35).
Technician procedure: `docs/runbooks/residential-lan-field-guide.md` §2.

The TXT payload carries **identity only**. It must never carry MQTT
credentials, the OpenBalena device token, or the customer's WiFi
passphrase — mDNS is unauthenticated and broadcast to every host on
the segment. `tests/test_no_secrets.py` enforces that mechanically.
"""
from __future__ import annotations

from .avahi_service import (
    ServiceDefinition,
    parse_service_file,
    render_service_file,
)
from .identity import (
    EXPECTED_TXT_KEYS,
    SERVICE_PORT,
    SERVICE_TYPE,
    Advertisement,
    is_valid_unit_id,
    read_customer_id,
    resolve_sw_version,
    resolve_transport,
    resolve_unit_id,
    short_unit_id,
)
from .netinfo import current_lan_ip, list_interface_addresses
from .publisher import Publisher, PublisherConfig, main
from .transport import (
    DEFAULT_REFRESH_INTERVAL_SEC,
    MDNS_TRANSPORT_VALUES,
    STATUS_LINK_LEAF,
    Transport,
    TransportSource,
    TransportState,
    current_transport,
    current_transport_reading,
    mdns_transport_value,
    parse_route_interface,
    parse_route_output,
    transport_for_interface,
)

__version__ = "0.1.0"

__all__ = [
    "Advertisement",
    "DEFAULT_REFRESH_INTERVAL_SEC",
    "EXPECTED_TXT_KEYS",
    "MDNS_TRANSPORT_VALUES",
    "Publisher",
    "PublisherConfig",
    "SERVICE_PORT",
    "SERVICE_TYPE",
    "STATUS_LINK_LEAF",
    "ServiceDefinition",
    "Transport",
    "TransportSource",
    "TransportState",
    "__version__",
    "current_lan_ip",
    "current_transport",
    "current_transport_reading",
    "is_valid_unit_id",
    "list_interface_addresses",
    "main",
    "mdns_transport_value",
    "parse_route_interface",
    "parse_route_output",
    "parse_service_file",
    "read_customer_id",
    "render_service_file",
    "resolve_sw_version",
    "resolve_transport",
    "resolve_unit_id",
    "short_unit_id",
    "transport_for_interface",
]
