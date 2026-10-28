"""The `aquabutler-mdns` publisher daemon.

Responsibilities, in order:

1. Resolve the advertisement's payload — identity (`unit_id`,
   `customer_id`, `sw_version`), the active `transport`, and the
   current `ip`.
2. Render `/etc/avahi/services/aquabutler.service` with that payload.
3. Ask `avahi-daemon` to reload its static services (`SIGHUP` /
   `avahi-daemon --reload`) — but only when the payload actually
   changed, so a steady-state unit does not churn the responder.

The loop is deliberately simple: resolve → compare → write → reload →
sleep. Every step is injectable so the unit tests drive it without a
network, an avahi daemon, or a device.

The identity and transport values are resolved by the shared sources
(`identity.py`, `transport.py`) so this container and the rotary
display can never disagree about which transport is active.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .avahi_service import DEFAULT_SERVICE_FILE, render_service_file
from .identity import (
    DEFAULT_CUSTOMER_ID_PATH,
    Advertisement,
    read_customer_id,
    resolve_sw_version,
    resolve_transport,
    resolve_unit_id,
    short_unit_id,
)
from .netinfo import current_lan_ip
from .transport import DEFAULT_REFRESH_INTERVAL_SEC, TransportSource


log = logging.getLogger(__name__)


# How often the payload is re-resolved. The address can move whenever
# the DHCP lease renews and the transport whenever the unit switches
# links, so the poll is quick — but it is a poll, not a watch, because
# neither event is observable from inside the container without extra
# privileges.
DEFAULT_POLL_INTERVAL_SEC = 15.0


@dataclass
class PublisherConfig:
    """Configuration for the publisher."""

    service_file: Path = DEFAULT_SERVICE_FILE
    customer_id_path: str = DEFAULT_CUSTOMER_ID_PATH
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC
    # Avahi's host name for the unit: `<unit_id>.local`.
    domain: str = "local"
    # Minimum interval between two route queries, in seconds. The
    # publisher holds one `TransportSource` for the life of the
    # process, so the route query is rate-limited here rather than
    # running once per poll cycle.
    transport_refresh_interval_sec: float = DEFAULT_REFRESH_INTERVAL_SEC


class Publisher:
    """Resolves the payload and keeps avahi's service file current."""

    def __init__(
        self,
        config: PublisherConfig | None = None,
        env: Mapping[str, str] | None = None,
        resolve_payload: Callable[[], Advertisement] | None = None,
        write_file: Callable[[Path, str], None] | None = None,
        reload_avahi: Callable[[], bool] | None = None,
        set_host_name: Callable[[str], bool] | None = None,
        current_host_name: Callable[[], str] | None = None,
        transport_source: TransportSource | None = None,
    ) -> None:
        self.config = config or PublisherConfig()
        self._env = env
        self._resolve_payload = resolve_payload
        self._write_file = write_file or self._default_write_file
        self._reload_avahi = reload_avahi or self._default_reload_avahi
        self._set_host_name = set_host_name or self._default_set_host_name
        self._current_host_name = current_host_name or self._default_current_host_name
        self._last_rendered: str | None = None
        self._host_name_set: str | None = None
        # The host name the daemon actually owns, as last observed.
        self._last_host_name: str | None = None
        # One source for the life of the process — this is what
        # rate-limits the route query. A fresh source per poll would
        # defeat the cache entirely.
        self._transport_source = transport_source or TransportSource(
            refresh_interval_sec=self.config.transport_refresh_interval_sec
        )

    # -- default side-effect implementations (injectable for tests) --

    @staticmethod
    def _default_write_file(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _default_reload_avahi() -> bool:
        """Ask avahi-daemon to re-read its static service files."""
        try:
            proc = subprocess.run(
                ["avahi-daemon", "--reload"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("avahi reload failed: %s", exc)
            return False
        if proc.returncode != 0:
            log.warning(
                "avahi reload rc=%s: %s", proc.returncode, (proc.stderr or "").strip()
            )
            return False
        return True

    @staticmethod
    def _default_set_host_name(host_name: str) -> bool:
        """Make avahi-daemon own the unit's `<unit_id>.local` name.

        Without this the daemon advertises the service but does not
        publish the host name, so `avahi-resolve -n <unit_id>.local`
        times out and the service's SRV record cannot be resolved —
        the service is browsable but useless. `avahi-set-host-name`
        claims the name for the daemon.

        The name is set once per boot; the daemon keeps it across
        service-file reloads, so this only needs to happen when the
        name changes.

        NOTE: this returns True when the *command* succeeded, not
        when the name was actually claimed. When the name is already
        taken on the segment, avahi silently falls back to a suffixed
        variant (`<unit_id>-2`, `-3`, …) and still exits 0. The caller
        must confirm the claim with `_default_current_host_name`.
        """
        # avahi-set-host-name takes the bare label, not the FQDN.
        label = host_name.split(".", 1)[0]
        if not label:
            return False
        try:
            proc = subprocess.run(
                ["avahi-set-host-name", label],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("avahi-set-host-name failed: %s", exc)
            return False
        if proc.returncode != 0:
            log.warning(
                "avahi-set-host-name(%s) rc=%s: %s",
                label,
                proc.returncode,
                (proc.stderr or "").strip(),
            )
            return False
        return True

    @staticmethod
    def _default_current_host_name() -> str:
        """The host name the avahi daemon *actually* owns.

        `avahi-set-host-name` exits 0 even when the requested name is
        already taken — the daemon quietly falls back to a suffixed
        variant. Trusting the exit code therefore produces a unit
        that advertises `<unit_id>.local` in its service file while
        the daemon owns `<unit_id>-9.local`: browsable, but
        `avahi-resolve -n <unit_id>.local` times out and the
        technician is left chasing a name that does not exist.

        So the daemon is asked what it owns, and that answer — not
        the requested name — is what goes into the service file.

        The daemon is queried over D-Bus, which is the only
        authoritative source: `avahi-daemon --check` reports liveness
        and nothing else. `org.freedesktop.Avahi.Server.GetHostNameFqdn`
        returns the name the daemon actually holds.

        Returns an empty string when the daemon cannot be reached — the
        caller then keeps the requested name and logs the uncertainty
        rather than inventing one.
        """
        try:
            proc = subprocess.run(
                [
                    "dbus-send",
                    "--system",
                    "--dest=org.freedesktop.Avahi",
                    "--type=method_call",
                    "--print-reply",
                    "/",
                    "org.freedesktop.Avahi.Server.GetHostNameFqdn",
                ],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not query the avahi daemon host name: %s", exc)
            return ""
        if proc.returncode != 0:
            log.warning(
                "could not query the avahi daemon host name: %s",
                (proc.stderr or "").strip(),
            )
            return ""
        # dbus-send prints:   string "aqb0007abcd-12.local"
        for line in (proc.stdout or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("string "):
                return stripped[len("string ") :].strip().strip('"')
        return ""

    # -- payload resolution --

    def resolve(self) -> Advertisement:
        """Resolve the current advertisement payload.

        The transport is resolved first, because the address comes
        from the *active* interface — the same one the transport
        names. Resolving them independently could advertise an
        address from an interface the unit is not using.
        """
        if self._resolve_payload is not None:
            return self._resolve_payload()

        env = self._env if self._env is not None else os.environ

        unit_id = resolve_unit_id(env)
        customer_id = read_customer_id(self.config.customer_id_path)
        sw_version = resolve_sw_version(env)
        transport_txt, transport, interface = resolve_transport(
            env, source=self._transport_source
        )

        ip = current_lan_ip(active_iface=interface or None)

        return Advertisement(
            unit_id=unit_id,
            customer_id=customer_id,
            sw_version=sw_version,
            transport=transport_txt,
            ip=ip,
        )

    # -- host name --

    def host_name(self, advertisement: Advertisement) -> str:
        """The unit's mDNS host name: `<unit_id>.local`."""
        short = short_unit_id(advertisement.unit_id)
        if not short:
            # No identity yet (the unit has not been provisioned). Use
            # a fixed placeholder so the advertisement is still
            # discoverable by service type — `avahi-browse -rt
            # _aquabutler._tcp` finds it either way.
            short = "aquabutler"
        return f"{short}.{self.config.domain}"

    # -- the publish step --

    def publish_once(self) -> bool:
        """Resolve, render, write and reload. Returns True on change.

        The host name and the service file have to agree with what
        the daemon actually holds, and the daemon only settles on a
        name *after* a reload. So the order matters:

        1. resolve the payload;
        2. claim the requested host name (once — the daemon keeps it
           across service-file reloads, unless it was contended);
        3. write the service file and reload the daemon;
        4. read back the name the daemon *actually* owns and, if it
           differs from what was written, correct the file.

        Step 4 is not optional. `avahi-daemon --reload` re-registers
        the host name as well as the services, so on a contended
        segment the daemon silently moves to a suffixed variant
        (`<unit_id>-8` → `<unit_id>-11` → …) on every reload. The
        name written into the file before the reload is therefore
        already stale by the time the reload returns. Advertising it
        would point the SRV record at a name the daemon does not own,
        and `avahi-resolve -n <unit_id>.local` would time out.

        The corrected file is written without a second reload: avahi
        reads the `<host-name>` when it loads the service, so the
        name that matters is the one in the file at reload time. The
        correction is verified against the daemon on the next cycle.
        """
        advertisement = self.resolve()
        requested_host_name = self.host_name(advertisement)

        # The daemon must own the unit's host name before the service
        # can be resolved: avahi publishes the SRV record pointing at
        # `<unit_id>.local`, and that name only resolves if the daemon
        # has claimed it.
        if requested_host_name != self._host_name_set:
            if self._set_host_name(requested_host_name):
                self._host_name_set = requested_host_name
            else:
                log.warning(
                    "could not claim the host name %s; the service will be "
                    "browsable but `avahi-resolve -n %s` will time out",
                    requested_host_name,
                    requested_host_name,
                )

        # `avahi-set-host-name` succeeds even when the name is already
        # taken — the daemon then quietly takes a suffixed variant.
        # Ask the daemon what it actually owns and advertise THAT, so
        # the SRV record points at a name that genuinely resolves.
        # Without this the unit advertises `<unit_id>.local` while the
        # daemon owns `<unit_id>-9.local`, and the technician's
        # `avahi-resolve -n <unit_id>.local` times out.
        host_name = self._effective_host_name(requested_host_name)
        rendered = render_service_file(advertisement, host_name=host_name)

        if rendered == self._last_rendered:
            log.debug("advertisement unchanged; not reloading avahi")
            return False

        self._write_file(self.config.service_file, rendered)
        self._last_rendered = rendered

        if not self._reload_avahi():
            log.warning(
                "wrote %s but avahi reload failed; the running responder "
                "still advertises the previous payload",
                self.config.service_file,
            )
            return True

        # The reload may have moved the daemon to a different host
        # name (a contended segment) or back to the requested one.
        # Reconcile the file with what the daemon now owns.
        self._reconcile_host_name(advertisement, rendered)

        log.info(
            "published %s at %s (transport=%s ip=%s)",
            self._last_host_name or host_name,
            self.config.service_file,
            advertisement.transport or "?",
            advertisement.ip or "?",
        )
        return True

    def _reconcile_host_name(self, advertisement: Advertisement, rendered: str) -> None:
        """Correct the service file if the daemon owns a different name.

        After a reload the daemon may have settled on a different host
        name than the one written into the file — on a contended
        segment it takes a suffixed variant, and it re-registers the
        name on every reload, so it can move again.

        When the file and the daemon disagree, the daemon wins: the
        SRV record must point at a name that resolves. The corrected
        file is written but NOT reloaded — avahi has already loaded
        the service with the previous `<host-name>`, and the next
        payload change reloads it with the corrected one. Re-reloading
        here would move the daemon's name again and start the drift
        over.
        """
        requested = self.host_name(advertisement)
        actual = self._current_host_name()
        if not actual:
            log.warning(
                "could not confirm the avahi host name; leaving %s as advertised",
                requested,
            )
            return

        self._last_host_name = actual

        if actual in rendered:
            # The file already points at the name the daemon owns.
            return

        log.warning(
            "the avahi daemon owns %s but the service file pointed at a "
            "different name; correcting the file so the SRV record "
            "resolves. `avahi-resolve -n %s` will not resolve — another "
            "device on the LAN is using the unit's name.",
            actual,
            requested,
        )
        corrected = render_service_file(advertisement, host_name=actual)
        self._write_file(self.config.service_file, corrected)
        self._last_rendered = corrected

    def _effective_host_name(self, requested: str) -> str:
        """The host name to advertise — the one the daemon actually owns.

        When the daemon could not be asked, the requested name is used
        and the uncertainty is logged. Inventing a name would be
        worse than the situation we are trying to fix.
        """
        actual = self._current_host_name()
        if not actual:
            log.warning(
                "could not confirm the avahi host name; assuming %s", requested
            )
            return requested
        if actual != requested:
            log.warning(
                "the host name %s was already taken on this segment; the "
                "avahi daemon owns %s, so that is what is advertised. "
                "`avahi-resolve -n %s` will not resolve — another device "
                "on the LAN is using the unit's name.",
                requested,
                actual,
                requested,
            )
        return actual

    def run_forever(self, stop: Callable[[], bool] | None = None) -> None:
        """Poll until stopped.

        `stop` is a callable returning True when the loop should end —
        the signal handler sets a flag. A failure to resolve the
        payload is logged and retried on the next tick: a unit whose
        lease has not come up yet must not crash the responder.
        """
        stop = stop or (lambda: False)
        log.info(
            "aquabutler-mdns starting (poll=%.0fs service_file=%s)",
            self.config.poll_interval_sec,
            self.config.service_file,
        )
        while not stop():
            try:
                self.publish_once()
            except Exception:  # noqa: BLE001 - the loop must survive
                log.exception("publish cycle failed; retrying")
            time.sleep(self.config.poll_interval_sec)
        log.info("aquabutler-mdns stopped")


def _install_signal_handlers(flag: dict[str, bool]) -> None:
    """Set `flag['stop'] = True` on SIGTERM/SIGINT."""

    def handler(signum, _frame):  # pragma: no cover - signal path
        log.info("received signal %s; shutting down", signum)
        flag["stop"] = True

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def main(argv: list[str] | None = None) -> int:
    """Container entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="AquaButler mDNS advertiser")
    parser.add_argument(
        "--service-file",
        default=str(DEFAULT_SERVICE_FILE),
        help="path of the avahi static service file to write",
    )
    parser.add_argument(
        "--customer-id-path",
        default=DEFAULT_CUSTOMER_ID_PATH,
        help="path of the file holding the customer ID",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SEC,
        help="seconds between payload re-resolutions",
    )
    parser.add_argument(
        "--transport-refresh-interval",
        type=float,
        default=DEFAULT_REFRESH_INTERVAL_SEC,
        help=(
            "minimum seconds between two route queries; the route query "
            "is a fork/exec of `ip`, so it is rate-limited"
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="resolve and publish a single time, then exit",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = PublisherConfig(
        service_file=Path(args.service_file),
        customer_id_path=args.customer_id_path,
        poll_interval_sec=args.poll_interval,
        transport_refresh_interval_sec=args.transport_refresh_interval,
    )
    publisher = Publisher(config=config)

    if args.once:
        publisher.publish_once()
        return 0

    flag = {"stop": False}
    _install_signal_handlers(flag)
    publisher.run_forever(stop=lambda: flag["stop"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_POLL_INTERVAL_SEC",
    "Publisher",
    "PublisherConfig",
    "main",
]
