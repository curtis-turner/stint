# Team-managed (TMP) walkthrough

A runnable walkthrough against a real Jira Cloud tenant, using the
experimental team-managed (TMP) dialect. It uses the schema in
[`vuln_management.py`](vuln_management.py): one project (`VM`), a
`Vulnerability` issue type, a `Severity` select field, a `Root Cause` text
field. No screens/schemes -- team-managed projects don't have them.

This path talks to Atlassian's undocumented internal APIs (see
`tmp_dialect_design.md` at the repo root for the full design record). It is
opt-in, experimental, and separate from the [company-managed
walkthrough](../company_managed/): different dialect (`jira_cloud_tmp`, not
`jira_cloud`), different CLI command (`stint apply`, not `stint
revision`/`stint upgrade`), and no migration files -- every apply reflects
live state and reconciles it against the schema on the spot, closer to
`terraform apply` than to a database migration.

Budget about 10 minutes. You need a Jira Cloud tenant where you are a site
admin, and an API token.

Commands below assume you cloned this repo, so they use `uv run stint`. If you
installed the package (`uv add stint` / `pip install stint`), drop the
`uv run` prefix. They also assume you're running from the repo root.

Expect a `UserWarning` about experimental/unsupported internal APIs the first
time you construct the TMP dialect (i.e. on your first `reflect`/`apply` call
below). That's expected, not an error.

## 0. Install and create the team-managed project

```bash
uv sync
```

Unlike the company-managed walkthrough, stint does not create the project
itself here -- `TmpDialect` only manages a project's fields, work types, and
layouts, not the project resource. In Jira, create a project: **Projects →
Create project → Team-managed**, any template. Note its project key; the
steps below assume `VM`, matching `vuln_management.py`'s `__key__`. Use your
own key throughout if you picked something else.

## 1. Validate the schema (no network)

```bash
uv run stint validate --schema examples/team_managed/vuln_management.py
```

Same command as company-managed -- schema validation doesn't know or care
which dialect will apply it.

## 2. Configure the connection

```bash
mkdir -p .stint
cp examples/team_managed/vm-devel.env.example.yaml .stint/vm-devel.yaml
```

Edit `.stint/vm-devel.yaml` and set `url` to your tenant, keeping the
`jira_cloud_tmp+` prefix (`jira_cloud_tmp+https://YOURSITE.atlassian.net`).
Reuse the same secrets as the company-managed walkthrough if you already
exported them:

```bash
export STINT_USER='you@example.com'
export STINT_TOKEN='...'
```

## 3. Reflect the project (read-only)

```bash
uv run stint reflect --dialect jira_cloud_tmp --project-key VM \
    --url "jira_cloud_tmp+https://YOURSITE.atlassian.net" --auth api-token
```

Prints the project's current fields, work types, and layouts as YAML. On a
fresh project this is close to empty -- that's what `apply` is about to fill in.

## 4. Preview the plan (dry run)

```bash
uv run stint apply \
    --schema examples/team_managed/vuln_management.py \
    --project-key VM \
    --state examples/team_managed/vm-devel.state \
    --env vm-devel \
    --dry-run
```

Prints a terraform-style plan (`N to add, M to change, K to destroy`) and
stops. No prompt, no writes.

## 5. Apply it

```bash
uv run stint apply \
    --schema examples/team_managed/vuln_management.py \
    --project-key VM \
    --state examples/team_managed/vm-devel.state \
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

## 6. The automation guard

The schema also declares a `ControlFieldGuard` bound to the project with
`__automation_rule_scheme__` — an Automation rule that fires when `Severity`
changes and puts the previous value back if the writer is not in
`allowed_writers`. Team-managed projects have no field-level lock at all
(anyone who can edit an issue can edit every field, UI and API alike), so
the guard is the enforcement, not a cosmetic screen.

The same `apply` command from step 5 reconciles the rule: the plan shows
`+ automation_rule (create)` the first time and nothing on re-runs (the
rule diffs idempotent against what Jira echoes). Before applying, put the
account you authenticate with into the guard's `allowed_writers` (replace
`svc-automation@example.com` with your `STINT_USER` email) — stint refuses
to apply a guard that would revert its own writes.

Two requirements are specific to team-managed projects, and `validate`
enforces both: the guard declares `fields=[...]` explicitly (there are no
screens to derive control fields from), and the allow-list uses users and
groups only (team-managed projects have no classic project roles).

To see the guard work: change `Severity` on any issue as a user *not* in
`allowed_writers` — the value snaps back in seconds and the issue gains a
comment ("Reverted an edit to the protected field(s)…"). Your own edits are
left alone. The rule-management API needs an administrator-level account;
if reflect's rule listing comes back 403, the guard cannot be managed with
that token.

## 7. Inspect state

```bash
cat examples/team_managed/vm-devel.state
```

There's no `stint current`/`stint history` equivalent here -- there's no
revision graph to be "at revision X of" (see the design-record link above for
why). The state file's `tmp_projects.VM` block is the only persisted record:
field/work-type/layout ids, keyed by alias. The automation rule's
alias→uuid mapping lives at the state file's root (`automation_rules:`) —
the rules are tenant objects, not project-scoped ones.

## 8. Change the schema and reconcile

Edit `vuln_management.py` (e.g. add another `CustomField`, or change
`Severity`'s options), then rerun step 5's `apply` command. The plan will show
only what drifted -- `apply` always diffs live state against the schema as it
is right now, not against a prior run.

## Reset the playground

`examples/team_managed/*.state` is gitignored scratch:

```bash
rm -f examples/team_managed/vm-devel.state
```

This does not undo changes in Jira -- TMP writes have no downgrade. Remove
fields/work types in the Jira UI, or edit the schema back and `apply` again
with `--allow-delete` (removes anything no longer declared; still no undo).

## Troubleshooting

- **`missing required connection params`** — `url`/`auth` are unset. Check
  `.stint/vm-devel.yaml` and that you passed `--env vm-devel`.
- **`stint apply` says `Apply cancelled.`** — you didn't type exactly `yes`
  at the confirmation prompt. Nothing was written; rerun and confirm, or pass
  `--auto-approve`.
- **`no Project with __key__ 'VM' found in schema`** — the project key passed
  to `--project-key` doesn't match any `Project.__key__` in the schema
  module. Check for typos, or that you edited `__key__` consistently if you
  renamed the example project.
- **`the authenticated account (...) is not among the guard's allowed
  writers`** — the `ControlFieldGuard` would revert the account stint
  authenticates as. Add your `STINT_USER` email to `allowed_writers` (or
  pass `--allow-lockout` to force it, and enjoy the revert loop).
- **`NOTE: automation_rule ... state references uuid ... but the rule is
  not present in Jira`** — the rule was deleted in the Jira UI (or by
  another tool). Delete the alias from the state file's `automation_rules`
  block and re-run apply to recreate it.
