# Try stint end to end

A runnable walkthrough against a real Jira Cloud tenant. It uses the schema in
[`platform.py`](platform.py): one project (`PLAT`), a `Bug` issue type, a
`Severity` select field, screens, and the schemes that bind them.

Budget about 10 minutes. You need a Jira Cloud tenant where you are a site
admin, and an API token.

Commands below assume you cloned this repo, so they use `uv run stint`. If you
installed the package (`pip install stint`), drop the `uv run` prefix.

## 1. Install

```bash
uv sync
```

## 2. Validate the schema (no network)

```bash
uv run stint validate --schema examples/platform.py
```

Expect `OK: examples/platform.py is valid.` This runs every schema-level check
without touching Jira.

## 3. Point the example lead at a real user

`platform.py` sets `__lead__ = "cturner@example.com"`. stint resolves that email
to a Jira account id at apply time, so it must be a real user on your tenant.
Edit the line to your own account email.

## 4. Configure the connection

```bash
mkdir -p .stint
cp examples/devel.env.example.yaml .stint/devel.yaml
```

Edit `.stint/devel.yaml` and set `url` to your tenant
(`jira_cloud+https://YOURSITE.atlassian.net`). Then export the two secrets:

```bash
export STINT_USER='you@example.com'   # your Jira Cloud account email
export STINT_TOKEN='...'              # https://id.atlassian.com/manage-profile/security/api-tokens
```

`.stint/` is gitignored, and the token never lands in a file.

## 5. Adopt what already exists (brownfield)

Every Jira tenant ships built-in objects. `stamp` matches your schema against
the live tenant by name and records the matches in state, so the next step does
not try to recreate them:

```bash
uv run stint stamp \
    --schema examples/platform.py \
    --state examples/devel.state \
    --env devel
```

Read the `matched` / `unmatched` lines. Unmatched objects are the ones stint
will create.

Starting from a truly empty tenant? Skip this step. The built-in `Bug` issue
type is adopted automatically on apply, so a greenfield run still works.

## 6. Generate a migration

```bash
uv run stint revision --autogenerate \
    --schema examples/platform.py \
    --state examples/devel.state \
    --migrations-dir examples/migrations \
    --env devel \
    -m "initial platform schema"
```

stint reflects the tenant, diffs it against the schema, and writes one migration
file with the ops needed to converge. If everything already matches, it prints
`no changes detected` and writes nothing.

## 7. Review the migration

```bash
ls examples/migrations
```

Open the newest file. It is plain Python: an `upgrade()` of `op.*` calls and a
`downgrade()`. Nothing has touched Jira yet.

## 8. Apply it

```bash
uv run stint upgrade --env devel \
    --state examples/devel.state \
    --migrations-dir examples/migrations
```

Each op is idempotent and records its Jira id in `examples/devel.state`. A
failed run is safe to re-run; it resumes where it stopped.

## 9. Inspect

```bash
uv run stint current --state examples/devel.state
uv run stint history --migrations-dir examples/migrations
```

`current` prints the applied revision; `history` lists the chain.

## Reset the playground

`examples/migrations/` and `examples/*.state` are gitignored scratch. Delete
them to start over:

```bash
rm -rf examples/migrations examples/*.state
```

This does not undo changes in Jira. To roll those back, downgrade to base
before deleting, or remove the objects in the Jira UI:

```bash
uv run stint downgrade --env devel \
    --state examples/devel.state \
    --migrations-dir examples/migrations \
    --revision base
```

## Troubleshooting

- **`missing required connection params`** — `url`/`auth` are unset. Check
  `.stint/devel.yaml` and that you passed `--env devel`.
- **Lead resolution fails with a permission error** — the token lacks "Browse
  users and groups". Grant it, or set `__lead__` to a raw account id.
- **`N pending migration(s) not yet applied`** — you ran autogenerate twice
  without applying. Run `upgrade` first, or pass `--force` to stack anyway.
- **A `create_*` op fails mid-migration** — Jira admin REST has no
  transactions. Fix the cause and re-run `upgrade`; completed ops are skipped.
