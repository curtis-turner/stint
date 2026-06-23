"""Initial: create the bug severity select field."""

from stint import op
from stint.fields import SelectField

revision = "abc123def456"
down_revision = None


async def upgrade():
    await op.create_custom_field(
        alias="bug_severity",
        name="Severity",
        type=SelectField,
        description="Bug severity classification",
        options=["S1", "S2", "S3", "S4"],
    )


async def downgrade():
    op.unsupported(
        "deleting bug_severity destroys all severity data on existing issues; "
        "remove this guard if you really want to roll back"
    )
