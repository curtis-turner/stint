# This file marks the `examples.adf` package and re‑exports the public objects
# used by the integration example.  Importing the package will load the
# schema module so that `stint` can discover the Project and IssueType
# classes during stamp/apply.

from .schema import ADFProject, Foo

__all__ = ["Foo", "ADFProject"]
