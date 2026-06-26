# CI/CD workflows

## `ci.yml`

Runs on every push to `main` and every pull request:

- `ruff format --check .`
- `ruff check .`
- `pytest` across Python 3.10–3.13

## `publish.yml`

Runs when a GitHub **release** is published. Pipeline:

1. `build` — `uv build` produces the sdist + wheel, uploaded as an artifact.
2. `testpypi` — downloads the artifact and publishes to TestPyPI.
3. `pypi` — runs only after `testpypi` succeeds, publishes the same artifacts
   to PyPI prod.

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

1. Bump `version` in `pyproject.toml` and update `CHANGELOG.md`.
2. Tag and publish a GitHub release for that version.
3. The pipeline builds, publishes to TestPyPI, then (after approval, if
   configured) to PyPI.
