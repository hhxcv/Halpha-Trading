"""Bind each AI review to its selected Codex model configuration.

Revision ID: 20260813_0014
Revises: 20260813_0013
"""

from alembic import op
import sqlalchemy as sa


revision = "20260813_0014"
down_revision = "20260813_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan_ai_review",
        sa.Column("review_model", sa.String(64), nullable=False, server_default="gpt-5.6-terra"),
        schema="halpha",
    )
    op.add_column(
        "plan_ai_review",
        sa.Column("reasoning_effort", sa.String(16), nullable=False, server_default="medium"),
        schema="halpha",
    )
    op.create_check_constraint(
        "ck_plan_ai_review_model",
        "plan_ai_review",
        "review_model IN ('gpt-5.6-terra','gpt-5.6-sol')",
        schema="halpha",
    )
    op.create_check_constraint(
        "ck_plan_ai_review_reasoning_effort",
        "plan_ai_review",
        "reasoning_effort IN ('low','medium','high')",
        schema="halpha",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION halpha.guard_plan_ai_review_transition()
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
             OR NEW.review_model <> OLD.review_model
             OR NEW.reasoning_effort <> OLD.reasoning_effort
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
def downgrade() -> None:
    raise RuntimeError("DATABASE_DOWNGRADE_FORBIDDEN")
