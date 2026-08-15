"""Expose the latest account-observation failure without weakening risk gates.

Revision ID: 20260815_0015
Revises: 20260813_0014
"""

from __future__ import annotations

import re

from alembic import op
import sqlalchemy as sa


revision = "20260815_0015"
down_revision = "20260813_0014"
branch_labels = None
depends_on = None


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
        "account_observation_failure",
        sa.Column("environment_id", sa.String(96), nullable=False),
        sa.Column("account_ref", sa.String(160), nullable=False),
        sa.Column("failure_at", UTC_TS, nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("retry_after_seconds", sa.Numeric(12, 3)),
        sa.PrimaryKeyConstraint(
            "environment_id",
            "account_ref",
            name="pk_account_observation_failure",
        ),
        sa.CheckConstraint(
            "reason_code ~ '^[A-Z0-9_]{1,128}$'",
            name="ck_account_observation_failure_reason_code",
        ),
        sa.CheckConstraint(
            "retry_after_seconds IS NULL OR retry_after_seconds >= 0",
            name="ck_account_observation_failure_retry_after",
        ),
        schema="halpha",
    )
    read_roles = [
        f"{role_prefix}_app",
        f"{role_prefix}_backup",
        *([f"{role_prefix}_app_reader"] if is_live else []),
    ]
    for role in read_roles:
        op.execute(
            sa.text(
                "GRANT SELECT ON TABLE halpha.account_observation_failure "
                f'TO "{role}"'
            )
        )
    executor_role = f"{role_prefix}_executor"
    op.execute(
        sa.text(
            "GRANT INSERT, UPDATE ON TABLE halpha.account_observation_failure "
            f'TO "{executor_role}"'
        )
    )


def downgrade() -> None:
    raise RuntimeError("DATABASE_DOWNGRADE_FORBIDDEN")
