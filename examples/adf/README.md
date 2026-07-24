# ADF Example

This example demonstrates how to work with the Atlassian
Document Format (ADF) in *stint*.

1. **Define a minimal issue type** – the only system fields we
   care about are `summary` and `description`.
2. **Build a payload** – the library automatically wraps the
   description text in an ADF document.
3. **Show the generated JSON** – useful for debugging or for
   feeding into the Jira API.
4. **Parse an ADF payload** – simulate a read from Jira and
   hydrate back into a Pydantic model.

Run it with:

```bash
uv run python -m examples.adf.example
```

The script prints the insert payload and the hydrated object.
