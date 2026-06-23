"""Second migration: add a text-style custom field."""

from stint import op
from stint.fields import TextField

revision = "def789ghi012"
down_revision = "abc123def456"


async def upgrade():
    await op.create_custom_field(
        alias="bug_root_cause",
        name="Root Cause",
        type=TextField,
        description="Free-text root-cause analysis",
    )


async def downgrade():
    await op.delete_custom_field(alias="bug_root_cause")
