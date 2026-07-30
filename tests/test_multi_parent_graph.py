"""Multi-parent (merge) migration graph."""

import pytest

from stint import load_migrations
from stint.migrations.exceptions import MigrationGraphError


# ── Tuple down_revision loads ────────────────────────────────────────
def test_loader_accepts_tuple_down_revision(tmp_path):
    """A merge migration's tuple down_revision loads and validates."""
    (tmp_path / "a.py").write_text(
        "revision = 'a'\ndown_revision = None\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    (tmp_path / "b.py").write_text(
        "revision = 'b'\ndown_revision = None\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    (tmp_path / "m.py").write_text(
        "revision = 'm'\ndown_revision = ('a', 'b')\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    graph = load_migrations(tmp_path)
    assert graph.by_revision["m"].down_revision == ("a", "b")
    assert graph.by_revision["m"].parents() == ("a", "b")


def test_loader_rejects_bogus_down_revision_type(tmp_path):
    (tmp_path / "a.py").write_text(
        "revision = 'a'\ndown_revision = 42\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    with pytest.raises(Exception, match="down_revision must be"):
        load_migrations(tmp_path)


# ── chain_from with a merge ──────────────────────────────────────────
def test_chain_from_includes_both_branches_before_merge(tmp_path):
    """With base→a, base→b, merge(a,b)→m: from base, chain is [a, b, m] (or [b, a, m])."""
    (tmp_path / "base.py").write_text(
        "revision = 'base'\ndown_revision = None\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    (tmp_path / "a.py").write_text(
        "revision = 'a'\ndown_revision = 'base'\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    (tmp_path / "b.py").write_text(
        "revision = 'b'\ndown_revision = 'base'\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    (tmp_path / "m.py").write_text(
        "revision = 'm'\ndown_revision = ('a', 'b')\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    graph = load_migrations(tmp_path)
    # Single head after merge:
    assert [h.revision for h in graph.heads()] == ["m"]

    chain = graph.chain_from("base")
    revs = [m.revision for m in chain]
    # Both branch migrations must come before the merge:
    assert set(revs) == {"a", "b", "m"}
    assert revs[-1] == "m"
    # a and b each must precede m:
    assert revs.index("a") < revs.index("m")
    assert revs.index("b") < revs.index("m")


def test_chain_from_intermediate_branch_includes_other_branch_and_merge(tmp_path):
    """If we're at `a`, the remaining chain is [b, m] — we still need b's work
    applied before the merge can run."""
    (tmp_path / "base.py").write_text(
        "revision = 'base'\ndown_revision = None\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    (tmp_path / "a.py").write_text(
        "revision = 'a'\ndown_revision = 'base'\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    (tmp_path / "b.py").write_text(
        "revision = 'b'\ndown_revision = 'base'\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    (tmp_path / "m.py").write_text(
        "revision = 'm'\ndown_revision = ('a', 'b')\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    graph = load_migrations(tmp_path)
    chain = graph.chain_from("a")
    revs = [m.revision for m in chain]
    assert set(revs) == {"b", "m"}
    assert revs[-1] == "m"


def test_graph_with_orphan_parent_in_tuple_rejected(tmp_path):
    (tmp_path / "m.py").write_text(
        "revision = 'm'\ndown_revision = ('a', 'b')\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    with pytest.raises(MigrationGraphError):
        load_migrations(tmp_path)
