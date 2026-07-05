# Try stint end to end

Two runnable walkthroughs against a real Jira Cloud tenant, one per dialect:

- **[`company_managed/`](company_managed/)** — the fully-supported,
  migration-based path (`stint revision`/`stint upgrade`). One project
  (`PLAT`), a `Bug` issue type, a `Severity` select field, screens, and the
  schemes that bind them.
- **[`team_managed/`](team_managed/)** — the experimental, opt-in path for
  team-managed ("next-gen") projects, which use a different write model and
  a different CLI command (`stint apply`). One project (`VM`), a
  `Vulnerability` issue type, a `Severity` select field, a `Root Cause` text
  field. No screens/schemes -- team-managed projects don't have them.

Each has its own README with the full walkthrough, step by step. Budget
about 10 minutes for either. You need a Jira Cloud tenant where you are a
site admin, and an API token.
