# First-Boot Provisioning on balenaOS RPi

> **Status**: 🟢 Active — verified 2026-09-17 (kanban `t_a24d5792`)
> **Audience**: Friday (code lane), Ultron (infra lane, on registry/builder), Bryan (operator who flashes Pis and runs `balena push`)
> **Scope**: How `aquabutlers-provisioner` captures the customer ID on a freshly flashed balenaOS Pi and triggers the DR auto-restore flow.
> **Parent**: `t_03d484e8` (Disaster-Recovery auto-restore feature, decomposed into 3 phases + wiki index)

---

## Overview

`aquabutlers-provisioner` is a new fleet sidecar that closes the loop on
pre-flashed Pi handoffs. Today, swapping in a new Pi requires a tech visit to
type a customer ID into the captive portal. With this service, the Pi boots
into one of two modes based on whether `/mnt/data/customer_id` already
exists on the persistent volume:

1. **Portal mode** (file missing) — bring up `hostapd` + `dnsmasq` on `wlan0`
   via the balenaOS host's `aquabutlers-portal.target` systemd unit. Serve a
   one-page form on `:80`. On submit: validate, persist, reboot.

2. **Restore mode** (file present) — connect to the central MQTT broker
   using env-var credentials (`MQTT_USER_<ID>` / `MQTT_PASS_<ID>`), subscribe
   to retained `pool/+/config/#`, publish `pool/<id>/cmd/restore` once, and
   forward each retained config message to `/mnt/data/njspc-config/` for
   njsPC to consume on next boot.

The service is a sibling of `poolController` and `relayEquipmentManager` in
the fleet compose, but uses `network_mode: host` + `privileged: true` so it
can bind `:80` on the captive-portal AP and `systemctl` the host's
`aquabutlers-portal.target`.

### Wire protocol (locked by orchestrator `t_03d484e8`)

| Topic                      | Direction   | QoS | Retain | Payload        | Source                |
|----------------------------|-------------|-----|--------|----------------|-----------------------|
| `pool/<id>/cmd/restore`    | Pi → broker | 1   | false  | `""` (empty)   | `aquabutlers-provisioner` |
| `pool/+/config/#`          | broker → Pi | 1   | true   | JSON config blob | central broker        |

The bridge (Phase B) subscribes to `pool/+/cmd/#` and routes `restore` to
the central DR handler. The config subscription drains whatever the central
broker has retained for this customer — typically schedule, pump defaults,
alert thresholds — so njsPC boots with the same config it had before the
swap.

### Repo

`https://github.com/ultron-aquabutler/aquabutlers-provisioner` — Node 20
TypeScript service, balenalib multi-stage Dockerfile, 25 unit tests +
real-mosquitto integration script.

---

## Architecture

### Service layout

```
src/
  customerId.ts    atomic read/write/validate of /mnt/data/customer_id
  portalServer.ts  captive-portal HTTP handler (pure + http server bootstrap)
  mqttBranch.ts    MQTT connect → subscribe pool/+/config/# → publish restore
  index.ts         orchestrator state machine (portal vs restore)
test/
  customerId.test.ts   8 tests — validation, atomicity, ENOENT, corruption
  portalServer.test.ts 8 tests — handler + ephemeral-port HTTP roundtrip
  mqttBranch.test.ts   6 tests — fake MQTT client, wire-protocol assertions
  index.test.ts        4 tests — state machine with injected exec + mqtt
  integration.live.ts  manual — runs against a real mosquitto (offline tests skip it)
Dockerfile         balenalib multi-stage (raspberrypi4-64-debian-node:20-bookworm)
                   build-arg BASE_IMAGE=node:20-alpine for local `docker build`
balena.yml         v1.1 manifest — sha-pending-pin, tracked in fleet balena.yml
```

### State machine

```
                    /mnt/data/customer_id
                    ┌─────────────────────┐
                    │ missing              │ present
                    ▼                     ▼
              ┌──────────┐         ┌──────────────┐
              │ portal   │ submit  │ restore      │ done
              │ mode     ├────────►│ mode         ├────►[exit / restart]
              │ (HTTP :80)│         │ (MQTT pub/sub)│
              └──────────┘         └──────────────┘
                    │                     ▲
                    └────reboot───────────┘
```

On form submit: write `customer_id`, `systemctl stop
aquabutlers-portal.target`, `systemctl reboot`. The balena supervisor
restarts `aquabutlers-provisioner`; the second boot finds the file and
enters restore mode.

### Persistent volume contract

`provisioner-data` (declared in both `docker-compose.yml` and `balena.yml
volumes:` block) mounts at `/mnt/data` inside the container. The customer ID
file lives at `/mnt/data/customer_id`. Config messages drained from the
central broker land in `/mnt/data/njspc-config/<id>-<key>.json` (keys with
slashes get collapsed to `__` for a flat filename).

The supervisor-managed volume survives balenaOS re-flash, which is the
whole point — the customer's ID carries across device swaps.

### Per-customer MQTT credentials

The orchestrator (`t_03d484e8`) flagged this as a known gap. Today, the
provisioner reads:

```
MQTT_USER_<ID_UPPER>=<user>
MQTT_PASS_<ID_UPPER>=<pass>
```

where `ID_UPPER = customer_id.toUpperCase().replace(/-/g, '_')`. These
must be set on the balena device via `balena env set` at registration
time. The follow-up card (filed at end of Phase A) covers the API that
generates these per-customer creds on first device registration. Without
that API, the Pi authenticates using a single shared bootstrap credential —
acceptable for the DR flow but blocks per-customer audit logging.

### Configuration sources

| Source              | What it sets                                | Who sets it          |
|---------------------|---------------------------------------------|----------------------|
| `balena.yml` env    | `MQTT_BROKER_URL` (central broker default)  | manifest author      |
| `balena env set`    | `MQTT_USER_<ID_UPPER>` / `MQTT_PASS_<ID_UPPER>` | fleet registration |
| `/mnt/data/customer_id` | the customer ID itself                 | portal submit or     |
|                     |                                             | pre-flash staging    |

---

## Operation

### Deploy

```bash
# 1. Build and push the provisioner image
cd ~/repos/aquabutlers-provisioner
npm install
npm test            # 25/25 should pass before push
docker build --build-arg BASE_IMAGE=balenalib/raspberrypi4-64-debian-node:20-bookworm \
  -t aquabutlers-provisioner:dev .
# (push to GHCR per AquaButler/OpenBalena_Fleet/CPE-Image-Build-Recipe.md)
# Tag with sha-<commit> after first push and update balena.yml `docker.provisioner.image`.

# 2. Add the sidecar to the fleet compose (already done in this PR — feat/aqb-017).
cd ~/repos/aquabutlers-fleet
docker compose build provisioner

# 3. (Bryan) `balena push aquabutlers-fleet` from the workstation. The OpenBalena
#    builder cross-compiles for ARM Pi targets and pushes the per-arch slices
#    back to registry.loc. Update the sha tag post-push.
```

### First-boot portal flow

1. Power on a freshly-flashed Pi (no `/mnt/data/customer_id`).
2. Wait ~30 seconds for balena supervisor + provisioner to come up.
3. The Pi broadcasts an open `AquaButlers-Setup` SSID. Connect from a phone.
4. Captive portal auto-opens (Android/iOS/Windows all hit `/` or
   `/generate_204` etc. — provisioner returns the form for any GET).
5. Submit the customer ID.
6. Form posts to `/submit`, validates, persists to `/mnt/data/customer_id`,
   reboots.
7. On the next boot, restore mode runs.

### Per-customer registration

```bash
# On the Aqua fleet, once the customer signs up:
balena env set MQTT_USER_ACME_POOLS=<secret-user> \
              MQTT_PASS_ACME_POOLS=<secret-pass> \
              --device <uuid>
# ACME_POOLS = 'acme-pools'.toUpperCase().replace(/-/g, '_')
```

The follow-up API (filed as a child card at end of Phase A) automates this
call from the central provisioning endpoint.

### Verify restore mode

```bash
# SSH into the device via OpenBalena VPN:
balena ssh <uuid> main provisioner

# Inside the container, watch logs:
journalctl -u balena -f | grep provisioner

# Expected output on a healthy restore:
#   [provisioner] checking for customer id at /mnt/data/customer_id
#   [provisioner] found customer id acme-pools — entering restore mode
#   [provisioner] restore published=true subscribed=true configWritten=3

# Confirm the bridge (Phase B) saw the restore:
mosquitto_sub -h mqtt.aquabutlers.com -t 'pool/+/cmd/#' -v &
# On the next device boot, you should see:
#   pool/acme-pools/cmd/restore (empty payload, retain=false)
```

### Re-provision (rare)

If a customer moves to a new site and needs a fresh ID (rare — most
swaps are device-replace, same ID):

```bash
balena ssh <uuid> main rm /mnt/data/customer_id
balena ssh <uuid> reboot
```

The Pi comes back up in portal mode and the on-site tech re-runs the
captive-portal flow.

---

## Configuration

### `docker-compose.yml` (fleet)

The provisioner block (added in this PR):

```yaml
  provisioner:
    build:
      context: ../aquabutlers-provisioner
      dockerfile: Dockerfile
    network_mode: host        # so :80 binds on wlan0 as the captive-portal DNS target
    privileged: true          # so it can systemctl the host's aquabutlers-portal.target
    restart: unless-stopped
    volumes:
      - provisioner-data:/mnt/data
    environment:
      - MQTT_BROKER_URL=mqtt://mqtt.aquabutlers.com:1883
      # Per-customer creds: MQTT_USER_<ID_UPPER>, MQTT_PASS_<ID_UPPER>
```

The `provisioner-data` volume (also added in this PR) is a named volume at
the top of the compose — supervisor-managed on balena, named on plain
docker.

### `balena.yml` (fleet)

Service block mirrors the compose — `image:
ghcr.io/ultron-aquabutler/aquabutlers-provisioner:sha-pending-pin`,
`network_mode: host`, `privileged: true`, `volumes:
provisioner-data:/mnt/data`. Healthcheck polls `:80` (portal mode) or
`/mnt/data/customer_id` (restore mode).

Pin update workflow per `AquaButler/OpenBalena_Fleet/Balena-YML-Pinning.md`:
after `balena push`, capture the new per-arch digests from `registry.loc`,
push to GHCR as `:sha-<commit>`, update `docker.provisioner.image` AND
`services.provisioner.image` to keep them in sync.

### `balena.yml` (standalone provisioner repo)

The provisioner repo ships its own balena.yml for independent deploy
(not used today — fleet is the deployment path). Pins via
`sha-pending-pin` until first push, then capture per-arch digests.

---

## Troubleshooting

> **TBD — route to Ultron (live-run authority) once the first real Pi has
> been flashed with this service.** The framework below is what to check
> when a tech reports provisioning problems; field-validated diagnostics
> go here once we've seen them.

### Pi boots but the SSID doesn't appear

**Possible cause**: `aquabutlers-portal.target` on the balenaOS host isn't
installed or failed to start.

**Check**:

```bash
balena ssh <uuid> host systemctl status aquabutlers-portal.target
journalctl -u aquabutlers-portal.target -n 50
```

If the target unit is missing, install it per the balenaOS host image
recipe (not in this card's scope — see Phase C / Ultron lane).

### Captive portal loads but submit returns 400

**Possible cause**: customer ID submitted doesn't match the regex
`^[a-z0-9_-]{3,64}$` (uppercase, spaces, or too-long IDs are rejected).

**Fix**: re-submit with a valid ID. The form shows the regex hint inline.

### Restore publishes but `cmd/restore` never reaches the central DR handler

**Possible cause**: the bridge's `pool/+/cmd/#` routing for the `restore`
sub-key is broken (separate Phase B card). Or the per-customer MQTT
credentials are stale/wrong.

**Check**:

```bash
# On the device:
balena ssh <uuid> main provisioner sh -c 'env | grep MQTT_'
# Verify MQTT_USER_<ID_UPPER> / MQTT_PASS_<ID_UPPER> are set and match
# what's in the broker's ACL.

# On the Aqua broker:
mosquitto_sub -h mqtt.aquabutlers.com -t 'pool/+/cmd/#' -v -u <admin> -P <pw>
# Trigger a restore:
balena ssh <uuid> rm /mnt/data/customer_id
balena ssh <uuid> reboot
# After reboot, watch for pool/<id>/cmd/restore (empty payload).
```

If the message reaches the broker but the DR handler doesn't fire, route
to the Phase B card owner.

### `/mnt/data/customer_id` exists but the file content is invalid

**Behavior**: `makeFsStore.read()` returns `null` on invalid content
(corruption recovery — see `customerId.test.ts` "corruption recovery"
test). The runner falls back to portal mode, overwriting the bad file on
the next submit.

**Workaround**: if you want to keep the original ID, fix it manually:

```bash
balena ssh <uuid> main sh -c 'echo "good-id" > /mnt/data/customer_id'
balena ssh <uuid> reboot
```

---

## Dependencies

### External services

- **BalenaOS host image** — must include the `aquabutlers-portal.target`
  systemd unit (hostapd + dnsmasq bring-up). Out of scope for this card;
  ships as part of the base image recipe.
- **Central MQTT broker** (`mqtt.aquabutlers.com:8883`) — accepts
  `pool/<id>/cmd/restore` publishes and serves retained `pool/+/config/#`.
  Provisioner connects via `mqtts://...` when TLS is on (depends on
  `MQTT_BROKER_URL`).
- **Per-customer MQTT credentials** — `MQTT_USER_<ID_UPPER>` /
  `MQTT_PASS_<ID_UPPER>` device env vars. See "known gap" above; the
  follow-up API automates this.

### Sibling services in the fleet

- **aquabutlers-mqtt-bridge** (Phase B is the lane that handles
  `cmd/restore` routing) — subscribes `pool/+/cmd/#`, routes `restore` to
  the central DR handler. Separate kanban card.
- **njsPC** (existing) — consumes `/mnt/data/njspc-config/<id>-<key>.json`
  on boot. The provisioner writes retained config here; njsPC reads on
  startup. Wire format is JSON blob (whatever the central broker
  retained).

### Related wiki pages

- `AquaButler/OpenBalena_Fleet/index` — parent OpenBalena Fleet overview
- `AquaButler/OpenBalena_Fleet/Balena-YML-Pinning` — pin update procedure
  for `aquabutlers-provisioner:sha-pending-pin`
- `AquaButler/OpenBalena_Fleet/CPE-Image-Build-Recipe` — how to build and
  push the provisioner image from source
- `AquaButler/OpenBalena_Fleet/Registry_Integration` — how the OpenBalena
  builder pulls from `registry.loc` and `ghcr.io`

### Kanban

- `t_a24d5792` — this card (Phase A: provisioner)
- `t_03d484e8` — parent (DR auto-restore decomposition)
- Phase B (bridge handler + routing bug fix) — sibling card, separate
  Friday lane
- Phase C (physical drill) — Ultron lane
- Per-customer MQTT creds API — follow-up card filed at completion

---

## See Also

- Balena docs: [device env vars](https://docs.balena.io/learn/manage/serv-vars/),
  [persistent storage](https://docs.balena.io/reference/storage/volumes)
- Eclipse Mosquitto: [retained messages](https://mosquitto.org/man/mosquitto-8.html)
- `AquaButler/OpenBalena_Fleet/index` — parent overview
