"""The advertised `ip` must come from the *active* interface.

The unit may have both `eth0` and `wlan0` up at the same time. Only
one of them is the supervisor's path out (policy §5, "last-connected
wins"), and only that one's address is the address a technician can
actually reach the unit on.

So the address lookup is interface-aware: the transport source names
the interface (`dev eth0` in the route output), and the address comes
from *that* interface.

This test file guards the seam between the two. The bug it exists to
catch is subtle: the transport source knows the transport *class*
(ETH / WIFI) but the address lookup needs the interface *name*
(`eth0`), and a bridge between the two that silently passes `None`
produces a plausible-but-wrong address — the unit advertises the
WiFi address while the technician is unplugging the Ethernet cable.

The `ip` TXT value and the `transport` TXT value must always describe
the *same* interface. A unit advertising `transport=eth` with the
address of `wlan0` is the failure mode.
"""
from __future__ import annotations

import pytest

from aquabutler_mdns.identity import Advertisement, resolve_transport
from aquabutler_mdns.netinfo import current_lan_ip, parse_ip_addr_output
from aquabutler_mdns.publisher import Publisher, PublisherConfig
from aquabutler_mdns.transport import (
    Transport,
    parse_route_interface,
    query_active_transport,
    transport_for_interface,
)


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def fake_runner(returncode=0, stdout="", stderr=""):
    def runner(cmd, **kwargs):
        return FakeProc(returncode, stdout, stderr)

    return runner


# Two interfaces are up at once — the CM4's Ethernet port and WiFi.
# The route says the kernel would use eth0, so eth0's address is the
# one the technician can reach the unit on.
TWO_IFACE_ADDR_SHOW = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN group default qlen 1000
    inet 127.0.0.1/8 scope host lo
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc pfifo_fast state UP group default qlen 1000
    inet 192.168.1.42/24 brd 192.168.1.255 scope global eth0
3: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc pfifo_fast state UP group default qlen 1000
    inet 10.0.0.5/24 brd 10.0.0.255 scope global wlan0
"""


# ---------------------------------------------------------------------
# The route output carries the interface name
# ---------------------------------------------------------------------


def test_parse_route_interface_extracts_the_dev_token():
    output = "1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.42 uid 0"
    assert parse_route_interface(output) == "eth0"


def test_parse_route_interface_on_wifi():
    assert parse_route_interface("1.1.1.1 dev wlan0 src 10.0.0.5 uid 0") == "wlan0"


@pytest.mark.parametrize(
    "output",
    [
        "",
        "RTNETLINK answers: Network is unreachable",
        "no route token here",
        "1.1.1.1 via 192.168.1.1 src 192.168.1.42 uid 0",
    ],
)
def test_parse_route_interface_absent_is_empty(output):
    assert parse_route_interface(output) == ""


def test_a_matched_but_unrecognised_interface_is_unknown_not_a_guess():
    """`dev` followed by an unknown interface name yields no transport.

    The interface name is still parsed out — but the transport stays
    UNKNOWN because the name is not one we recognise. The unit
    advertises no transport rather than a wrong one.
    """
    output = "1.1.1.1 dev br-nonexistent src 10.1.2.6 uid 0"
    assert parse_route_interface(output) == "br-nonexistent"
    assert transport_for_interface("br-nonexistent") is None


def test_query_carries_the_interface_name():
    """The reading must carry the interface, not just the transport class."""
    runner = fake_runner(stdout="1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.42 uid 0")
    reading = query_active_transport(runner=runner)
    assert reading.transport is Transport.ETH
    assert reading.interface == "eth0"


def test_query_without_a_route_has_no_interface():
    runner = fake_runner(returncode=2, stderr="RTNETLINK answers: Network is unreachable")
    reading = query_active_transport(runner=runner)
    assert reading.transport is Transport.UNKNOWN
    assert reading.interface == ""


# ---------------------------------------------------------------------
# The address comes from the active interface
# ---------------------------------------------------------------------


def test_current_lan_ip_prefers_the_active_interface():
    """With eth0 active, the eth0 address wins — not wlan0's."""
    runner = fake_runner(stdout=TWO_IFACE_ADDR_SHOW)
    assert current_lan_ip(active_iface="eth0", runner=runner) == "192.168.1.42"


def test_current_lan_ip_follows_the_interface_when_wifi_is_active():
    """The same two interfaces, wlan0 active — the answer must flip."""
    runner = fake_runner(stdout=TWO_IFACE_ADDR_SHOW)
    assert current_lan_ip(active_iface="wlan0", runner=runner) == "10.0.0.5"


def test_current_lan_ip_falls_back_when_the_active_interface_has_no_address():
    """An interface that just came up has no lease yet.

    The technician still gets *an* address rather than nothing — but
    it is the fallback, and the unit must not crash.
    """
    runner = fake_runner(stdout=TWO_IFACE_ADDR_SHOW)
    # eth0 is active but the address list has no eth0 entry.
    assert current_lan_ip(active_iface="eth9", runner=runner) in (
        "192.168.1.42",
        "10.0.0.5",
    )


def test_current_lan_ip_with_no_active_interface_prefers_wired():
    runner = fake_runner(stdout=TWO_IFACE_ADDR_SHOW)
    assert current_lan_ip(active_iface=None, runner=runner) == "192.168.1.42"


# ---------------------------------------------------------------------
# The publisher wires the two together
# ---------------------------------------------------------------------


def test_publisher_advertises_the_active_interface_address(monkeypatch, tmp_path):
    """End-to-end seam: route -> transport + interface -> advertised ip.

    This is the test that fails if the publisher drops the interface
    name on the floor between the transport source and the address
    lookup. Before the fix the publisher passed `None` for the active
    interface, so the unit advertised the wired address even while
    the route said WiFi was active.

    The transport is injected as a `TransportSource` — the same
    object the publisher holds in production — so the test drives the
    real resolution path rather than a stand-in for it.
    """
    route_output = "1.1.1.1 dev wlan0 src 10.0.0.5 uid 0"
    addr_output = TWO_IFACE_ADDR_SHOW

    def fake_route_runner(cmd, **kwargs):
        return FakeProc(0, route_output)

    monkeypatch.delenv("AQUABUTLER_TRANSPORT", raising=False)
    monkeypatch.setenv("AQUABUTLER_UNIT_ID", "aqb0007abcd")
    monkeypatch.setenv("AQUABUTLER_SW_VERSION", "1.2.3")

    import aquabutler_mdns.netinfo as netinfo_module
    from aquabutler_mdns.transport import TransportSource

    # Patch the subprocess seams the publisher uses.
    monkeypatch.setattr(
        netinfo_module,
        "list_interface_addresses",
        lambda runner=None, timeout=2.0: parse_ip_addr_output(addr_output),
    )

    publisher = Publisher(
        config=PublisherConfig(service_file=tmp_path / "aquabutler.service"),
        env={},
        transport_source=TransportSource(
            query=lambda: query_active_transport(runner=fake_route_runner),
        ),
    )
    advertisement = publisher.resolve()

    # The transport says wifi...
    assert advertisement.transport == "wifi"
    # ...and the address is the wifi interface's, not the wired one.
    assert advertisement.ip == "10.0.0.5"
    assert advertisement.ip != "192.168.1.42"


def test_resolve_transport_returns_the_interface(monkeypatch):
    """`resolve_transport` reports the interface alongside the transport."""
    monkeypatch.delenv("AQUABUTLER_TRANSPORT", raising=False)

    import aquabutler_mdns.identity as identity_module
    from aquabutler_mdns.transport import query_active_transport

    monkeypatch.setattr(
        identity_module,
        "current_transport_reading",
        lambda *a, **k: query_active_transport(
            runner=fake_runner(stdout="1.1.1.1 dev eth0 src 192.168.1.42 uid 0")
        ),
        raising=False,
    )

    txt, transport, interface = resolve_transport({})
    assert txt == "eth"
    assert transport is Transport.ETH
    assert interface == "eth0"


def test_resolve_transport_override_has_no_interface(monkeypatch):
    """The operator override names a transport, not an interface.

    So the interface is empty — and the address lookup falls back to
    any usable address rather than guessing an interface name from a
    transport class.
    """
    monkeypatch.setenv("AQUABUTLER_TRANSPORT", "wifi")
    txt, transport, interface = resolve_transport({})
    assert txt == "wifi"
    assert transport is Transport.WIFI
    assert interface == ""


def test_advertisement_txt_records_stay_a_subset_of_the_expected_keys():
    """The interface never leaks into the TXT payload itself."""
    advertisement = Advertisement(
        unit_id="aqb0007abcd",
        customer_id="acme-pool-01",
        sw_version="1.2.3",
        transport="eth",
        ip="192.168.1.42",
    )
    assert advertisement.txt_keys() == {
        "unit_id",
        "customer_id",
        "sw_version",
        "transport",
        "ip",
    }
