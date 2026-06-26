---
title: "Show HN submission"
show_hn_title: "Show HN: stint - schema-as-code and migrations for Jira admin"
submission_url: "https://github.com/curtis-turner/stint"
---

# Show HN submission

## Title

```
Show HN: stint - schema-as-code and migrations for Jira admin
```

## URL

Point the submission at the repo so readers see the code and can try
`uv add stint`. Link the write-up from the first comment.

```
https://github.com/curtis-turner/stint
```

## First comment

Changing a Jira project today means clicking through the admin web UI. No diff, no review, no record of who changed what or why. Staging and production drift apart and nobody can reconstruct how. We stopped managing application databases this way two decades ago. Jira config never got the same treatment, even though the blast radius is just as large.

stint treats Jira admin like a schema you keep in your repo. You declare projects, issue types, custom fields, screens, and schemes as Pydantic classes. You autogenerate a migration from the diff between that declaration and the live instance, review the emitted Python, commit it, and apply the same chain across environments. The mental model is SQLAlchemy plus Alembic, pointed at Jira instead of a database.

```python
async def upgrade():
    await op.create_custom_field(
        alias="bug_severity",
        name="Severity",
        type=SelectField,
        options=["S1", "S2", "S3", "S4"],
    )
```

The same model classes double as a typed query layer for reads and writes. It compiles to JQL, not SQL, so think of it as a query builder over Jira search rather than a full relational ORM. The point is that a custom field is `cf[10042]` in raw JQL, and that id differs in every environment. `Bug.c.severity == "S1"` resolves the right id from the same state file the migrations use, so one expression runs against dev and prod with no hardcoded ids.

Jira is the first backend, not the only one. The migration and query layers route through a dialect, so other work-management tools can drop in without changing your schemas or queries. Linear is the candidate I'm eyeing next. Linear already has a Terraform provider, so the bet there is not "first to exist" but a different model: an ordered migration history instead of state reconciliation, plus a typed ORM over your issues that Terraform does not give you. Whether that trade is worth it to Linear users is exactly what I want to find out.

Status is alpha (0.1.0a0). Honest about the edges:

- Jira Cloud only. Company-managed and team-managed projects.
- No transactions. Jira admin REST has none, so a failed migration can leave a partial state. Re-running picks up where it left off because each op checks what already exists first.
- Workflows, permission schemes, and notification schemes are not in 0.1.
- Writes wrap plain text into ADF correctly, but reads do not parse ADF yet.
- Drift still happens. If someone edits the UI, stint won't stop them. It gives you a way to reflect the instance, see the diff, and pull the change back into code.

I built this because I have reconciled two Jira instances by hand more than once and wanted the change to be a commit my teammate could review instead of a memory of what I clicked.

Feedback I'd most value: whether the migration model holds up against how your org actually mutates Jira, and where the typed query layer falls short of what you query today.

If you have managed Jira config by hand across environments, I want to hear how you do it today and what would make a tool like this worth adopting. And if you are on Linear or another work-management tool and would want the same schema-as-code workflow, say so. That demand shapes which backend comes next.

Repo: https://github.com/curtis-turner/stint
Write-up: https://github.com/curtis-turner/stint/blob/main/blog_why_stint.md

---

## Appetite threshold (decide before posting, do not edit after)

Window: 7 days from the first post, measured across HN, r/jira, the
Atlassian community, and r/Python combined. Weight the can't-leave-Jira
venues (r/jira, Atlassian) higher than HN, which skews toward people who
resent Jira and is venue-biased against this idea.

Vanity metrics do not count. Upvotes and stars are noise.

GO (invest further, build a real roadmap) if any one of these holds:

- 5+ unprompted comments from people describing this exact pain, e.g. "we
  document our Jira config by hand," "our envs have drifted and nobody
  knows how."
- 2+ people actually run it against a live instance (issues filed, or
  questions that only make sense if they ran `stint reflect`/`validate`).
- 1+ person asking, with real intent, how to adopt it for their org or
  when Linear lands.

NO-GO (shelve, or rethink the premise) if the response is:

- Mostly "why not just use the UI / Terraform / a script," or
- polite interest with zero pain descriptions and zero trials.

AMBIGUOUS (moderate curiosity, no trials): do not decide on HN alone.
Post to r/jira and the Atlassian community first, because those are the
people with the actual pain, then read the combined signal.

Founder-bias check: "I would have loved this for years" is not a data
point. It says the pain is real for one experienced engineer. It says
nothing about how many others would adopt a tool this large.
