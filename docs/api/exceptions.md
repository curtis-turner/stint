# Exceptions: `stint.exceptions`

All library failures subclass `StintError`, so user-level handling is one `except StintError` away. Domain errors are printed as one clean line by the CLI; `PartialCommitError` carries per-operation `results` on the data plane.

::: stint.exceptions
