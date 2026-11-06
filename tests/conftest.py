"""Shared test configuration.

httpx2/respx interop: respx 0.23 has no native httpx2 support (its mock
targets and isinstance checks are httpx-only), so we route it through the
``pytest-httpx2`` plugin, which registers an ``httpcore2`` mocker with
respx. Importing ``pytest_httpx2`` performs that registration; bumping
``respx.mocks.DEFAULT_MOCKER`` redirects every bare ``@respx.mock``
decorator and ``with respx.mock:`` block — the idioms already used across
the suite — to it. When respx ships native httpx2 support, drop this file's
body and the pytest-httpx2 dev dependency.

Note: at the mock layer these are still plain ``httpx.Response`` /
``httpx.Request`` objects (respx asserts isinstance-Response). Objects do
already flow from httpx2's transport, so the difference is invisible to
callsites — except for exceptions raised in ``side_effect`` callbacks,
which must use ``httpx2.*`` types so stint's retry/error mapping sees them.
"""

import pytest_httpx2  # noqa: F401  (registers the httpcore2 mocker)
from respx import mocks as respx_mocks

respx_mocks.DEFAULT_MOCKER = "httpcore2"
