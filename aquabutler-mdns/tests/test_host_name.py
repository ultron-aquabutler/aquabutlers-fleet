"""The advertised host name must be the one the daemon actually owns.

`avahi-set-host-name` is a liar.

It exits 0 when it succeeds — and it also exits 0 when the name it
was asked for is **already taken** on the segment. In that case the
daemon quietly falls back to a suffixed variant:

    Host name conflict, retrying with aqb0007abcd-2
    Host name conflict, retrying with aqb0007abcd-3
    ...

and carries on as if nothing happened.

The failure this produces is subtle and expensive. The publisher
writes the service file with `<host-name>aqb0007abcd.local</host-name>`,
the daemon owns `aqb0007abcd-9.local`, and the unit looks healthy in
every log line. But the technician's

    avahi-resolve -n aqb0007abcd.local

times out — because no host on the segment answers for that name.
The unit is browsable and useless, which is the worst combination:
the technician sees the device listed and cannot reach it.

So the publisher asks the daemon (over D-Bus — `avahi-daemon --check`
reports liveness and nothing else) which name it actually holds, and
advertises *that*. The technician then resolves a name that exists,
and the log says plainly that another device on the LAN is squatting
on the unit's name.

These tests were written after reproducing the failure against a real
container: two avahi daemons on one segment, the second one's name
forced into a `-12` suffix while its service file still said
`aqb0007abcd.local`.
"""
from __future__ import annotations

import pytest

from aquabutler_mdns.avahi_service import parse_service_file
from aquabutler_mdns.identity import Advertisement
from aquabutler_mdns.publisher import Publisher, PublisherConfig


def make_publisher(tmp_path, current_host_name, set_host_name=None):
    """A publisher whose side effects are all injected."""
    written: list[tuple[str, str]] = []

    def write_file(path, content):
        written.append((str(path), content))

    return (
        Publisher(
            config=PublisherConfig(service_file=tmp_path / "aquabutler.service"),
            resolve_payload=lambda: Advertisement(
                unit_id="aqb0007abcd",
                customer_id="acme-pool-01",
                sw_version="1.2.3",
                transport="eth",
                ip="192.168.1.42",
            ),
            write_file=write_file,
            reload_avahi=lambda: True,
            set_host_name=set_host_name or (lambda name: True),
            current_host_name=current_host_name,
        ),
        written,
    )


# ---------------------------------------------------------------------
# The happy path — the daemon owns the requested name
# ---------------------------------------------------------------------


def test_the_requested_name_is_advertised_when_the_daemon_owns_it(tmp_path):
    publisher, written = make_publisher(
        tmp_path, current_host_name=lambda: "aqb0007abcd.local"
    )
    publisher.publish_once()

    assert len(written) == 1
    parsed = parse_service_file(written[0][1])
    assert parsed.host_name == "aqb0007abcd.local"


# ---------------------------------------------------------------------
# The conflict — the daemon took a different name
# ---------------------------------------------------------------------


def test_the_name_the_daemon_actually_owns_is_advertised(tmp_path):
    """A suffixed name is advertised when the requested one was taken.

    The SRV record then points at a name that genuinely resolves,
    which is strictly better than pointing at one that does not.
    """
    publisher, written = make_publisher(
        tmp_path, current_host_name=lambda: "aqb0007abcd-9.local"
    )
    publisher.publish_once()

    parsed = parse_service_file(written[0][1])
    assert parsed.host_name == "aqb0007abcd-9.local"
    assert parsed.host_name != "aqb0007abcd.local"


def test_the_conflict_is_logged_as_a_warning(tmp_path, caplog):
    """A name collision is an operational problem, not a detail.

    The unit is reachable, but under a different name than policy
    says, and another device on the LAN is holding the unit's name.
    The operator has to be told.
    """
    publisher, _ = make_publisher(
        tmp_path, current_host_name=lambda: "aqb0007abcd-9.local"
    )
    with caplog.at_level("WARNING"):
        publisher.publish_once()

    assert any("already taken" in r.message for r in caplog.records)


def test_the_service_name_still_reflects_the_unit_id(tmp_path):
    """The conflict changes the host name, not the unit's identity.

    `unit_id` in the TXT payload is the unit's stable handle; the
    host name is the per-segment name. A conflict on the latter must
    not alter the former.
    """
    publisher, written = make_publisher(
        tmp_path, current_host_name=lambda: "aqb0007abcd-9.local"
    )
    publisher.publish_once()

    parsed = parse_service_file(written[0][1])
    assert parsed.txt["unit_id"] == "aqb0007abcd"
    assert parsed.txt["customer_id"] == "acme-pool-01"


# ---------------------------------------------------------------------
# The daemon cannot be asked
# ---------------------------------------------------------------------


def test_the_requested_name_is_used_when_the_daemon_cannot_be_asked(tmp_path):
    """An unanswerable daemon is not a reason to invent a name.

    The requested name is used — the same behaviour as before the
    check existed — and the uncertainty is logged. Advertising a
    made-up name would be worse than advertising the intended one.
    """
    publisher, written = make_publisher(tmp_path, current_host_name=lambda: "")
    publisher.publish_once()

    parsed = parse_service_file(written[0][1])
    assert parsed.host_name == "aqb0007abcd.local"


def test_an_unanswerable_daemon_is_logged(tmp_path, caplog):
    publisher, _ = make_publisher(tmp_path, current_host_name=lambda: "")
    with caplog.at_level("WARNING"):
        publisher.publish_once()

    assert any("could not confirm" in r.message for r in caplog.records)


# ---------------------------------------------------------------------
# The host name is only claimed once
# ---------------------------------------------------------------------


def test_the_name_is_claimed_once_and_verified_every_cycle(tmp_path):
    """The claim happens once; the verification happens every cycle.

    Re-claiming the name on every poll would fight with the daemon's
    own state and generate pointless D-Bus churn. But the *check*
    must run every cycle: a name can be lost (the daemon restarted,
    the device rebooted into a different role) without the publisher
    noticing.
    """
    claims: list[str] = []
    publisher, _ = make_publisher(
        tmp_path,
        current_host_name=lambda: "aqb0007abcd.local",
        set_host_name=lambda name: (claims.append(name), True)[1],
    )

    publisher.publish_once()
    publisher.publish_once()
    publisher.publish_once()

    assert claims == ["aqb0007abcd.local"]


def test_the_publisher_rechecks_the_name_after_a_restart(tmp_path):
    """A changed daemon host name re-renders the service file."""
    names = iter(["aqb0007abcd.local", "aqb0007abcd.local", "thor.local"])
    publisher, written = make_publisher(
        tmp_path, current_host_name=lambda: next(names)
    )

    publisher.publish_once()
    publisher.publish_once()
    publisher.publish_once()

    # The third cycle saw a different daemon name, so the file was
    # rewritten with the new one.
    assert len(written) == 2
    assert parse_service_file(written[-1][1]).host_name == "thor.local"


# ---------------------------------------------------------------------
# The D-Bus query itself
# ---------------------------------------------------------------------


def test_the_dbus_host_name_query_parses_the_reply(monkeypatch):
    """`dbus-send` prints `string "name.local"` — parse that."""
    import subprocess

    from aquabutler_mdns.publisher import Publisher as P

    class FakeProc:
        returncode = 0
        stdout = (
            "method return time=1 sender=:1.0 -> destination=:1.2 serial=5 reply_serial=2\n"
            '   string "aqb0007abcd-12.local"\n'
        )
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc())
    assert P._default_current_host_name() == "aqb0007abcd-12.local"


def test_the_dbus_host_name_query_handles_a_daemon_failure(monkeypatch):
    import subprocess

    from aquabutler_mdns.publisher import Publisher as P

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "Error org.freedesktop.DBus.Error.ServiceUnknown"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc())
    assert P._default_current_host_name() == ""


def test_the_dbus_host_name_query_handles_a_missing_binary(monkeypatch):
    import subprocess

    from aquabutler_mdns.publisher import Publisher as P

    def boom(*a, **k):
        raise FileNotFoundError("dbus-send")

    monkeypatch.setattr(subprocess, "run", boom)
    assert P._default_current_host_name() == ""
