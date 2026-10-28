"""Tests for the shared transport source.

These mirror the rotary display's `tests/test_transport.py` — the two
containers must compute the same answer from the same evidence, so
the two test suites must assert the same behaviour.

The parity test at the bottom compares this implementation against the
rotary display's copy when both are present on the same machine.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from aquabutler_mdns.transport import (
    MDNS_TRANSPORT_VALUES,
    ROUTE_PROBE_TARGET,
    Transport,
    current_transport,
    mdns_transport_value,
    parse_route_output,
    query_active_transport,
    transport_env_override,
    transport_for_interface,
)


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def fake_runner(returncode=0, stdout="", stderr=""):
    calls: list = []

    def runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return FakeProc(returncode, stdout, stderr)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


# ---------------------------------------------------------------------
# Interface mapping
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "iface,expected",
    [
        ("eth0", Transport.ETH),
        ("eth1", Transport.ETH),
        ("enp1s0", Transport.ETH),
        ("enx001122334455", Transport.ETH),
        ("enp0s31f6", Transport.ETH),
        ("wlan0", Transport.WIFI),
        ("wlan1", Transport.WIFI),
        ("wlp3s0", Transport.WIFI),
        ("wwan0", Transport.LTE),
        ("usb0", Transport.LTE),
    ],
)
def test_transport_for_interface(iface, expected):
    assert transport_for_interface(iface) is expected


@pytest.mark.parametrize("iface", ["lo", "docker0", "br-abc123", "tun0", None, ""])
def test_unknown_interfaces_are_not_mapped(iface):
    assert transport_for_interface(iface) is None


# ---------------------------------------------------------------------
# Route output parsing
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "output,expected",
    [
        (
            "1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.42 uid 0",
            Transport.ETH,
        ),
        ("1.1.1.1 dev wlan0 src 10.0.0.5 uid 0", Transport.WIFI),
        ("1.1.1.1 dev wlan1 src 10.0.0.6 uid 0", Transport.WIFI),
        ("1.1.1.1 dev eth0 src 192.168.1.42 uid 0\n cache", Transport.ETH),
    ],
)
def test_parse_route_output(output, expected):
    assert parse_route_output(output) is expected


@pytest.mark.parametrize(
    "output",
    [
        "",
        "RTNETLINK answers: Network is unreachable",
        "1.1.1.1 dev br-nonexistent src 1.2.3.4 uid 0",
        "no dev token here",
    ],
)
def test_parse_route_output_unrecognised_is_unknown(output):
    assert parse_route_output(output) is Transport.UNKNOWN


# ---------------------------------------------------------------------
# The route query
# ---------------------------------------------------------------------


def test_query_active_transport_on_eth():
    runner = fake_runner(stdout="1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.42 uid 0")
    reading = query_active_transport(runner=runner)
    assert reading.transport is Transport.ETH
    assert reading.source == "route"
    assert reading.is_known
    assert runner.calls[0][0] == ["ip", "route", "get", ROUTE_PROBE_TARGET]


def test_query_active_transport_on_wifi():
    runner = fake_runner(stdout="1.1.1.1 dev wlan0 src 10.0.0.5 uid 0")
    assert query_active_transport(runner=runner).transport is Transport.WIFI


def test_query_active_transport_no_route_is_unknown():
    runner = fake_runner(returncode=2, stderr="RTNETLINK answers: Network is unreachable")
    reading = query_active_transport(runner=runner)
    assert reading.transport is Transport.UNKNOWN
    assert not reading.is_known


def test_query_active_transport_missing_binary_is_unknown():
    def runner(cmd, **kwargs):
        raise FileNotFoundError("ip")

    assert query_active_transport(runner=runner).transport is Transport.UNKNOWN


def test_query_active_transport_timeout_is_unknown():
    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 2.0)

    assert query_active_transport(runner=runner).transport is Transport.UNKNOWN


def test_current_transport_wrapper():
    runner = fake_runner(stdout="1.1.1.1 dev eth0 src 192.168.1.42 uid 0")
    assert current_transport(runner=runner) is Transport.ETH


# ---------------------------------------------------------------------
# The mDNS TXT value form
# ---------------------------------------------------------------------


def test_mdns_values_are_lower_case():
    """The policy names the TXT values `eth` / `wifi`."""
    assert MDNS_TRANSPORT_VALUES[Transport.ETH] == "eth"
    assert MDNS_TRANSPORT_VALUES[Transport.WIFI] == "wifi"
    assert MDNS_TRANSPORT_VALUES[Transport.LTE] == "lte"


@pytest.mark.parametrize(
    "transport,expected",
    [
        (Transport.ETH, "eth"),
        (Transport.WIFI, "wifi"),
        (Transport.LTE, "lte"),
        ("ETH", "eth"),
        ("WIFI", "wifi"),
        ("LTE", "lte"),
    ],
)
def test_mdns_transport_value(transport, expected):
    assert mdns_transport_value(transport) == expected


def test_mdns_transport_value_unknown_is_empty():
    """An unknown transport must NOT be advertised — omit the key."""
    assert mdns_transport_value(Transport.UNKNOWN) == ""
    assert mdns_transport_value(None) == ""
    assert mdns_transport_value("satellite") == ""
    assert mdns_transport_value("") == ""


def test_mdns_transport_value_accepts_interface_names():
    assert mdns_transport_value("eth0") == "eth"
    assert mdns_transport_value("wlan0") == "wifi"


# ---------------------------------------------------------------------
# The operator override
# ---------------------------------------------------------------------


def test_env_override(monkeypatch):
    monkeypatch.setenv("AQUABUTLER_TRANSPORT", "eth")
    assert transport_env_override() is Transport.ETH
    monkeypatch.setenv("AQUABUTLER_TRANSPORT", "wifi")
    assert transport_env_override() is Transport.WIFI


def test_env_override_absent(monkeypatch):
    monkeypatch.delenv("AQUABUTLER_TRANSPORT", raising=False)
    assert transport_env_override() is None


def test_env_override_invalid_is_ignored(monkeypatch):
    monkeypatch.setenv("AQUABUTLER_TRANSPORT", "satellite")
    assert transport_env_override() is None


# ---------------------------------------------------------------------
# Parity with the rotary display's copy
# ---------------------------------------------------------------------


def _rotary_display_transport_module():
    """Locate the rotary display's transport module, if it is on this box.

    The rotary display lives in `ultron-aquabutler/AquaButler-Server`
    under `src/rotary_display/rotary_display/net/transport.py`. The two
    copies must not diverge — a technician comparing the display's
    `LINK:` line against the mDNS `transport` record must never see a
    contradiction.

    Returns the loaded `transport` module, or None when the rotary
    display is not checked out on this machine.
    """
    return _load_rotary_module("transport.py")


def _rotary_display_contract_module():
    """Locate the rotary display's shared-contract module.

    `mdns_transport_value` — the mapping from a transport to the mDNS
    TXT value — lives in the contract module, not the transport module.
    """
    return _load_rotary_module("contract.py")


def _load_rotary_module(filename: str):
    """Load one module from the rotary display's `net` package.

    The rotary display's `net` package uses relative imports
    (`from .transport import ...`), so the whole package is loaded
    under a synthetic package name rather than the file in isolation.
    """
    import importlib.util
    from pathlib import Path

    roots = [
        Path("/home/serveradmin/AquaButler-Server/src/rotary_display/rotary_display"),
        Path(
            "/home/serveradmin/.hermes/kanban/boards/homelab/workspaces"
            "/t_1ff3d0bb/wt/src/rotary_display/rotary_display"
        ),
    ]
    for root in roots:
        net_dir = root / "net"
        if not net_dir.is_dir():
            continue
        pkg_name = "rotary_display_parity_net"
        pkg_spec = importlib.util.spec_from_file_location(
            pkg_name, net_dir / "__init__.py", submodule_search_locations=[str(net_dir)]
        )
        if pkg_spec is None or pkg_spec.loader is None:
            continue
        pkg = importlib.util.module_from_spec(pkg_spec)
        sys.modules[pkg_name] = pkg
        try:
            pkg_spec.loader.exec_module(pkg)
            return importlib.import_module(f"{pkg_name}.{filename[:-3]}")
        except Exception:
            sys.modules.pop(pkg_name, None)
            raise
    return None


def test_transport_implementation_matches_the_rotary_display():
    """Both containers must resolve the same transport from the same input.

    Skipped when the rotary display's copy is not present on this
    machine — it lives in a different repo and is not always checked
    out. The CI job for the fleet image has it.
    """
    module = _rotary_display_transport_module()
    if module is None:
        pytest.skip("rotary display transport module not present on this machine")

    outputs = [
        "1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.42 uid 0",
        "1.1.1.1 dev wlan0 src 10.0.0.5 uid 0",
        "1.1.1.1 dev wlan1 src 10.0.0.6 uid 0",
        "1.1.1.1 dev wwan0 src 10.1.2.3 uid 0",
        "1.1.1.1 dev usb0 src 10.1.2.4 uid 0",
        "1.1.1.1 dev enp1s0 src 10.1.2.5 uid 0",
        "1.1.1.1 dev br-nonexistent src 10.1.2.6 uid 0",
        "1.1.1.1 dev lo src 127.0.0.1 uid 0",
        "",
        "garbage",
    ]
    for output in outputs:
        ours = parse_route_output(output)
        theirs = module.parse_route_output(output)
        # The two modules define separate `Transport` enum classes, so
        # identity comparison would always fail. Compare the values.
        assert ours.value == theirs.value, (
            f"transport parsing diverged for {output!r}: "
            f"{ours.value!r} != {theirs.value!r}"
        )

    for iface in ["eth0", "eth1", "enp1s0", "enx001122334455", "wlan0", "wlp3s0",
                  "wwan0", "usb0", "lo", "docker0", "br-abc"]:
        ours = transport_for_interface(iface)
        theirs = module.transport_for_interface(iface)
        assert (ours is None) == (theirs is None), f"mapping diverged for {iface!r}"
        if ours is not None:
            assert ours.value == theirs.value, f"mapping diverged for {iface!r}"

    for value in ["ETH", "eth", "wifi", "WIFI", "lte", "LTE", "ethernet",
                  "wireless", "satellite", "", None]:
        # `mdns_transport_value` lives in the rotary display's shared
        # contract module, not its transport module.
        contract = _rotary_display_contract_module()
        if contract is None or not hasattr(contract, "mdns_transport_value"):
            pytest.skip("rotary display contract module not present on this machine")
        assert mdns_transport_value(value) == contract.mdns_transport_value(value), (
            f"mdns value mapping diverged for {value!r}"
        )
