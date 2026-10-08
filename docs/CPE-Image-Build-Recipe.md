# CPE Image Build & Push Recipe

> **Status**: Active (post-fork-rebase, 2026-09-23)
> **Scope**: How the AquaButlers dashboard CPE image is built from local source and pushed to the local registry, ready for `balena push` cross-compile.
> **Audience**: Friday (code lane), Bryan (operator who runs `balena push`)
> **Vault ingest pending**: This is the durable doc-in-commit copy. Vault path `AquaButler/OpenBalena_Fleet/CPE-Image-Build-Recipe.md` should be updated to match when Obsidian writes are restored.

---

## Quick Recipe

```bash
# 1. Ensure fork source is on master (post-rebase HEAD 6551aa3)
cd /home/serveradmin/.hermes/profiles/friday/repos/AquaButler-Control-dashPanel
git checkout master
git status  # must show "## master...origin/master" with no uncommitted files

# 2. Build the local amd64 image (no balena CLI required)
TAG=sha-6551aa3 bash \
  /home/serveradmin/.hermes/kanban/boards/aquabutlers/attachments/t_971a642f/build-njspc-dash.sh

# 3. Capture the new OCI digest
DIGEST=$(docker inspect --format='{{index .Id}}' \
  registry.loc.wallacearizona.us/ultron-aquabutler/njspc-dash:sha-6551aa3)
echo "Digest: ${DIGEST}"

# 4. Update aquabutlers-fleet/balena.yml dashboard pin to the new sha-tag
#    (matches fork HEAD commit SHA) and add the new digest to the per-arch
#    digests comment block.

# 5. (Bryan) balena push aquabutlers-fleet to cross-compile for arm64/arm/v7
```

## Prerequisites

- Docker CLI installed (verified `docker --version` = 27.5.1)
- Local registry credentials in `~/.docker/config.json`:
  - User: `docker`, Password: `5!2tL0!Q5W2ZzI`
- Source dir: `/home/serveradmin/.hermes/profiles/friday/repos/AquaButler-Control-dashPanel/`
- Fork HEAD on `master` (NOT `feature/rebased` — that's an abandoned branch)

## Why we don't use balena CLI on this box

`balena` CLI is not installed on this build host (verified `which balena` → not found). Cross-compile for arm64 + arm/v7 happens via OpenBalena builder (Ultron's lane on `aqua1`). The local-registry push is the handoff point.

## Build process

### 1. Fork source state

The fork (`ultron-aquabutler/Controller-dashPanel`) master branch is at:

- **HEAD**: `6551aa3` ("Merge fork-sync/v10.0.1-sync: upstream v10.0.1 rebase complete")
- **Merge-base with upstream**: `ab8060c` (upstream HEAD itself — fork is fully caught up)
- **Fork-local commits ahead**: 10 (GHCR workflow, AqualinkD theme, balena.yml, aqualinkd-dark theme, multi-arch support)

```bash
git -C /home/serveradmin/.hermes/profiles/friday/repos/AquaButler-Control-dashPanel log --oneline -1
# 6551aa3 Merge fork-sync/v10.0.1-sync: upstream v10.0.1 rebase complete
```

### 2. Verify the source builds clean

```bash
cd /home/serveradmin/.hermes/profiles/friday/repos/AquaButler-Control-dashPanel
git checkout master
npm install
npm run build
# Expected: exit 0, dist/app.js sha256 = b73147ceb85db5266e895022499a5b8c62557c1c25de7c12332794a512e7eac0
```

The source-bundle hash `b73147ceb...` is the integrity marker — it must match the upstream v10.0.1 tag build (no in-tree patches).

### 3. Build the local amd64 image

```bash
TAG=sha-6551aa3 bash \
  /home/serveradmin/.hermes/kanban/boards/aquabutlers/attachments/t_971a642f/build-njspc-dash.sh

# Output:
#   Successfully tagged njspc-dash:sha-6551aa3
#   Successfully tagged registry.loc.wallacearizona.us/ultron-aquabutler/njspc-dash:sha-6551aa3
```

The local Dockerfile (`Dockerfile.njspc-dash.local`, attached at `t_971a642f`) substitutes the balena template var with `node:22-bookworm` for a plain amd64 build.

### 4. Push to local registry

The build script pushes by default. After push:

```bash
curl -sf -u "docker:5!2tL0!Q5W2ZzI" \
  "https://registry.loc.wallacearizona.us/v2/ultron-aquabutler/njspc-dash/tags/list"
# Expected output includes "sha-6551aa3" tag
```

### 5. Capture the OCI digest

```bash
DIGEST=$(docker inspect --format='{{index .Id}}' \
  registry.loc.wallacearizona.us/ultron-aquabutler/njspc-dash:sha-6551aa3)
echo "Digest: ${DIGEST}"
# Example: sha256:8b5602df1a33b150d1f5b9ed44d552d3203f255b07dd0560a6512ca1696a285c
```

### 6. Update balena.yml pin

In `aquabutlers-fleet/balena.yml`, update:

1. The `docker.dashboard.image` line to use the new sha-tag (matching fork HEAD commit SHA):
   ```yaml
   image: registry.loc.wallacearizona.us/ultron-aquabutler/njspc-dash:sha-6551aa3
   ```

2. The comment block above the `docker:` section:
   - Update the sha-tag from old → new
   - Update the fork HEAD commit SHA reference
   - Add the new per-arch digest to the digest list (amd64 verified; arm64 + arm/v7 after `balena push`)

## Troubleshooting

### `npm run build` fails with TS2349 errors

**Old (pre-rebase) cause**: `import * as extend` namespace call without `esModuleInterop`.

**Resolution**: This was fixed by the upstream v10.0.1 sync (kanban `t_532f0b86`). Fork source now uses `import extend from 'extend'` (default form) and `esModuleInterop: true` is in `tsconfig.json`. If you see TS2349 errors, you're on an old branch — `git checkout master` to get the canonical state.

### `npm run build` fails with "Merge conflict marker encountered"

**Cause**: Local clone has uncommitted WIP from a prior abandoned session (e.g., on `feature/rebased` or `main`).

**Fix**:
```bash
cd /home/serveradmin/.hermes/profiles/friday/repos/AquaButler-Control-dashPanel
git checkout master  # abandon the WIP branch
git status  # must be clean
```

### `docker build` fails on missing `ssh_config`

**Cause**: The local Dockerfile recipe was customized for a WIP branch that had a `ssh_config` file. The canonical fork (`master`) does NOT have `ssh_config`.

**Fix**: Use the recipe attached at `t_971a642f` (path: `kanban/boards/aquabutlers/attachments/t_971a642f/Dockerfile.njspc-dash.local`). The current version does NOT copy `ssh_config` — only `package*.json`, `dist/`, `defaultConfig.json`, and `node_modules/`.

### `docker push` fails with auth error

**Cause**: Local-registry credentials expired or not in `~/.docker/config.json`.

**Fix**:
```bash
docker login registry.loc.wallacearizona.us -u docker
# Password: 5!2tL0!Q5W2ZzI
```

### `balena push aquabutlers-fleet` fails with "manifest unknown"

**Cause**: balena.yml references a sha-tag that the local registry doesn't have (typo or stale pin).

**Fix**: Verify the sha-tag exists in the registry:
```bash
curl -sf -u "docker:5!2tL0!Q5W2ZzI" \
  "https://registry.loc.wallacearizona.us/v2/ultron-aquabutler/njspc-dash/tags/list"
```

If missing, rebuild and re-push from step 3.

### Container boots but immediately exits with ENOENT on `/app/scripts/messages/testModules`

**Cause**: Missing runtime directory in standalone mode.

**Impact**: Non-fatal. The dashboard container logs the error but continues running. UI may show a missing-test-modules warning on first load. The dashboard itself works.

**Fix** (optional): Add to the Dockerfile prod stage:
```dockerfile
RUN mkdir -p /app/scripts/messages/testModules
```

---

## Stale vault entries (pre-rebase)

These entries in the original Obsidian wiki are NOW OBSOLETE and should be corrected when vault writes are restored:

1. `AquaButler/OpenBalena_Fleet/CPE-Image-Build-Recipe.md` — Troubleshooting section
   - **Old**: "**Do NOT** add `esModuleInterop: true` — that breaks the fork's existing `import * as extend` namespace calls"
   - **New**: Fork has been rebased onto upstream v10.0.1 with `esModuleInterop: true` and default imports; the namespace-import workaround is no longer needed.

2. `AquaButler/OpenBalena_Fleet/Balena-YML-Pinning.md` — Dashboard pin
   - **Old**: `njspc-dash:sha-0640f88`
   - **New**: `njspc-dash:sha-6551aa3` (and `sha256:8b5602df1a33...` for amd64 digest)

3. `AquaButler/OpenBalena_Fleet/OpenBalena_Fleet.md` — Source repos list
   - **Old**: `brydenver2/AquaButler-Control-dashPanel` (404 — repo was renamed)
   - **New**: `ultron-aquabutler/Controller-dashPanel` (the canonical fork)

---

## See Also

- [[./Balena-YML-Pinning|Balena-YML-Pinning Policy]]
- [[./Registry_Integration|Registry Integration]]
- [[./Builder-Remote-Images|Builder & Remote Images]]
- Kanban `t_971a642f` — original local build PREREQ
- Kanban `t_532f0b86` — upstream v10.0.1 sync (fork rebase)
- Kanban `t_0928eb33` — build verification and balena.yml pin
- Kanban `t_643b57f8` — fork rebase card (this page is the resolution)