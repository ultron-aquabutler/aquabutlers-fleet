# Dockerfile — aquabutler-mdns
#
# The AquaButler device's mDNS advertiser. Publishes the
# `_aquabutler._tcp` DNS-SD service on the customer's residential LAN
# so a technician's laptop can find the unit and its current address
# without the cloud.
#
# Policy: `docs/Ethernet-deployment-policy.md` §2 in
#   ultron-aquabutler/AquaButler-Server (PR #35).
# Technician procedure: `docs/runbooks/residential-lan-field-guide.md` §2.
#
# Build contexts:
#   balena push   -> the balenalib raspberrypi4-64-alpine base
#                    (BALENA_MACHINE_NAME is substituted at push time)
#   docker build  -> alpine:3.20 (override via --build-arg BASE_IMAGE)
#
# Runtime:
#   - avahi-daemon  the mDNS responder (host-networked, so it sees the
#                   customer's LAN interface)
#   - avahi-tools   avahi-publish / avahi-browse / avahi-resolve
#   - the publisher writes /etc/avahi/services/aquabutler.service and
#     asks avahi-daemon to reload it when the payload changes
#
# IMPORTANT: this container MUST run with `network_mode: host`. mDNS is
# link-local multicast (224.0.0.251:5353) — a bridge-networked
# responder advertises onto the container's veth, not onto the
# customer's LAN, and the technician's laptop never sees it.

ARG BASE_IMAGE=alpine:3.20
FROM ${BASE_IMAGE}

LABEL maintainer="ultron-aquabutler"
LABEL org.opencontainers.image.title="aquabutler-mdns"
LABEL org.opencontainers.image.description="AquaButler device mDNS advertiser — publishes _aquabutler._tcp on the customer's residential LAN."
LABEL org.opencontainers.image.licenses="AGPL-3.0-only"
LABEL org.opencontainers.image.source="https://github.com/ultron-aquabutler/aquabutlers-fleet"

# avahi        - the responder daemon
# avahi-tools  - avahi-publish / avahi-browse / avahi-resolve
# iproute2     - `ip route get` (the shared transport source) and
#                `ip addr show` (the current LAN address)
# dbus         - avahi-daemon refuses to start without a D-Bus
#                connection; a private system bus is started in the
#                entrypoint. Also provides `dbus-send`, which the
#                publisher uses to ask the daemon which host name it
#                actually owns (see publisher._default_current_host_name)
# python3      - the publisher
# libcap       - avahi-daemon drops privileges via capabilities
#
# NOTE: Alpine has no `nss-mdns` package (it is Debian-only), so
# `.local` names are not resolvable *inside* this container. That is
# fine — name resolution is the technician's laptop's job, not the
# advertiser's. `avahi-browse`/`avahi-resolve` still work here
# because they talk to the daemon directly.
RUN apk add --no-cache \
        avahi \
        avahi-tools \
        dbus \
        iproute2 \
        libcap \
        python3 \
    && rm -rf /var/cache/apk/*

# The advertised software version. The publisher carries it in the
# `sw_version` TXT record. Override per device with a balena device
# variable if a fleet is versioned independently of the image.
ENV AQUABUTLER_SW_VERSION="0.1.0"

WORKDIR /app
COPY aquabutler_mdns ./aquabutler_mdns
COPY tests ./tests

# The avahi service file is *generated* at runtime — the `ip` and
# `transport` TXT values are runtime facts. This directory is bind-
# mounted in the balena service definition so the generated file is
# visible on the host for troubleshooting.
RUN mkdir -p /etc/avahi/services

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# avahi-daemon needs to bind the mDNS multicast socket and to talk to
# D-Bus; it drops to the `avahi` user for its own work but keeps the
# caps it needs for the socket.
ENV AVAHI_DBUS_MODE=python3:3

HEALTHCHECK --interval=60s --timeout=8s --start-period=30s --retries=3 \
    CMD avahi-daemon --check || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
