# CLI reference

`stint` exposes a single top-level command built on Cyclopts. Every command accepts the shared connection flags; `-h` on any shows full detail.

```console
$ stint --help
```

## Commands

| Command | What it does |
|---|---|
| `stint validate` | Run schema-level checks on a Python schema module. **No network calls.** |
| `stint stamp` | Brownfield: populate the state file by matching schema declarations to *existing* Jira objects (read-only, no writes). |
| `stint reflect` | Reflect the tenant into a snapshot (`ServerInfoSnapshot`-shaped) and print it as YAML (default) or JSON. Useful for the [autogen](migrations.md) diff to work with. |
| `stint revision` | Create a new migration file: empty skeleton, `--merge` to join heads, or `--autogenerate` to diff the schema against a live environment. |
| `stint upgrade` | Apply pending migrations to an environment, in revision order. `--to <revision>` stops early. |
| `stint downgrade` | Roll back to a prior revision (`--revision base` = unwind everything). |
| `stint current` | Print the revision recorded in the state file. |
| `stint history` | List all migrations in revision order. |
| `stint apply` ⚠️ | Team-managed only (`jira_cloud_tmp` dialect): reflect the project, diff it against schema, show a terraform-style plan, confirm, apply. Flags: `--dry-run`, `--auto-approve`, `--allow-delete`. |

## Shared flags

| Flag | Meaning |
|---|---|
| `--schema` | Schema module (dotted) or file path. |
| `--state` | Path to the state YAML (default `state/dev.yaml` per repo convention). Committed; contains no secrets. |
| `--env` | Environment name — resolves connection details from `~/.stint/envs/<env>.yaml`. |
| `--url` | Tenant URL, e.g. `jira_cloud+https://you.atlassian.net`. |
| `--auth` | `api-token`, `pat`, or `basic`. |
| `--token-env` / `--user-env` | Env-var names holding the token and username (default `STINT_TOKEN` / `STINT_USER`). |
| `--dialect` | Dialect override for the URL-less case. |
| `--no-verify-ssl` | Skip TLS verification (self-signed dev proxies). |

## Environment files

So each developer and CI pipeline can point at its own tenant with one word, connection settings live in `~/.stint/envs/<env>.yaml`:

```yaml
# ~/.stint/envs/dev.yaml
url: jira_cloud+https://dev.atlassian.net
auth: api-token
# tokens still come from the environment, never from here
user_env: STINT_USER
token_env: STINT_TOKEN
```

Domain errors (`StintError` and subclasses) print as one clean line on stderr; unexpected bugs show a full traceback.

## Typical loop

```console
# development
$ export STINT_USER='you@example.com' STINT_TOKEN='...'
$ stint validate --schema schemas/platform.py
$ stint revision --autogenerate --schema schemas.platform --env dev -m "add severity"
$ stint upgrade --env dev

# CI (single writer per tenant)
$ stint upgrade --env prod
```
