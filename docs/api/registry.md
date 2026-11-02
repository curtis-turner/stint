# Registry: `stint.registry`

Tracks declared schema objects by alias and by Python class. Duplicates from a *different* module raise `RegistryError`; re-executing a module replaces its own objects. `reset()` is provided for test isolation.

::: stint.registry
