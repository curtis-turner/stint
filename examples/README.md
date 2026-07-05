# Try stint end to end

Two runnable walkthroughs against a real Jira Cloud tenant:

- **Company-managed (CMP)**: [`platform.py`](platform.py) — the fully-supported,
  migration-based path. One project (`PLAT`), a `Bug` issue type, a `Severity`
  select field, screens, and the schemes that bind them.
- **Team-managed (TMP)**: [`vuln_management.py`](vuln_management.py) — the
  experimental, opt-in path for team-managed ("next-gen") projects, which use
  a different write model and a different set of CLI commands. One project
  (`VM`), a `Vulnerability` issue type, a `Severity` select field, a
  `Root Cause` text field. No screens/schemes -- team-managed projects don't
  have them.

Budget about 10 minutes for either. You need a Jira Cloud tenant where you are
a site admin, and an API token.

Commands below assume you cloned this repo, so they use `uv run stint`. If you
installed the package (`uv add stint` / `pip install stint`), drop the
`uv run` prefix.

## Install

```bash
uv sync
```

---

## Company-managed (CMP) walkthrough

### 1. Validate the schema (no network)

```bash
uv run stint validate --schema examples/platform.py
```

Expect `OK: examples/platform.py is valid.` This runs every schema-level check
without touching Jira.

### 2. Point the example lead at a real user

`platform.py` sets `__lead__ = "cturner@example.com"`. stint resolves that email
to a Jira account id at apply time, so it must be a real user on your tenant.
Edit the line to your own account email.

### 3. Configure the connection

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

### 4. Adopt what already exists (brownfield)

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

### 5. Generate a migration

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

### 6. Review the migration

```bash
ls examples/migrations
```

Open the newest file. It is plain Python: an `upgrade()` of `op.*` calls and a
`downgrade()`. Nothing has touched Jira yet.

### 7. Apply it

```bash
uv run stint upgrade --env devel \
    --state examples/devel.state \
    --migrations-dir examples/migrations
```

Each op is idempotent and records its Jira id in `examples/devel.state`. A
failed run is safe to re-run; it resumes where it stopped.

### 8. Inspect

```bash
uv run stint current --state examples/devel.state
uv run stint history --migrations-dir examples/migrations
```

`current` prints the applied revision; `history` lists the chain.

### Reset the playground

`examples/migrations/` and `examples/*.state` are gitignored scratch. Delete
them to start over:

```bash
rm -rf examples/migrations examples/devel.state
```

This does not undo changes in Jira. To roll those back, downgrade to base
before deleting, or remove the objects in the Jira UI:

```bash
uv run stint downgrade --env devel \
    --state examples/devel.state \
    --migrations-dir examples/migrations \
    --revision base
```

---

## Team-managed (TMP) walkthrough

This path talks to Atlassian's undocumented internal APIs (see
`tmp_dialect_design.md` at the repo root for the full design record). It is
opt-in, experimental, and separate from the CMP path above: different dialect
(`jira_cloud_tmp`, not `jira_cloud`), different CLI commands (`stint apply`,
not `stint revision`/`stint upgrade`), and no migration files -- every apply
reflects live state and reconciles it against the schema on the spot, closer
to `terraform apply` than to a database migration.

Expect a `UserWarning` about experimental/unsupported internal APIs the first
time you construct the TMP dialect (i.e. on your first `reflect`/`apply` call
below). That's expected, not an error.

### 0. Create the team-managed project

Unlike the CMP walkthrough, stint does not create the project itself here --
`TmpDialect` only manages a project's fields, work types, and layouts, not the
project resource. In Jira, create a project: **Projects → Create project →
Team-managed**, any template. Note its project key; the steps below assume
`VM`, matching `vuln_management.py`'s `__key__`. Use your own key throughout
if you picked something else.

### 1. Validate the schema (no network)

```bash
uv run stint validate --schema examples/vuln_management.py
```

Same command as CMP -- schema validation doesn't know or care which dialect
will apply it.

### 2. Configure the connection

```bash
mkdir -p .stint
cp examples/vm-devel.env.example.yaml .stint/vm-devel.yaml
```

Edit `.stint/vm-devel.yaml` and set `url` to your tenant, keeping the
`jira_cloud_tmp+` prefix (`jira_cloud_tmp+https://YOURSITE.atlassian.net`).
Reuse the same secrets as the CMP walkthrough if you already exported them:

```bash
export STINT_USER='you@example.com'
export STINT_TOKEN='...'
```

### 3. Reflect the project (read-only)

```bash
uv run stint reflect --dialect jira_cloud_tmp --project-key VM \
    --url "jira_cloud_tmp+https://YOURSITE.atlassian.net" --auth api-token
```

Prints the project's current fields, work types, and layouts as YAML. On a
fresh project this is close to empty -- that's what `apply` is about to fill in.

### 4. Preview the plan (dry run)

```bash
uv run stint apply \
    --schema examples/vuln_management.py \
    --project-key VM \
    --state examples/vm-devel.state \
    --env vm-devel \
    --dry-run
```

Prints a terraform-style plan (`N to add, M to change, K to destroy`) and
stops. No prompt, no writes.

### 5. Apply it

```bash
uv run stint apply \
    --schema examples/vuln_management.py \
    --project-key VM \
    --state examples/vm-devel.state \
    --env vm-devel
```

Prints the same plan, then prompts:

```
Do you want to perform these actions?
  stint will make the changes described above.
  Only 'yes' will be accepted to approve.

Enter a value:
```

Type `yes` to proceed. Pass `--auto-approve` instead of confirming
interactively (for scripts/CI).

### 6. Inspect state

```bash
cat examples/vm-devel.state
```

There's no `stint current`/`stint history` equivalent here -- there's no
revision graph to be "at revision X of" (see the design-record link above for
why). The state file's `tmp_projects.VM` block is the only persisted record:
field/work-type/layout ids, keyed by alias.

### 7. Change the schema and reconcile

Edit `vuln_management.py` (e.g. add another `CustomField`, or change
`Severity`'s options), then rerun step 5's `apply` command. The plan will show
only what drifted -- `apply` always diffs live state against the schema as it
is right now, not against a prior run.

### Reset the playground

`examples/*.state` is gitignored scratch:

```bash
rm -f examples/vm-devel.state
```

This does not undo changes in Jira -- TMP writes have no downgrade. Remove
fields/work types in the Jira UI, or edit the schema back and `apply` again
with `--allow-delete` (removes anything no longer declared; still no undo).

---

## Troubleshooting

- **`missing required connection params`** — `url`/`auth` are unset. Check
  `.stint/<env>.yaml` and that you passed the matching `--env`.
- **Lead resolution fails with a permission error** (CMP) — the token lacks
  "Browse users and groups". Grant it, or set `__lead__` to a raw account id.
- **`N pending migration(s) not yet applied`** (CMP) — you ran autogenerate
  twice without applying. Run `upgrade` first, or pass `--force` to stack anyway.
- **A `create_*` op fails mid-migration** (CMP) — Jira admin REST has no
  transactions. Fix the cause and re-run `upgrade`; completed ops are skipped.
- **`stint apply` says `Apply cancelled.`** (TMP) — you didn't type exactly
  `yes` at the confirmation prompt. Nothing was written; rerun and confirm, or
  pass `--auto-approve`.
- **`no Project with __key__ 'VM' found in schema`** (TMP) — the project key
  passed to `--project-key` doesn't match any `Project.__key__` in the schema
  module. Check for typos, or that you edited `__key__` consistently if you
  renamed the example project.
