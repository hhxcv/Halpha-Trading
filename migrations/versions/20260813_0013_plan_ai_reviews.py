"""Gate new-risk plan fixing and activation on one exact AI review.

Revision ID: 20260813_0013
Revises: 20260803_0012
"""

from __future__ import annotations

import re

from alembic import op
from sqlalchemy.dialects import postgresql
import sqlalchemy as sa


revision = "20260813_0013"
down_revision = "20260803_0012"
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
        "plan_ai_review",
        sa.Column("review_id", UUID, primary_key=True),
        sa.Column("environment_id", sa.String(96), nullable=False),
        sa.Column("plan_id", UUID, nullable=False),
        sa.Column("draft_version", sa.BigInteger(), nullable=False),
        sa.Column("draft_content_digest", sa.CHAR(64), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("request_digest", sa.CHAR(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("market_context_digest", sa.CHAR(64)),
        sa.Column("market_source_cutoff", UTC_TS),
        sa.Column("decision", sa.String(16)),
        sa.Column("reason", sa.Text()),
        sa.Column("suggestions", JSONB, nullable=False),
        sa.Column("progress_message", sa.String(240), nullable=False),
        sa.Column("public_output", sa.Text(), nullable=False),
        sa.Column("failure_code", sa.String(96)),
        sa.Column("created_at", UTC_TS, nullable=False),
        sa.Column("started_at", UTC_TS),
        sa.Column("completed_at", UTC_TS),
        sa.Column("updated_at", UTC_TS, nullable=False),
        sa.ForeignKeyConstraint(
            ("environment_id", "plan_id"),
            (
                "halpha.trade_plan_draft.environment_id",
                "halpha.trade_plan_draft.plan_id",
            ),
            name="fk_plan_ai_review_draft",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("draft_version > 0", name="ck_plan_ai_review_draft_version"),
        sa.CheckConstraint(
            "draft_content_digest ~ '^[0-9a-f]{64}$'",
            name="ck_plan_ai_review_draft_digest",
        ),
        sa.CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'",
            name="ck_plan_ai_review_request_digest",
        ),
        sa.CheckConstraint(
            "market_context_digest IS NULL OR "
            "market_context_digest ~ '^[0-9a-f]{64}$'",
            name="ck_plan_ai_review_market_digest",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','APPROVED','REJECTED','FAILED')",
            name="ck_plan_ai_review_status",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('APPROVE','REJECT')",
            name="ck_plan_ai_review_decision",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(suggestions) = 'array'",
            name="ck_plan_ai_review_suggestions",
        ),
        sa.CheckConstraint(
            "(status = 'APPROVED' AND decision = 'APPROVE' "
            " AND reason IS NOT NULL AND completed_at IS NOT NULL "
            " AND failure_code IS NULL) OR "
            "(status = 'REJECTED' AND decision = 'REJECT' "
            " AND reason IS NOT NULL AND completed_at IS NOT NULL "
            " AND failure_code IS NULL) OR "
            "(status = 'FAILED' AND decision IS NULL AND reason IS NULL "
            " AND failure_code IS NOT NULL AND completed_at IS NOT NULL) OR "
            "(status IN ('QUEUED','RUNNING') AND decision IS NULL "
            " AND reason IS NULL AND failure_code IS NULL "
            " AND completed_at IS NULL)",
            name="ck_plan_ai_review_result_shape",
        ),
        sa.UniqueConstraint(
            "environment_id",
            "review_id",
            name="uq_plan_ai_review_environment",
        ),
        sa.UniqueConstraint(
            "environment_id",
            "idempotency_key",
            name="uq_plan_ai_review_idempotency",
        ),
        schema="halpha",
    )
    op.create_index(
        "ix_plan_ai_review_latest",
        "plan_ai_review",
        ("environment_id", "plan_id", "created_at", "review_id"),
        unique=False,
        schema="halpha",
    )
    op.add_column(
        "trade_plan_version",
        sa.Column("ai_review_ref", UUID),
        schema="halpha",
    )
    op.create_foreign_key(
        "fk_trade_plan_version_ai_review",
        "trade_plan_version",
        "plan_ai_review",
        ("environment_id", "ai_review_ref"),
        ("environment_id", "review_id"),
        source_schema="halpha",
        referent_schema="halpha",
    )
    op.execute(
        """
        CREATE FUNCTION halpha.guard_plan_ai_review_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF NEW.review_id <> OLD.review_id
             OR NEW.environment_id <> OLD.environment_id
             OR NEW.plan_id <> OLD.plan_id
             OR NEW.draft_version <> OLD.draft_version
             OR NEW.draft_content_digest <> OLD.draft_content_digest
             OR NEW.idempotency_key <> OLD.idempotency_key
             OR NEW.request_digest <> OLD.request_digest
             OR NEW.prompt_version <> OLD.prompt_version
             OR NEW.created_at <> OLD.created_at THEN
            RAISE EXCEPTION 'PLAN_AI_REVIEW_IDENTITY_IMMUTABLE' USING ERRCODE = '23514';
          END IF;
          IF OLD.status IN ('APPROVED','REJECTED','FAILED') THEN
            RAISE EXCEPTION 'PLAN_AI_REVIEW_TERMINAL' USING ERRCODE = '23514';
          END IF;
          IF NOT (
            (OLD.status = 'QUEUED' AND NEW.status IN ('RUNNING','FAILED'))
            OR (OLD.status = 'RUNNING' AND NEW.status IN ('RUNNING','APPROVED','REJECTED','FAILED'))
          ) THEN
            RAISE EXCEPTION 'PLAN_AI_REVIEW_TRANSITION_INVALID' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_plan_ai_review_transition
        BEFORE UPDATE ON halpha.plan_ai_review
        FOR EACH ROW
        EXECUTE FUNCTION halpha.guard_plan_ai_review_transition()
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION halpha.guard_plan_ai_review_transition() FROM PUBLIC"
    )
    read_roles = [
        f"{role_prefix}_app",
        f"{role_prefix}_backup",
        *([f"{role_prefix}_app_reader"] if is_live else []),
    ]
    for role in read_roles:
        op.execute(
            sa.text(f'GRANT SELECT ON TABLE halpha.plan_ai_review TO "{role}"')
        )
    app_role = f"{role_prefix}_app"
    op.execute(
        sa.text(f'GRANT INSERT ON TABLE halpha.plan_ai_review TO "{app_role}"')
    )
    op.execute(
        sa.text(
            "GRANT UPDATE (status, market_context_digest, market_source_cutoff, "
            "decision, reason, suggestions, progress_message, public_output, "
            "failure_code, started_at, completed_at, updated_at) ON TABLE "
            f'halpha.plan_ai_review TO "{app_role}"'
        )
    )


def downgrade() -> None:
    raise RuntimeError("DATABASE_DOWNGRADE_FORBIDDEN")
