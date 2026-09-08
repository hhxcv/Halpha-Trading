"""Persist exact manual scalping cycle identities and template snapshots.

Revision ID: 20260816_0016
Revises: 20260815_0015
"""

from __future__ import annotations

import re

from alembic import op
from sqlalchemy.dialects import postgresql
import sqlalchemy as sa


revision = "20260816_0016"
down_revision = "20260815_0015"
branch_labels = None
depends_on = None


UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
UTC_TS = sa.DateTime(timezone=True)
QUALIFICATION_DATABASE_PATTERN = re.compile(
    r"^halpha_workbench_fixture_[1-9][0-9]*$"
)


def _role_prefix() -> tuple[str, bool]:
    database_name = str(
        op.get_bind().execute(sa.text("SELECT current_database()")).scalar_one()
    ).strip()
    if database_name == "halpha_demo":
        return "halpha_demo", False
    if database_name in {"halpha_live_copy", "halpha_live_personal"}:
        return database_name, True
    if QUALIFICATION_DATABASE_PATTERN.fullmatch(database_name):
        return "halpha_demo", False
    raise RuntimeError(f"UNSUPPORTED_HALPHA_DATABASE: {database_name}")


def upgrade() -> None:
    role_prefix, is_live = _role_prefix()
    op.create_table(
        "scalp_cycle",
        sa.Column("cycle_id", UUID, primary_key=True),
        sa.Column("environment_id", sa.String(96), nullable=False),
        sa.Column("account_ref", sa.String(160), nullable=False),
        sa.Column("instrument_ref", sa.String(96), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("template_snapshot", JSONB, nullable=False),
        sa.Column("template_digest", sa.CHAR(64), nullable=False),
        sa.Column("request_digest", sa.CHAR(64), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("plan_id", UUID, nullable=False),
        sa.Column("plan_version_id", UUID, nullable=False),
        sa.Column("activation_id", UUID, nullable=False),
        sa.Column("triggered_at", UTC_TS, nullable=False),
        sa.ForeignKeyConstraint(
            ("environment_id", "plan_id"),
            (
                "halpha.trade_plan_draft.environment_id",
                "halpha.trade_plan_draft.plan_id",
            ),
            name="fk_scalp_cycle_draft",
        ),
        sa.ForeignKeyConstraint(
            ("environment_id", "plan_version_id"),
            (
                "halpha.trade_plan_version.environment_id",
                "halpha.trade_plan_version.plan_version_id",
            ),
            name="fk_scalp_cycle_version",
        ),
        sa.ForeignKeyConstraint(
            ("environment_id", "activation_id"),
            (
                "halpha.plan_activation.environment_id",
                "halpha.plan_activation.activation_id",
            ),
            name="fk_scalp_cycle_activation",
        ),
        sa.CheckConstraint(
            "direction IN ('LONG','SHORT')",
            name="ck_scalp_cycle_direction",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(template_snapshot) = 'object' AND "
            "template_snapshot ->> 'schema_version' = 'HALPHA_SCALP_TEMPLATE_V1'",
            name="ck_scalp_cycle_template_shape",
        ),
        sa.CheckConstraint(
            "template_digest ~ '^[0-9a-f]{64}$'",
            name="ck_scalp_cycle_template_digest",
        ),
        sa.CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'",
            name="ck_scalp_cycle_request_digest",
        ),
        sa.UniqueConstraint(
            "environment_id", "cycle_id", name="uq_scalp_cycle_environment"
        ),
        sa.UniqueConstraint(
            "environment_id",
            "idempotency_key",
            name="uq_scalp_cycle_idempotency",
        ),
        sa.UniqueConstraint(
            "environment_id",
            "activation_id",
            name="uq_scalp_cycle_activation",
        ),
        schema="halpha",
    )
    op.create_index(
        "ix_scalp_cycle_recent",
        "scalp_cycle",
        ("environment_id", "account_ref", "triggered_at"),
        unique=False,
        schema="halpha",
    )
    read_roles = [
        f"{role_prefix}_app",
        f"{role_prefix}_backup",
        *([f"{role_prefix}_app_reader"] if is_live else []),
    ]
    for role in read_roles:
        op.execute(
            sa.text(f'GRANT SELECT ON TABLE halpha.scalp_cycle TO "{role}"')
        )
    op.execute(
        sa.text(
            f'GRANT INSERT ON TABLE halpha.scalp_cycle TO "{role_prefix}_app"'
        )
    )


def downgrade() -> None:
    raise RuntimeError("DATABASE_DOWNGRADE_FORBIDDEN")
