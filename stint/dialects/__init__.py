"""Dialect registry. Per-backend protocol implementations live below this.

The dialect protocols are the only extension point a new backend must satisfy.
"""

from stint.dialects.base import BaseDialect, CmpDialect

__all__ = ["BaseDialect", "CmpDialect"]
