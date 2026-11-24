# Control-field audit: `stint.audit`

Out-of-band write detection for screen-locked fields. The restricted-Edit-screen
pattern locks fields in the UI only (JRACLOUD-98054); this module derives the
declared control fields and reports recent changelog writes by identities
outside an allow-list.

See the [Locking guide](../guides/locking.md) for the threat model.

::: stint.audit
