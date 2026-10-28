# aquabutler-mdns

The AquaButler device's **mDNS advertiser**. Publishes the `_aquabutler._tcp`
DNS-SD service on the **customer's residential LAN** so a technician's laptop
can find the unit and its current address **without the cloud**.

That is the LAN-local fallback for the offline-first behaviour the
architecture is built around (`AquaButler-Hardware/docs/Architecture.md` §11).

> **Policy**: `docs/Ethernet-deployment-policy.md` §2 in
> `ultron-aquabutler/AquaButler-Server` (PR #35).
> **Technician procedure**: `docs/runbooks/residential-lan-field-guide.md` §2.

---

## What it advertises

| TXT key | Source |
|---------|--------|
| `unit_id` | `AQUABUTLER_UNIT_ID` → the balena device UUID |
| `customer_id` | `/mnt/data/customer_id` (written by the provisioner) |
| `sw_version` | `AQUABUTLER_SW_VERSION` |
| `transport` | the shared transport source — `eth` or `wifi` |
| `ip` | the current address of the **active** interface |

Service type `_aquabutler._tcp`, port **5150** (the device's own dashPanel
UI). The unit's mDNS name is `<unit_id>.local`.

**Identity only.** mDNS is unauthenticated and broadcast to every host on the
segment, so MQTT credentials, the OpenBalena device token and the customer's
WiFi passphrase must never appear in the payload. `tests/test_no_secrets.py`
enforces this mechanically — the environment is planted with every secret the
device could plausibly expose to this container, and none of it may reach the
rendered service file.

## Running it

```bash
# On the device (balenaOS) — see the balena.yml service block.
# Locally:
docker compose up aquabutler-mdns
```

`network_mode: host` is **required**. mDNS is link-local multicast
(`224.0.0.251:5353`) and does not cross a routed boundary — a bridge-networked
responder advertises onto the container's veth, not onto the customer's LAN.

## Verifying it

```bash
avahi-browse -rt _aquabutler._tcp      # from a laptop on the customer's LAN
avahi-resolve -n <unit_id>.local
```

## Tests

```bash
cd aquabutler-mdns && python3 -m pytest -q
```

The suite covers the payload's key set, the no-secrets guarantee, the
active-interface resolution, and the host-name claim (including the
`avahi-set-host-name` conflict case, where avahi silently takes a suffixed
variant and the unit would otherwise advertise a name that does not resolve).

The transport implementation is kept in step with the rotary display's copy by
`tests/test_transport.py::test_transport_implementation_matches_the_rotary_display`,
which loads the rotary display's real module when both are present on the same
machine.

## Layout

| File | Purpose |
|------|---------|
| `aquabutler_mdns/publisher.py` | The publish loop: resolve → render → write → reload. |
| `aquabutler_mdns/identity.py` | The TXT payload and the key set policy. |
| `aquabutler_mdns/transport.py` | The shared active-transport source (parity-tested against the rotary display). |
| `aquabutler_mdns/netinfo.py` | The current residential-LAN address of the active interface. |
| `aquabutler_mdns/avahi_service.py` | Rendering and parsing the avahi service file. |
| `docker-entrypoint.sh` | Starts D-Bus, then avahi-daemon, then the publisher. |
