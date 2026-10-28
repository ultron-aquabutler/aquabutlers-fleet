"""Tests for the current residential-LAN address resolution.

The `ip` TXT record carries the device's **current** address. Under
DHCP that address moves — the advertisement must follow it.
"""
from __future__ import annotations

import pytest

from aquabutler_mdns.netinfo import (
    current_lan_ip,
    list_interface_addresses,
    parse_ip_addr_output,
)


SAMPLE_IP_ADDR = """
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN group default qlen 1000
    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
    inet 127.0.0.1/8 scope host lo
       valid_lft forever preferred_lft forever
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc pfifo_fast state UP group default qlen 1000
    link/ether dc:a6:32:00:00:00 brd ff:ff:ff:ff:ff:ff
    inet 192.168.1.42/24 brd 192.168.1.255 scope global eth0
       valid_lft forever preferred_lft forever
3: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc pfifo_fast state UP group default qlen 1000
    link/ether dc:a6:32:00:00:01 brd ff:ff:ff:ff:ff:ff
    inet 10.0.0.5/24 brd 10.0.0.255 scope global wlan0
       valid_lft forever preferred_lft forever
    inet6 fe80::dc:a6:32:ff:fe00:1/64 scope link
       valid_lft forever preferred_lft forever
"""


def test_parse_ip_addr_output_finds_global_ipv4():
    addresses = parse_ip_addr_output(SAMPLE_IP_ADDR)
    by_iface = {a.iface: a.address for a in addresses}
    assert by_iface["eth0"] == "192.168.1.42"
    assert by_iface["wlan0"] == "10.0.0.5"


def test_parse_ip_addr_output_skips_loopback():
    addresses = parse_ip_addr_output(SAMPLE_IP_ADDR)
    assert all(a.address != "127.0.0.1" for a in addresses)
    assert all(a.iface != "lo" for a in addresses)


def test_parse_ip_addr_output_skips_link_local():
    output = """
2: eth0: <UP> mtu 1500
    inet 169.254.10.20/16 brd 169.254.255.255 scope global eth0
"""
    addresses = parse_ip_addr_output(output)
    assert addresses == []


def test_parse_ip_addr_output_empty():
    assert parse_ip_addr_output("") == []
    assert parse_ip_addr_output("garbage") == []


def test_current_lan_ip_prefers_the_active_interface():
    """The active interface's address is the one advertised."""
    addresses = parse_ip_addr_output(SAMPLE_IP_ADDR)
    assert current_lan_ip(
        active_iface="wlan0", runner=lambda cmd, **kw: _FakeProc(SAMPLE_IP_ADDR)
    ) == "10.0.0.5"
    assert addresses  # sanity: the parser found both


def test_current_lan_ip_falls_back_when_active_interface_has_no_address():
    """A just-up interface with no lease must not blank the record."""
    result = current_lan_ip(
        active_iface="eth1", runner=lambda cmd, **kw: _FakeProc(SAMPLE_IP_ADDR)
    )
    assert result in {"192.168.1.42", "10.0.0.5"}
    assert result != ""


def test_current_lan_ip_prefers_wired_when_no_active_interface():
    result = current_lan_ip(
        active_iface=None, runner=lambda cmd, **kw: _FakeProc(SAMPLE_IP_ADDR)
    )
    assert result == "192.168.1.42"


def test_current_lan_ip_is_empty_when_there_is_no_address():
    result = current_lan_ip(runner=lambda cmd, **kw: _FakeProc(""))
    assert result == ""


def test_list_interface_addresses_handles_a_missing_binary():
    def runner(cmd, **kwargs):
        raise FileNotFoundError("ip")

    assert list_interface_addresses(runner=runner) == []


def test_list_interface_addresses_handles_a_non_zero_exit():
    class P:
        returncode = 1
        stdout = ""
        stderr = "boom"

    assert list_interface_addresses(runner=lambda cmd, **kw: P()) == []


class _FakeProc:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


@pytest.mark.parametrize(
    "output,expected_count",
    [
        (SAMPLE_IP_ADDR, 2),
        ("", 0),
    ],
)
def test_list_interface_addresses_count(output, expected_count):
    assert len(list_interface_addresses(runner=lambda cmd, **kw: _FakeProc(output))) == expected_count
