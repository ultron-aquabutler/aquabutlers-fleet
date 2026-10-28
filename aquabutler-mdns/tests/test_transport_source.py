"""Tests for the rate-limited transport source.

The sibling card `t_1ff3d0bb` (rotary display active-transport line)
found that the rotary display was running `ip route get 1.1.1.1` on
*every frame* — 30 `fork`/`exec` per second, sustained. It rate-
limited the query to one per `refresh_interval_sec` (2 s default).

This container has the same obligation: the route query is a
subprocess spawn, and it must not run per poll cycle or faster. The
semantics here mirror the rotary display's `TransportSource` so the
two containers resolve the transport on the same cadence and cannot
disagree about which one is stale.

Three behaviours are pinned below, each of which the rotary display
also pins:

1. the route query is rate-limited (at most one per interval);
2. a failed query is never carried forward from a previous success;
3. the MQTT input path is NOT rate-limited — an inbound link
   message applies immediately.
"""
from __future__ import annotations

import pytest

from aquabutler_mdns.transport import (
    DEFAULT_REFRESH_INTERVAL_SEC,
    STATUS_LINK_LEAF,
    Transport,
    TransportReading,
    TransportSource,
    UNKNOWN_READING,
)


def _reading(transport=Transport.ETH, interface="eth0", source="route"):
    return TransportReading(transport=transport, source=source, interface=interface)


class FakeClock:
    """A monotonic clock the test drives by hand."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingQuery:
    """A route query that records how many times it ran."""

    def __init__(self, reading=None):
        self.reading = reading if reading is not None else _reading()
        self.calls = 0

    def __call__(self) -> TransportReading:
        self.calls += 1
        return self.reading


# ---------------------------------------------------------------------
# The rate limit
# ---------------------------------------------------------------------


def test_route_query_runs_once_when_state_is_read_repeatedly():
    """Reading the state repeatedly must not re-run the query."""
    query = CountingQuery()
    source = TransportSource(query=query, clock=FakeClock())

    for _ in range(50):
        assert source.state.transport is Transport.ETH

    assert query.calls == 1


def test_route_query_reruns_after_the_refresh_interval():
    query = CountingQuery()
    clock = FakeClock()
    source = TransportSource(query=query, clock=clock)

    source.state
    assert query.calls == 1

    # Just inside the interval — still cached.
    clock.advance(DEFAULT_REFRESH_INTERVAL_SEC - 0.1)
    source.state
    assert query.calls == 1

    # Past the interval — refreshed.
    clock.advance(0.2)
    source.state
    assert query.calls == 2


def test_refresh_interval_is_configurable():
    query = CountingQuery()
    clock = FakeClock()
    source = TransportSource(query=query, clock=clock, refresh_interval_sec=30.0)

    source.state
    clock.advance(10.0)
    source.state
    assert query.calls == 1

    clock.advance(25.0)
    source.state
    assert query.calls == 2


def test_default_refresh_interval_matches_the_rotary_display():
    """2 s — the same default the rotary display uses."""
    assert DEFAULT_REFRESH_INTERVAL_SEC == 2.0


def test_a_changed_route_is_picked_up_after_the_interval():
    """A transport change is visible once the interval has elapsed."""
    readings = [_reading(Transport.ETH, "eth0"), _reading(Transport.WIFI, "wlan0")]

    class RotatingQuery:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            reading = readings[min(self.calls, len(readings) - 1)]
            self.calls += 1
            return reading

    clock = FakeClock()
    source = TransportSource(query=RotatingQuery(), clock=clock)

    assert source.state.transport is Transport.ETH
    assert source.state.interface == "eth0"

    clock.advance(DEFAULT_REFRESH_INTERVAL_SEC + 0.1)
    assert source.state.transport is Transport.WIFI
    assert source.state.interface == "wlan0"


# ---------------------------------------------------------------------
# A failed query is never carried forward
# ---------------------------------------------------------------------


def test_a_failed_query_is_not_carried_forward():
    """A query that fails yields UNKNOWN — the last success is dropped.

    Carrying a stale reading forward would advertise a transport the
    unit is no longer using, which is exactly the misleading signal
    the whole feature exists to eliminate.
    """
    readings = [_reading(Transport.ETH, "eth0"), UNKNOWN_READING]
    state = {"n": 0}

    def query():
        reading = readings[min(state["n"], len(readings) - 1)]
        state["n"] += 1
        return reading

    clock = FakeClock()
    source = TransportSource(query=query, clock=clock)

    assert source.state.transport is Transport.ETH

    clock.advance(DEFAULT_REFRESH_INTERVAL_SEC + 0.1)
    assert source.state.transport is Transport.UNKNOWN
    assert not source.state.is_known


def test_a_failed_query_does_not_fall_back_to_a_stale_route_reading():
    """The cached route reading is replaced, not shadowed."""
    query = CountingQuery(_reading(Transport.ETH, "eth0"))
    clock = FakeClock()
    source = TransportSource(query=query, clock=clock)

    assert source.state.transport is Transport.ETH

    # The next query fails outright.
    query.reading = UNKNOWN_READING
    clock.advance(DEFAULT_REFRESH_INTERVAL_SEC + 0.1)

    state = source.state
    assert state.transport is Transport.UNKNOWN
    assert state.source == "none"


# ---------------------------------------------------------------------
# The MQTT path is not rate-limited
# ---------------------------------------------------------------------


def test_mqtt_link_applies_immediately():
    """An inbound link message takes effect on the very next read."""
    query = CountingQuery(UNKNOWN_READING)
    clock = FakeClock()
    source = TransportSource(query=query, clock=clock)

    assert source.state.transport is Transport.UNKNOWN

    source.apply_mqtt_link({"value": "wifi"}, now=clock.now)
    state = source.state
    assert state.transport is Transport.WIFI
    assert state.source == "mqtt"


def test_mqtt_link_accepts_the_named_transport_field():
    query = CountingQuery(UNKNOWN_READING)
    clock = FakeClock()
    source = TransportSource(query=query, clock=clock)

    source.apply_mqtt_link({"transport": "eth"}, now=clock.now)
    assert source.state.transport is Transport.ETH


def test_mqtt_link_is_not_rate_limited():
    """Two messages in the same instant both apply — no coalescing."""
    query = CountingQuery(UNKNOWN_READING)
    clock = FakeClock()
    source = TransportSource(query=query, clock=clock)

    source.apply_mqtt_link({"value": "eth"}, now=clock.now)
    assert source.state.transport is Transport.ETH
    # Same timestamp — the interval has not elapsed.
    source.apply_mqtt_link({"value": "wifi"}, now=clock.now)
    assert source.state.transport is Transport.WIFI


def test_a_malformed_mqtt_link_is_unknown_not_ignored():
    """A malformed publisher surfaces as 'unknown', not as a stale value."""
    query = CountingQuery(UNKNOWN_READING)
    clock = FakeClock()
    source = TransportSource(query=query, clock=clock)

    source.apply_mqtt_link({"value": "eth"}, now=clock.now)
    assert source.state.transport is Transport.ETH

    source.apply_mqtt_link({"value": "satellite"}, now=clock.now)
    assert source.state.transport is Transport.UNKNOWN


def test_a_stale_mqtt_value_reverts_to_unknown():
    """A stale transport label is as misleading as a stale address."""
    query = CountingQuery(UNKNOWN_READING)
    clock = FakeClock()
    source = TransportSource(query=query, clock=clock, stale_after_sec=60.0)

    source.apply_mqtt_link({"value": "wifi"}, now=clock.now)
    assert source.state.transport is Transport.WIFI

    clock.advance(120.0)
    assert source.state.transport is Transport.UNKNOWN


# ---------------------------------------------------------------------
# The operator override
# ---------------------------------------------------------------------


def test_the_env_override_wins_over_the_route(monkeypatch):
    monkeypatch.setenv("AQUABUTLER_TRANSPORT", "lte")
    query = CountingQuery(_reading(Transport.ETH, "eth0"))
    source = TransportSource(query=query, clock=FakeClock())

    state = source.state
    assert state.transport is Transport.LTE
    assert state.source == "env"
    # The override names a transport, not an interface.
    assert state.interface == ""


def test_the_env_override_does_not_run_the_route_query(monkeypatch):
    monkeypatch.setenv("AQUABUTLER_TRANSPORT", "eth")
    query = CountingQuery()
    source = TransportSource(query=query, clock=FakeClock())

    source.state
    assert query.calls == 0


# ---------------------------------------------------------------------
# The topic leaf
# ---------------------------------------------------------------------


def test_the_status_link_leaf_is_the_existing_status_family():
    """`status/link` is a leaf inside the existing `status` family.

    The MQTT topic schema is locked: `pool/<customer_id>/#` must not
    gain, lose, or re-key a topic. The display already subscribes to
    `pool/+/status/#`, so `status/link` needs no new subscription.
    """
    assert STATUS_LINK_LEAF == "link"


# ---------------------------------------------------------------------
# Parity with the rotary display's TransportSource
# ---------------------------------------------------------------------


def _load_rotary_transport_source():
    """Load the rotary display's `transport_source` module, if present."""
    import importlib.util
    import sys
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
        pkg_name = "rotary_display_parity_src_net"
        pkg_spec = importlib.util.spec_from_file_location(
            pkg_name, net_dir / "__init__.py", submodule_search_locations=[str(net_dir)]
        )
        if pkg_spec is None or pkg_spec.loader is None:
            continue
        pkg = importlib.util.module_from_spec(pkg_spec)
        sys.modules[pkg_name] = pkg
        try:
            pkg_spec.loader.exec_module(pkg)
            return importlib.import_module(f"{pkg_name}.transport_source")
        except Exception:
            sys.modules.pop(pkg_name, None)
            raise
    return None


def test_transport_source_matches_the_rotary_display():
    """Both containers must resolve the transport the same way.

    Skipped when the rotary display is not checked out on this
    machine — it lives in a different repo.
    """
    module = _load_rotary_transport_source()
    if module is None:
        pytest.skip("rotary display transport_source not present on this machine")

    # The rate-limit default must agree, or the two containers would
    # disagree about which reading is stale.
    assert DEFAULT_REFRESH_INTERVAL_SEC == module.DEFAULT_REFRESH_INTERVAL_SEC
    assert STATUS_LINK_LEAF == module.STATUS_LINK_LEAF

    # The same call sequence must produce the same answers.
    def run(source_cls):
        readings = [
            _reading(Transport.ETH, "eth0"),
            _reading(Transport.WIFI, "wlan0"),
        ]
        state = {"n": 0}

        def query():
            reading = readings[min(state["n"], len(readings) - 1)]
            state["n"] += 1
            return reading

        clock = FakeClock()
        source = source_cls(query=query, clock=clock)
        out = []
        out.append(source.state.transport.value)
        out.append(source.state.transport.value)
        clock.advance(DEFAULT_REFRESH_INTERVAL_SEC + 0.1)
        out.append(source.state.transport.value)
        return out

    ours = run(TransportSource)
    theirs = run(module.TransportSource)
    assert ours == theirs, f"transport resolution diverged: {ours!r} != {theirs!r}"
