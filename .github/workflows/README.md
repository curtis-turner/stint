# CI/CD workflows

## `ci.yml`

Runs on every push to `main` and every pull request:

- `ruff format --check .`
- `ruff check .`
- `pytest` across Python 3.10–3.13

## `publish.yml`

Runs when a GitHub **release** is published. Builds once, then routes by
channel:

1. `classify` — decides the channel. A release is a **pre-release** when its
   tag carries a PEP 440 pre-release/dev marker (`a1`, `b2`, `rc1`, `.dev3`)
   **or** the GitHub "Set as a pre-release" checkbox is ticked.
2. `build` — `uv build` produces the sdist + wheel, uploaded as an artifact.
3. `testpypi` — runs **only for pre-releases**. A dry run to validate the
   publish path before cutting a prod release.
4. `pypi` — runs **only for final releases**. Publishes to PyPI prod.

`testpypi` and `pypi` are mutually exclusive: a single release goes to exactly
one index. Validate on TestPyPI with an alpha tag, then cut a final tag for
prod.

Both uploads use OIDC **trusted publishing** (`uv publish --trusted-publishing
always`). No API tokens are stored anywhere.

`--check-url` makes each upload skip files already present on the index, so a
re-run on an existing version is a no-op instead of a hard failure.

## One-time setup (required before the first release)

The workflow cannot configure these itself.

### 1. Register the trusted publisher on each index

On **TestPyPI** (https://test.pypi.org/manage/account/publishing/) and again on
**PyPI** (https://pypi.org/manage/account/publishing/), add a GitHub
trusted publisher with:

| Field            | Value         |
| ---------------- | ------------- |
| PyPI project     | `stint`       |
| Owner            | `curtis-turner` |
| Repository       | `stint`       |
| Workflow name    | `publish.yml` |
| Environment      | `testpypi` (on TestPyPI) / `pypi` (on PyPI) |

If the project does not exist on the index yet, register it as a *pending*
publisher — the first successful upload creates it.

### 2. Create the GitHub environments

In **Settings → Environments**, create `testpypi` and `pypi`. The environment
names must match the values registered above and in `publish.yml`.

Recommended: add a **required reviewer** protection rule to the `pypi`
environment. Prod then waits for a manual approval after TestPyPI succeeds,
giving you a chance to verify the TestPyPI upload first.

## Cutting a release

**Dry run on TestPyPI** (pre-release):

1. Bump `version` to a pre-release in `pyproject.toml` (e.g. `0.2.0a1`).
2. Publish a GitHub release for that tag with **"Set as a pre-release"**
   ticked (the PEP 440 marker alone also triggers it).
3. The pipeline builds and publishes to TestPyPI only.

**Prod release** (final):

1. Bump `version` to a final version (e.g. `0.2.0`) and update `CHANGELOG.md`.
2. Publish a GitHub release for that tag as a normal (non-pre-release) release.
3. The pipeline builds and publishes to PyPI prod (after approval, if the
   `pypi` environment has a required-reviewer rule).
