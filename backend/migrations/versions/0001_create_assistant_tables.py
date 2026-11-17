"""Create the application-owned ticket and submission tables.

Revision ID: 0001
Revises:
Create Date: 2026-11-17

Only creates objects named "assistant_*". It never renames, alters or drops
any other table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

CONTENT_COLUMNS = (
    "subject", "description", "resolution_notes", "category", "tags", "created_at", "status", "product",
    "error_detail", "investigation", "root_cause", "verification", "resolved_by", "resolved_at",
)


def upgrade() -> None:
    op.create_table(
        "assistant_tickets",
        sa.Column("ticket_id", sa.String(64), primary_key=True),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("resolution_notes", sa.Text, nullable=False),
        sa.Column("category", sa.Text),
        sa.Column("tags", postgresql.ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("created_at", sa.Date),
        sa.Column("status", sa.Text, nullable=False, server_default="resolved"),
        sa.Column("product", sa.Text),
        sa.Column("error_detail", sa.Text),
        sa.Column("investigation", sa.Text),
        sa.Column("root_cause", sa.Text),
        sa.Column("verification", sa.Text),
        sa.Column("resolved_by", sa.Text),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("index_status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("index_error", sa.Text),
        sa.Column("indexed_version", sa.Integer),
        sa.CheckConstraint(r"ticket_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'", name="assistant_tickets_id_format"),
        sa.CheckConstraint("index_status IN ('pending', 'indexed', 'failed')", name="assistant_tickets_index_status"),
    )
    op.create_index(
        "assistant_tickets_unindexed",
        "assistant_tickets",
        ["index_status"],
        postgresql_where=sa.text("index_status <> 'indexed'"),
    )
    op.create_index("assistant_tickets_status", "assistant_tickets", [sa.text("lower(status)")])
    op.create_table(
        "assistant_submissions",
        sa.Column("submission_id", sa.String(64), primary_key=True),
        sa.Column("ticket_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    changed = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in CONTENT_COLUMNS)
    # Any content change, by the app or by someone editing the table directly,
    # gets a new version and is queued for re-indexing.
    op.execute(
        f"""
        CREATE FUNCTION assistant_tickets_touch() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF {changed} THEN
                NEW.version := OLD.version + 1;
                NEW.updated_at := now();
                NEW.index_status := 'pending';
                NEW.index_error := NULL;
            END IF;
            RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER assistant_tickets_touch BEFORE UPDATE ON assistant_tickets "
        "FOR EACH ROW EXECUTE FUNCTION assistant_tickets_touch();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS assistant_tickets_touch ON assistant_tickets;")
    op.execute("DROP FUNCTION IF EXISTS assistant_tickets_touch();")
    op.drop_table("assistant_submissions")
    op.drop_index("assistant_tickets_status", table_name="assistant_tickets")
    op.drop_index("assistant_tickets_unindexed", table_name="assistant_tickets")
    op.drop_table("assistant_tickets")
