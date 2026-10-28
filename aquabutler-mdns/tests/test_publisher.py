"""Tests for the publisher daemon.

The publisher is the piece that keeps the advertisement honest as the
device's address and transport change. These tests drive it with
injected side effects — no network, no avahi daemon, no device.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aquabutler_mdns.identity import Advertisement
from aquabutler_mdns.publisher import Publisher, PublisherConfig


class Recorder:
    """Captures the side effects the publisher performs."""

    def __init__(self):
        self.written: list[tuple[Path, str]] = []
        self.reloads = 0
        self.host_names: list[str] = []

    def write(self, path: Path, content: str) -> None:
        self.written.append((path, content))

    def reload(self) -> bool:
        self.reloads += 1
        return True

    def set_host_name(self, host_name: str) -> bool:
        self.host_names.append(host_name)
        return True


def make_publisher(payloads, recorder=None):
    """Build a publisher whose payload resolution walks `payloads`."""
    recorder = recorder or Recorder()
    queue = list(payloads)

    def resolve():
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    publisher = Publisher(
        config=PublisherConfig(service_file=Path("/tmp/aquabutler.service")),
        resolve_payload=resolve,
        write_file=recorder.write,
        reload_avahi=recorder.reload,
        set_host_name=recorder.set_host_name,
    )
    return publisher, recorder


def test_first_publish_writes_and_reloads():
    publisher, recorder = make_publisher(
        [Advertisement(unit_id="aqb0007abcd", ip="192.168.1.42", transport="eth")]
    )
    assert publisher.publish_once() is True
    assert len(recorder.written) == 1
    assert recorder.reloads == 1
    path, content = recorder.written[0]
    assert path == Path("/tmp/aquabutler.service")
    assert "_aquabutler._tcp" in content
    assert "ip=192.168.1.42" in content


def test_unchanged_payload_does_not_reload():
    """A steady-state unit must not churn the responder."""
    payload = Advertisement(unit_id="aqb0007abcd", ip="192.168.1.42", transport="eth")
    publisher, recorder = make_publisher([payload])
    assert publisher.publish_once() is True
    assert publisher.publish_once() is False
    assert recorder.reloads == 1
    assert len(recorder.written) == 1


def test_a_moved_address_triggers_a_rewrite_and_reload():
    """A DHCP lease move must be advertised."""
    publisher, recorder = make_publisher(
        [
            Advertisement(unit_id="aqb0007abcd", ip="192.168.1.42", transport="eth"),
            Advertisement(unit_id="aqb0007abcd", ip="192.168.1.99", transport="eth"),
        ]
    )
    publisher.publish_once()
    assert publisher.publish_once() is True
    assert recorder.reloads == 2
    assert "ip=192.168.1.99" in recorder.written[-1][1]


def test_a_transport_switch_triggers_a_rewrite_and_reload():
    """Switching Ethernet → WiFi must be advertised."""
    publisher, recorder = make_publisher(
        [
            Advertisement(unit_id="aqb0007abcd", ip="192.168.1.42", transport="eth"),
            Advertisement(unit_id="aqb0007abcd", ip="10.0.0.5", transport="wifi"),
        ]
    )
    publisher.publish_once()
    assert publisher.publish_once() is True
    assert recorder.reloads == 2
    assert "transport=wifi" in recorder.written[-1][1]
    assert "ip=10.0.0.5" in recorder.written[-1][1]


def test_host_name_is_the_short_unit_id():
    publisher, _ = make_publisher(
        [Advertisement(unit_id="cb6f09d18ab4c08556f54a5bd7cfd353d4907c4a61998ba8a54cd9f2abc5ee")]
    )
    advertisement = publisher.resolve()
    assert publisher.host_name(advertisement) == "cb6f09d18ab4.local"


def test_host_name_falls_back_when_unprovisioned():
    publisher, _ = make_publisher([Advertisement(unit_id="")])
    advertisement = publisher.resolve()
    assert publisher.host_name(advertisement) == "aquabutler.local"


def test_the_daemon_is_told_to_own_the_unit_host_name():
    """The daemon must claim `<unit_id>.local` or the SRV record cannot resolve.

    Without this, `avahi-browse` finds the service but
    `avahi-resolve -n <unit_id>.local` times out — the advertisement
    is browsable but useless.
    """
    publisher, recorder = make_publisher([Advertisement(unit_id="cb6f09d18ab4")])
    publisher.publish_once()
    assert recorder.host_names == ["cb6f09d18ab4.local"]


def test_the_host_name_is_only_set_once():
    """The daemon keeps the name across service-file reloads."""
    publisher, recorder = make_publisher(
        [
            Advertisement(unit_id="cb6f09d18ab4", ip="192.168.1.42"),
            Advertisement(unit_id="cb6f09d18ab4", ip="192.168.1.99"),
        ]
    )
    publisher.publish_once()
    publisher.publish_once()
    assert recorder.host_names == ["cb6f09d18ab4.local"]


def test_a_failed_host_name_set_is_reported_not_fatal():
    """The advertisement still publishes even if the name cannot be claimed."""
    publisher = Publisher(
        config=PublisherConfig(service_file=Path("/tmp/aquabutler.service")),
        resolve_payload=lambda: Advertisement(unit_id="cb6f09d18ab4"),
        write_file=lambda path, content: None,
        reload_avahi=lambda: True,
        set_host_name=lambda name: False,
    )
    assert publisher.publish_once() is True


def test_reload_failure_is_reported_but_the_file_is_written():
    """A failed avahi reload must not lose the rendered payload."""
    recorder = Recorder()
    publisher = Publisher(
        config=PublisherConfig(service_file=Path("/tmp/aquabutler.service")),
        resolve_payload=lambda: Advertisement(unit_id="aqb0007abcd", ip="1.2.3.4"),
        write_file=recorder.write,
        reload_avahi=lambda: False,
    )
    assert publisher.publish_once() is True
    assert len(recorder.written) == 1
    assert recorder.reloads == 0


def test_the_written_file_parses_back():
    """The file the publisher writes must be a valid avahi service file."""
    from aquabutler_mdns.avahi_service import parse_service_file

    publisher, recorder = make_publisher(
        [
            Advertisement(
                unit_id="aqb0007abcd",
                customer_id="acme-pool-01",
                sw_version="0.1.0",
                transport="eth",
                ip="192.168.1.42",
            )
        ]
    )
    publisher.publish_once()
    parsed = parse_service_file(recorder.written[0][1])
    assert parsed.service_type == "_aquabutler._tcp"
    assert parsed.port == 5150
    assert parsed.host_name == "aqb0007abcd.local"
    assert parsed.txt_keys == {
        "unit_id",
        "customer_id",
        "sw_version",
        "transport",
        "ip",
    }


def test_run_forever_stops_when_told():
    publisher, recorder = make_publisher([Advertisement(unit_id="aqb0007abcd")])
    publisher.config = PublisherConfig(
        service_file=Path("/tmp/aquabutler.service"), poll_interval_sec=0.0
    )
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 2

    publisher.run_forever(stop=stop)
    assert calls["n"] > 2


# ---------------------------------------------------------------------
# The route query is rate-limited across publish cycles
# ---------------------------------------------------------------------


def test_the_publisher_holds_one_transport_source_across_publishes(monkeypatch):
    """The route query must not run once per publish cycle.

    The publisher holds a single `TransportSource` for the life of the
    process; that is what makes the rate limit effective. A fresh
    source per cycle would defeat the cache and put the container back
    to one `fork`/`exec` per poll — the thing the sibling card
    `t_1ff3d0bb` found and fixed on the display side.
    """
    monkeypatch.delenv("AQUABUTLER_TRANSPORT", raising=False)

    import aquabutler_mdns.netinfo as netinfo_module
    from aquabutler_mdns.transport import Transport, TransportReading, TransportSource

    monkeypatch.setattr(
        netinfo_module,
        "list_interface_addresses",
        lambda runner=None, timeout=2.0: [],
    )

    queries = {"n": 0}

    def query():
        queries["n"] += 1
        return TransportReading(
            transport=Transport.ETH, source="route", interface="eth0"
        )

    publisher = Publisher(
        config=PublisherConfig(service_file=Path("/tmp/aquabutler.service")),
        env={},
        transport_source=TransportSource(query=query),
    )

    for _ in range(20):
        publisher.resolve()

    assert queries["n"] == 1, "the route query ran once per publish cycle"


def test_the_refresh_interval_reaches_the_transport_source():
    """`PublisherConfig.transport_refresh_interval_sec` is honoured.

    The config value is what the publisher passes to the source *it*
    builds. An explicitly injected source keeps its own interval —
    injection is a test seam, and it should win.
    """
    import aquabutler_mdns.netinfo as netinfo_module
    from aquabutler_mdns.transport import Transport, TransportReading, TransportSource

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.delenv("AQUABUTLER_TRANSPORT", raising=False)
    monkeypatch.setattr(
        netinfo_module,
        "list_interface_addresses",
        lambda runner=None, timeout=2.0: [],
    )

    class FakeClock:
        def __init__(self):
            self.now = 0.0

        def __call__(self):
            return self.now

    clock = FakeClock()
    queries = {"n": 0}

    def query():
        queries["n"] += 1
        return TransportReading(
            transport=Transport.ETH, source="route", interface="eth0"
        )

    # The publisher builds its own source from the config — this is
    # the production path (the entrypoint passes the interval on the
    # command line, which lands in `PublisherConfig`).
    publisher = Publisher(
        config=PublisherConfig(
            service_file=Path("/tmp/aquabutler.service"),
            transport_refresh_interval_sec=30.0,
        ),
        env={},
    )
    # Swap in the query and clock the publisher will use.
    publisher._transport_source._query = query
    publisher._transport_source._clock = clock

    publisher.resolve()
    clock.now += 10.0
    publisher.resolve()
    assert queries["n"] == 1, "the query ran before the refresh interval elapsed"

    clock.now += 25.0
    publisher.resolve()
    assert queries["n"] == 2
    monkeypatch.undo()
