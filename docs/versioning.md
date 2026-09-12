# Version Management

## VERSION files

| File | Controls |
|------|----------|
| `ansible/guest/VERSION` | VM / guest image release line |
| `src/sek8s/VERSION` | `sek8s` Python package version |
| `src/sek8s-common/VERSION` | `sek8s-common` Python package version |
| `src/attestation-proxy/VERSION` | `attestation-proxy` Python package version |
| `src/chutes-cvm/VERSION` | `chutes-cvm` host CLI package version |

Each `src/<pkg>/VERSION` is the **source of truth** for `[tool.poetry] version` in the
corresponding `pyproject.toml`. The sync script (`scripts/sync_pyproject_versions.py`)
keeps them aligned, and CI enforces it.

## Version domains — which changes require which bump

There are several independent release domains (below, plus the CalVer **ops** domain —
`ansible/host/`, `host-tools/`, `.github/workflows/` — versioned via `changelogs/ops/VERSION`).
A single PR may touch more than one; bump each affected VERSION file.

### VM image domain

Changes under any of these paths require an **`ansible/guest/VERSION`** bump:

- `ansible/guest/*` (guest image build; **`ansible/host/`** bare-metal playbooks do **not** require this bump)
- `src/sek8s/*`
- `src/sek8s-common/*`
- `nvevidence/*`
- `pyproject.toml` (root)
- `poetry.lock`

Rationale: all of these are baked into the guest VM image. `sek8s-common` is included
because it is installed on the VM alongside `sek8s`.

### Attestation-proxy domain

Changes under `src/attestation-proxy/*` require an **`src/attestation-proxy/VERSION`**
bump (not `ansible/guest/VERSION`).

Rationale: the proxy runs as a standalone k3s container image, independently releasable
from the VM image.

### chutes-cvm domain

Changes under `src/chutes-cvm/*` require an **`src/chutes-cvm/VERSION`** bump (SemVer).
Changelog fragments live in `changelogs/chutes-cvm/`.

Rationale: `chutes-cvm` is the host-side CLI + toolkit, installed independently on bare-metal
hosts (via `src/chutes-cvm/install.sh` or `pip`) — released on its own cadence, not tied to the
guest VM image. It is not baked into the VM image, so it does not bump `ansible/guest/VERSION`.

### Cross-domain changes

If a PR touches both domains (e.g. `src/sek8s-common/*` and `src/attestation-proxy/*`),
both VERSION files must be bumped.

### `sek8s-common` and proxy consumers

When `sek8s-common` changes, a `ansible/guest/VERSION` bump is required (VM domain),
but an `attestation-proxy/VERSION` bump is **not** automatically required. The proxy
picks up common changes on its next release. If a common change is breaking for the
proxy, bump the proxy version in the same PR.

## Git tags

Tags are created automatically on merge to `main` when the corresponding VERSION file
changes:

| VERSION file | Tag format |
|-------------|------------|
| `ansible/guest/VERSION` | `v{version}` |
| `src/sek8s/VERSION` | `sek8s-v{version}` |
| `src/sek8s-common/VERSION` | `sek8s-common-v{version}` |
| `src/attestation-proxy/VERSION` | `attestation-proxy-v{version}` |

The `v{version}` tag (from `ansible/guest/VERSION`) is the **VM image release tag**.
Per-package tags track Python package versions independently.

## Changelogs

Changelogs use a **fragment-based** system. Each feature branch drops a `.md` file
into `changelogs/<component>/unreleased/`. Promotion aggregates fragments into a
versioned `## [x.y.z]` entry in `CHANGELOG.md` and deletes the fragment files.

| Component | CHANGELOG | Fragments | Paired VERSION file |
|-----------|-----------|-----------|---------------------|
| VM image | `changelogs/vm/CHANGELOG.md` | `changelogs/vm/unreleased/` | `ansible/guest/VERSION` |
| sek8s | `changelogs/sek8s/CHANGELOG.md` | `changelogs/sek8s/unreleased/` | `src/sek8s/VERSION` |
| Proxy | `changelogs/attestation-proxy/CHANGELOG.md` | `changelogs/attestation-proxy/unreleased/` | `src/attestation-proxy/VERSION` |
| Ops | `changelogs/ops/CHANGELOG.md` | `changelogs/ops/unreleased/` | _(none — date-stamped)_ |

`sek8s-common` does not have its own changelog. Common changes are documented in the
consuming package's changelog (`sek8s` or `attestation-proxy`).

### Adding a changelog fragment

On your feature branch, create a `.md` file in **every component's** `unreleased/`
directory that your branch touches. The filename must match the branch name (strip
the prefix): `feature/nvidia-590-drivers` → `nvidia-590-drivers.md`.

If your branch changes files in both `src/sek8s/` and `ansible/guest/`, you need fragments
in both `changelogs/sek8s/unreleased/` and `changelogs/vm/unreleased/`.

Path-to-component mapping:

| Changed path | Requires fragment in |
|-------------|---------------------|
| `src/sek8s/*` | `changelogs/sek8s/unreleased/` |
| `src/sek8s-common/*` | `changelogs/sek8s/unreleased/` |
| `src/attestation-proxy/*` | `changelogs/attestation-proxy/unreleased/` |
| `ansible/guest/*` | `changelogs/vm/unreleased/` |
| `nvevidence/*` | `changelogs/vm/unreleased/` |
| `ansible/host/*`, `host-tools/*`, `.github/workflows/*` | `changelogs/ops/unreleased/` |

Use [Keep a Changelog](https://keepachangelog.com/) category headers:

```markdown
### Added
- New image management API endpoints

### Fixed
- Attestation-proxy restart bug in attestation-system namespace
```

Categories: **Added**, **Changed**, **Fixed**, **Removed**.

CI enforces this on PRs to `release/**` branches — the check maps changed files to
components and verifies the branch-named fragment exists in each one.

### Promotion: how fragments become changelog entries

Promotion is **idempotent** — running it multiple times is safe. If the version heading
already exists, new fragments are merged into the existing section by category.

#### Release branch workflow (`release/**`)

1. Feature branches merge into `release/next` (or similar).
2. On each push to a `release/**` branch, the `changelog-auto-promote.yml` workflow
   runs `promote_changelogs.py --promote`, commits promoted changelogs, and pushes
   directly to the release branch.
3. When the release branch is PR'd to `main`, the strict check enforces that **no
   fragments remain** — everything must already be promoted.
4. On merge to `main`, tags are created for bumped versions.

#### Trunk-based workflow (direct to main)

1. Before creating a PR to `main`, run `make promote-changelogs` locally.
2. The same strict check applies: no fragments allowed on PRs to `main`.

Never manually add `## [x.y.z]` headings to `CHANGELOG.md` — automation owns those.

### Git tags

Tags are created on merge to `main` by the `version-tag.yml` workflow. For components
with changelogs, tags are only created when the version heading exists in `CHANGELOG.md`.
For `sek8s-common` (no changelog), tags are created unconditionally when the VERSION
file changes.

## CI enforcement

The `version-tag.yml` workflow runs on PRs to `main` and `release/**` branches, and
on push to `main`:

1. **Domain version check** (PRs to `main` and push to `main` only) — if files in a
   domain changed, the domain's VERSION must be bumped. Skipped on PRs to release
   branches since VERSION is bumped once per release, not per feature.
2. **Pyproject sync check** — `scripts/sync_pyproject_versions.py --check` verifies
   that every `src/<pkg>/VERSION` matches its `pyproject.toml` version.
3. **Changelog check**:
   - PRs to `release/**`: branch-named fragment required in every affected component
     (maps changed files to components automatically).
   - PRs to `main` / push to `main`: strict mode, fails if any fragments remain in
     any `unreleased/` directory.

## Scripts

```bash
# Sync VERSION -> pyproject.toml (fix mode)
python scripts/sync_pyproject_versions.py

# Sync check (CI)
python scripts/sync_pyproject_versions.py --check

# Promote fragments into CHANGELOG.md (idempotent)
make promote-changelogs
# or: python scripts/promote_changelogs.py --promote

# Verify no orphaned fragments (strict, used before merging to main)
make check-changelogs
# or: python scripts/promote_changelogs.py --check --strict

# Validate branch-named fragments exist for changed components (release branch PRs)
git diff --name-only base..head | python scripts/promote_changelogs.py --check-branch feature/my-branch
```
