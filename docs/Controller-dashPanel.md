# Controller-dashPanel — CPE Dashboard Service

> **Status**: Active (post-fork-rebase, 2026-09-23)
> **Fleet service**: `dashboard` in `aquabutlers-fleet/balena.yml`
> **Source repo**: `ultron-aquabutler/Controller-dashPanel` (GitHub), upstream `rstrouse/nodejs-poolcontroller-dashPanel`
> **Build**: local-source via `build-njspc-dash.sh` → local registry → `balena push` by OpenBalena builder
> **Registry pin**: `registry.loc.wallacearizona.us/ultron-aquabotler/njspc-dash:sha-6551aa3` (fork HEAD post-rebase-merge, 2026-09-23)
> **Upstream sync**: v10.0.1 merged 2026-09-23 (kanban `t_532f0b86`); fork master at `6551aa3` on `ultron-aquabutler/Controller-dashPanel`
> **OCI digest**: amd64 `sha256:8b5602df1a33b150d1f5b9ed44d552d3203f255b07dd0560a6512ca1696a285c` (built locally 2026-09-23 from fork HEAD 6551aa3)

> **Pending vault ingest**: This file is the durable doc-in-commit copy (per SOUL.md Cross-Agent Documentation Handoff rule). When the Obsidian vault write path is restored (DNS NXDOMAIN on `obsidian.loc.wallacearizona.us` + NFS RO at `/mnt/Stor1/nfs_appdata` as of 2026-09-23), this content should be ingested to `AquaButler/Controller-dashPanel.md` (or equivalent service page). Until then, this commit is the canonical home.

---

## Overview

The `dashboard` service is the web UI for the njsPC pool controller. It is a Node.js/TypeScript application that serves a single-page dashboard (jQuery 4, Socket.IO) backed by the `njspc` relay service. It runs on customer Raspberry Pi devices as part of the `aquabutlers-fleet` OpenBalena deployment and also has a standalone mode for local testing.

This page documents the fleet-deployed service. For the local build recipe, see [[./CPE-Image-Build-Recipe|CPE Image Build & Push Recipe]].

---

## Architecture

```
Customer RPi (balenaOS)
  └─ dashboard container (njspc-dash)
       ├─ Express 5 web server (:5150)
       ├─ Socket.IO client → poolController relay
       ├─ MQTT client → Mosquitto cluster
       └─ File uploads (multer) → /app/data/uploads/

Registry (local, amd64 base)
  └─ registry.loc.wallacearizona.us/ultron-aquabutler/njspc-dash:sha-6551aa3

Build path:
  ultron-aquabutler/Controller-dashPanel (fork, upstream-synced)
       │  npm run build (TypeScript → dist/app.js)
       │  npm run build-scss (SCSS → themes/aqualinkd/css/)
       ▼
  build-njspc-dash.sh (local Dockerfile, node:22-bookworm)
       │  docker build → OCI image (amd64)
       │  docker push → registry.loc.wallacearizona.us/ultron-aquabutler/njspc-dash
       ▼
  OpenBalena builder (balena push, QEMU cross-compile)
       │  Pulls amd64 base from registry.loc
       │  Cross-compiles for arm64/arm/v7
       │  Pushes per-arch manifests back to registry.loc
       ▼
  registry.loc — multi-arch OCI index (amd64 + arm64 + arm/v7)
```

### Upstream vs Fork

| Aspect | Upstream | Fork |
|--------|----------|------|
| Repo | `rstrouse/nodejs-poolcontroller-dashPanel` | `ultron-aquabutler/Controller-dashPanel` |
| Version | v10.0.1 (ab8060c) | v10.0.1 + AqualinkD theme (fork master 6551aa3) |
| Merge-base | `ab8060c` (= upstream HEAD itself) | fork is fully caught up to upstream |
| Fork-local commits | — | 10 commits: GHCR workflow, AqualinkD theme, balena.yml, aqualinkd-dark theme, multi-arch support |
| Security module | `server/api/Security.ts` + `server/security/SecurityService.ts` | Included (from upstream a0cb967, default-import form) |
| Theme | Default | AqualinkD branded (aqualinkd + aqualinkd-dark) |
| balena.yml | None | Fleet manifest for OpenBalena |
| GHCR publish | None | `.github/workflows/ghcr-publish.yml` |

---

## Operation

### Start / Stop / Restart

```bash
# Via OpenBalena (device side)
ssh <device-hostname>
balena stop dashboard
balena restart dashboard
balena logs dashboard

# Standalone (local dev / smoke test)
docker run --rm --network host \
  -e MQTT_BROKER_URL=mqtt://127.0.0.1:1883 \
  registry.loc.wallacearizona.us/ultron-aquabutler/njspc-dash:sha-6551aa3 \
  node dist/app.js
```

### Health Check

```bash
# Container is healthy if it binds to 0.0.0.0:5150
curl -s http://localhost:5150/ | grep -o '<title>.*</title>'

# Or via the upstream healthcheck path
curl -fsS http://127.0.0.1:5150/config/appVersion?health || exit 1
```

### Log Locations

```bash
# Via balena
balena logs dashboard

# Standalone (stdout/stderr)
docker logs $(docker ps -q --filter ancestor=registry.loc.wallacearizona.us/ultron-aquabutler/njspc-dash:sha-6551aa3)
```

### Build & Push (Friday lane)

```bash
# 1. Ensure source is upstream-synced (fork HEAD on master)
cd /home/serveradmin/.hermes/profiles/friday/repos/AquaButler-Control-dashPanel
git checkout master
git pull

# 2. Build (no in-tree modifications required; fork master is canonical)
bash /home/serveradmin/.hermes/kanban/boards/aquabutlers/attachments/t_971a642f/build-njspc-dash.sh

# 3. Capture new digest
DIGEST=$(docker inspect --format='{{index .Id}}' \
  registry.loc.wallacearizona.us/ultron-aquabutler/njspc-dash:sha-6551aa3)
echo "Digest: ${DIGEST}"

# 4. Update balena.yml pin (Friday's job — see Balena-YML-Pinning)
# Then run: balena push aquabutlers-fleet  (Bryan's workstation with balena CLI)
```

### Update / Redeploy

```bash
# Pull latest source and rebuild
git -C /home/serveradmin/.hermes/profiles/friday/repos/AquaButler-Control-dashPanel pull
bash /home/serveradmin/.hermes/kanban/boards/aquabutlers/attachments/t_971a642f/build-njspc-dash.sh

# OpenBalena: balena push aquabutlers-fleet (from Bryan's workstation with balena CLI)
# This cross-compiles and pushes per-arch manifests to registry.loc

# Device receives OTA update automatically (OpenBalena release channel)
```

---

## Configuration

### Environment Variables (balena.yml)

| Variable | Default | Purpose |
|----------|---------|---------|
| `MQTT_BROKER_URL` | `mqtt://mqtt:1883` | MQTT broker for telemetry (in-cluster `mqtt` service) |
| `PORT` | `5150` | HTTP server port |
| `UPLOAD_DIR` | `/app/data/uploads` | Multer file upload directory |

### Key Files

| File | Purpose |
|------|---------|
| `dist/app.js` | Compiled entry point |
| `defaultConfig.json` | Default configuration (security block included post-sync) |
| `server/api/Security.ts` | Security routes (upstream a0cb967, default-import form) |
| `server/security/SecurityService.ts` | Security service (scrypt PIN hashing) |
| `themes/aqualinkd/` | AqualinkD-branded theme CSS/JS |
| `themes/aqualinkd-dark/` | AqualinkD dark mode theme |

### Registry Credentials

```bash
# Local registry (pre-populated)
cat ~/.docker/config.json | jq '.auths["registry.loc.wallacearizona.us"]'
# User: docker, password: 5!2tL0!Q5W2ZzI (htpasswd)
```

---

## Troubleshooting

### npm run build exits non-zero

**Cause**: TypeScript compilation errors.

**Diagnosis**:
```bash
cd /home/serveradmin/.hermes/profiles/friday/repos/AquaButler-Control-dashPanel
npm run build 2>&1 | grep -E "error TS|Error"
```

**Common errors**:

| Error | Cause | Fix |
|-------|-------|-----|
| `TS2307: Cannot find module './api/Security'` | Fork was behind upstream; Security.ts missing | Sync from upstream: `git checkout master && git pull` (fork master now includes Security.ts) |
| `TS2315: Type 'Server' is not generic` | `@types/ws@8.x` / `socket.io@4.x` type defs vs old TS | Add `"skipLibCheck": true` to `tsconfig.json` (NOT needed post-rebase; fork tsconfig already has it implicitly via ort strategy) |
| `TS2349: This expression is not callable` | `import * as extend` namespace call without `esModuleInterop` | Use default import: `import extend from 'extend'` (handled in post-rebase v10.0.1 sync; fork source uses default form) |
| `Merge conflict marker encountered` | Local clone has uncommitted WIP from prior session | `git reset HEAD --` to unstage, then `git checkout master` to abandon WIP (canonical fork is on `master`, not `feature/rebased`) |

### Container exits immediately on startup

**Cause**: Missing runtime directories (standalone mode) or OCI index mismatch (ARM device).

**Diagnosis**:
```bash
docker logs $(docker ps -aq --filter ancestor=registry.loc.wallacearizona.us/ultron-aquabutler/njspc-dash:sha-6551aa3)
```

**If error is `/app/defaultConfig.json not found`**: add `COPY --from=build /app/defaultConfig.json ./defaultConfig.json` to the Dockerfile prod stage.

**If ARM device**: verify the multi-arch OCI index was produced by `balena push`. See [[./Balena-YML-Pinning|Balena-YML-Pinning Policy]].

### balena push fails with pull access denied

**Cause**: `registry.loc` auth expired, or builder's registry secret is stale.

**Fix**:
```bash
# Re-login on build host
docker login registry.loc.wallacearizona.us -u docker

# On Aqua builder (Ultron lane)
ssh aqua1 "docker login registry.loc.wallacearizona.us -u docker"
docker --context aqua-swarm secret inspect registry_loc_password 2>/dev/null || \
  echo -n '5!2tL0!Q5W2ZzI' | docker --context aqua-swarm secret create registry_loc_password -
```

### Dashboard shows blank page or 404 on /

**Diagnosis**:
```bash
# Check web server is responding
curl -s http://localhost:5150/ | head -5

# Check Socket.IO handshake
curl -s http://localhost:5150/socket.io/?EIO=4&transport=polling
```

**If blank**: verify `themes/aqualinkd/css/` was compiled and copied into the image. Run `npm run build-scss` before building.

### OCI digest mismatch after rebase

**Symptom**: Balena deploy fails with "manifest unknown" or "digest mismatch".

**Cause**: balena.yml pinned to old digest, or the local registry doesn't have the new digest yet.

**Fix**:
```bash
# Rebuild from current fork HEAD
bash /home/serveradmin/.hermes/kanban/boards/aquabutlers/attachments/t_971a642f/build-njspc-dash.sh

# Capture the new amd64 digest
DIGEST=$(docker inspect --format='{{index .Id}}' \
  registry.loc.wallacearizona.us/ultron-aquabotler/njspc-dash:<tag>)
echo "New digest: ${DIGEST}"

# Update balena.yml with the new sha-tag (matching fork HEAD commit SHA) and
# add the new digest to the per-arch digests comment block.
```

---

## Dependencies & Topology

### What this service depends on

| Dependency | Host / Address | Purpose |
|------------|----------------|---------|
| `njspc` relay service | `poolcontroller` (in-cluster) :5000 | REST relay to pool controller |
| Mosquitto MQTT | `mqtt` (in-cluster) :1883 | Telemetry bus |
| Local registry | `registry.loc.wallacearizona.us` | OCI image source |

### What depends on this service

| Consumer | Relationship |
|----------|-------------|
| `aquabutlers-fleet` (balena.yml) | Fleet manifest declares `dashboard` service |
| Customer RPi devices | Run this as a balena container |

### Network / VLAN

- **Fleet deployment**: `balenaNetwork` (balenaOS managed network)
- **VLAN**: customer-site LAN (not homelab VLANs)
- **Ports**: 5150/tcp (HTTP, no TLS — TLS terminates at balena proxy)

---

## See Also

- [[./CPE-Image-Build-Recipe|CPE Image Build & Push Recipe]] — local build, registry push, digest capture
- [[./Balena-YML-Pinning|Balena-YML-Pinning Policy]] — how fleet pins are updated after build
- Kanban `t_971a642f` — original local build PREREQ
- Kanban `t_532f0b86` — upstream v10.0.1 sync (fork rebase)
- Kanban `t_0928eb33` — build verification and balena.yml pin
- Kanban `t_643b57f8` — fork rebase card (this page is the resolution)