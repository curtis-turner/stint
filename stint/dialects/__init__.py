"""Dialect registry. Per-backend protocol implementations live below this.

The dialect protocol is the only extension point a new backend must satisfy.
"""

from stint.dialects.base import Dialect

__all__ = ["Dialect"]
