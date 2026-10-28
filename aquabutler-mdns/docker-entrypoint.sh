#!/bin/sh
# docker-entrypoint.sh — start D-Bus, avahi-daemon and the aquabutler-mdns publisher.
#
# Three processes, in dependency order:
#
#   1. dbus-daemon  a private system bus. avahi-daemon refuses to
#                   start without a D-Bus connection — it exits with
#                   "Failed to contact D-Bus daemon" otherwise. The
#                   container has no D-Bus of its own, and balenaOS's
#                   host bus is not bind-mounted in.
#   2. avahi-daemon the mDNS responder.
#   3. the publisher writes the avahi static service file and asks the
#                   daemon to reload it.
#
# The publisher depends on the daemon: if the daemon is not up yet, the
# reload fails and the running responder keeps advertising the previous
# payload (or nothing, on first boot). So the publisher waits for the
# daemon before its first publish.
#
# avahi-daemon runs in the foreground unless `-D/--daemonize` is given,
# so no foreground flag is needed (and `-f` is NOT it — `-f` is
# `--file=FILE` and takes an argument).

set -eu

SERVICE_FILE="${AQUABUTLER_SERVICE_FILE:-/etc/avahi/services/aquabutler.service}"
CUSTOMER_ID_PATH="${AQUABUTLER_CUSTOMER_ID_PATH:-/mnt/data/customer_id}"
POLL_INTERVAL="${AQUABUTLER_POLL_INTERVAL:-15}"
DBUS_SOCKET="${DBUS_SYSTEM_BUS_SOCKET:-/var/run/dbus/system_bus_socket}"

# -------------------------------------------------------------------------
# 1. D-Bus
# -------------------------------------------------------------------------
# avahi-daemon publishes its records over D-Bus and exits when it cannot
# reach the bus. Start a private system bus first.
echo "aquabutler-mdns: starting dbus-daemon"

mkdir -p "$(dirname "$DBUS_SOCKET")"

dbus-daemon --system --fork --print-address=1 --print-pid=1 \
    > /tmp/dbus-address 2>/dev/null || true

DBUS_PID="$(sed -n 's/^[0-9]*$//p' /tmp/dbus-address 2>/dev/null | tail -1 || true)"
if [ -z "${DBUS_PID}" ]; then
    # --print-pid may not have produced a parsable line; fall back to
    # pgrep. The bus is what matters, not the pid.
    DBUS_PID="$(pgrep -f 'dbus-daemon --system' 2>/dev/null | head -1 || true)"
fi

# Wait for the socket to appear — avahi-daemon will not retry.
dbus_wait=0
while [ ! -S "$DBUS_SOCKET" ] && [ "$dbus_wait" -lt 20 ]; do
    dbus_wait=$((dbus_wait + 1))
    sleep 0.5
done

if [ -S "$DBUS_SOCKET" ]; then
    echo "aquabutler-mdns: dbus-daemon is up"
    export DBUS_SYSTEM_BUS_ADDRESS="unix:path=$DBUS_SOCKET"
else
    echo "aquabutler-mdns: WARNING - no D-Bus socket at $DBUS_SOCKET" >&2
    # Fall back to disabling avahi's D-Bus interface rather than
    # refusing to start. The publisher talks to the daemon over
    # `avahi-daemon --reload`, not over D-Bus, so the responder still
    # works — it just cannot be introspected with avahi-browse from
    # inside this container.
    printf '[server]\nenable-dbus=no\n' > /etc/avahi/avahi-daemon.conf
fi

# -------------------------------------------------------------------------
# 2. avahi-daemon
# -------------------------------------------------------------------------
# In a container with `network_mode: host` avahi sees the real LAN
# interfaces directly, which is what makes the advertisement visible on
# the customer's segment.
echo "aquabutler-mdns: starting avahi-daemon"

avahi-daemon --no-drop-root --no-chroot &
AVAHI_PID=$!

# avahi-daemon exits if it cannot set up the mDNS socket (no usable
# interface yet — the unit's lease may not have come up). Retry a few
# times rather than crash-looping the container, but give up eventually
# so the supervisor surfaces the problem.
attempt=1
max_attempts=5
while ! avahi-daemon --check 2>/dev/null; do
    if [ "$attempt" -ge "$max_attempts" ]; then
        echo "aquabutler-mdns: avahi-daemon did not come up after ${max_attempts} attempts" >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    echo "aquabutler-mdns: waiting for avahi-daemon (attempt ${attempt})"
    sleep 2
done

echo "aquabutler-mdns: avahi-daemon is up"

# -------------------------------------------------------------------------
# 3. the publisher
# -------------------------------------------------------------------------
PUBLISHER_ARGS="--service-file $SERVICE_FILE --customer-id-path $CUSTOMER_ID_PATH --poll-interval $POLL_INTERVAL"

# `--once` (forwarded from the container's command line) publishes a
# single snapshot and exits — the mode used by the smoke test and by a
# one-shot check on a live device.
if [ "${1:-}" = "--once" ]; then
    # shellcheck disable=SC2086
    exec python3 -m aquabutler_mdns.publisher $PUBLISHER_ARGS --once
fi

# shellcheck disable=SC2086
python3 -m aquabutler_mdns.publisher $PUBLISHER_ARGS &
PUBLISHER_PID=$!

# Forward termination to every child, then wait.
term() {
    kill -TERM "$PUBLISHER_PID" 2>/dev/null || true
    kill -TERM "$AVAHI_PID" 2>/dev/null || true
    if [ -n "${DBUS_PID:-}" ]; then
        kill -TERM "$DBUS_PID" 2>/dev/null || true
    fi
}
trap term TERM INT

# If any process exits, tear the others down so the container restarts
# as a unit rather than limping along with half the advertisement
# working.
wait -n "$AVAHI_PID" "$PUBLISHER_PID" 2>/dev/null || wait "$AVAHI_PID" "$PUBLISHER_PID"

term
wait || true
