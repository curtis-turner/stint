"""`pensum revision` CLI: empty stub + merge stub."""

from pensum import load_migrations
from pensum.cli.main import main


def _write_initial(mig_dir, rev: str = "initial1234") -> None:
    """Seed a migration dir with one base migration."""
    mig_dir.mkdir(parents=True, exist_ok=True)
    (mig_dir / f"2026_05_20_1200_{rev}.py").write_text(
        f"revision = {rev!r}\ndown_revision = None\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )


# ── Empty skeleton ────────────────────────────────────────────────────
def test_revision_writes_skeleton_with_current_head_as_parent(tmp_path, capsys):
    mig_dir = tmp_path / "migrations"
    _write_initial(mig_dir, "initial1234")
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "-m",
            "add bug severity",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "wrote " in out
    assert "revision=" in out
    # New file present, loads cleanly, parent = "initial1234"
    graph = load_migrations(mig_dir)
    revs = list(graph.by_revision)
    assert "initial1234" in revs
    new_rev = next(r for r in revs if r != "initial1234")
    assert graph.by_revision[new_rev].down_revision == "initial1234"


def test_revision_writes_skeleton_with_no_parent_when_empty_dir(tmp_path):
    mig_dir = tmp_path / "migrations"
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "-m",
            "initial schema",
        ]
    )
    assert rc == 0
    graph = load_migrations(mig_dir)
    new = next(iter(graph.by_revision.values()))
    assert new.down_revision is None


def test_revision_message_becomes_filename_slug(tmp_path):
    mig_dir = tmp_path / "migrations"
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "-m",
            "Add bug-severity, with options!",
        ]
    )
    assert rc == 0
    files = list(mig_dir.glob("*.py"))
    assert len(files) == 1
    assert "add_bug_severity_with_options" in files[0].name


# ── Merge ─────────────────────────────────────────────────────────────
def test_revision_merge_writes_tuple_down_revision(tmp_path, capsys):
    """With two heads, --merge emits a migration with tuple down_revision."""
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "a.py").write_text(
        "revision = 'a_rev'\ndown_revision = None\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    (mig_dir / "b.py").write_text(
        "revision = 'b_rev'\ndown_revision = None\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "--merge",
            "a_rev",
            "b_rev",
            "-m",
            "merge two branches",
        ]
    )
    assert rc == 0
    graph = load_migrations(mig_dir)
    merge = next(m for m in graph.by_revision.values() if m.down_revision and isinstance(m.down_revision, tuple))
    assert set(merge.down_revision) == {"a_rev", "b_rev"}


def test_revision_merge_rejects_non_head(tmp_path, capsys):
    """Merging a revision that isn't a head fails clearly."""
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "a.py").write_text(
        "revision = 'a_rev'\ndown_revision = None\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    (mig_dir / "child.py").write_text(
        "revision = 'child'\ndown_revision = 'a_rev'\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    (mig_dir / "b.py").write_text(
        "revision = 'b_rev'\ndown_revision = None\nasync def upgrade(): pass\nasync def downgrade(): pass\n"
    )
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "--merge",
            "a_rev",
            "b_rev",  # a_rev has a child, not a head
            "-m",
            "merge",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 2
    assert "not current heads" in out


def test_revision_rejects_autogenerate_with_merge(tmp_path, capsys):
    mig_dir = tmp_path / "migrations"
    _write_initial(mig_dir)
    rc = main(
        [
            "revision",
            "--migrations-dir",
            str(mig_dir),
            "--merge",
            "x",
            "y",
            "--autogenerate",
            "-m",
            "x",
        ]
    )
    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().out
