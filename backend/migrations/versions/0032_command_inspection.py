"""命令巡检（4.4 P21）：巡检模板、巡检任务、每次执行与逐项结果。

Revision ID: 0032
Revises: 0031
"""
from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inspection_templates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("builtin_key", sa.String(64), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False, server_default=""),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", sa.String(200), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inspection_templates")),
        sa.UniqueConstraint("builtin_key", name=op.f("uq_inspection_templates_builtin_key")),
    )
    op.create_table(
        "inspection_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("asset_ids", sa.JSON(), nullable=False),
        sa.Column("schedule", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["template_id"], ["inspection_templates.id"], name=op.f("fk_inspection_jobs_template_id_inspection_templates"),
                                ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inspection_jobs")),
    )
    op.create_index(op.f("ix_inspection_jobs_template_id"), "inspection_jobs", ["template_id"])
    op.create_index(op.f("ix_inspection_jobs_next_run_at"), "inspection_jobs", ["next_run_at"])
    op.create_table(
        "inspection_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("template_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("template_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False, server_default="linux"),
        sa.Column("triggered_by", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("asset_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("assets_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["inspection_jobs.id"], name=op.f("fk_inspection_runs_job_id_inspection_jobs"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["template_id"], ["inspection_templates.id"], name=op.f("fk_inspection_runs_template_id_inspection_templates"),
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inspection_runs")),
    )
    op.create_index(op.f("ix_inspection_runs_job_id"), "inspection_runs", ["job_id"])
    op.create_index(op.f("ix_inspection_runs_template_id"), "inspection_runs", ["template_id"])
    op.create_index(op.f("ix_inspection_runs_created_at"), "inspection_runs", ["created_at"])
    op.create_index(op.f("ix_inspection_runs_status"), "inspection_runs", ["status"])
    op.create_table(
        "inspection_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("asset_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("item_key", sa.String(64), nullable=False, server_default=""),
        sa.Column("item_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("command", sa.String(1000), nullable=False, server_default=""),
        sa.Column("output", sa.Text(), nullable=False, server_default=""),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, server_default=""),
        sa.Column("detail", sa.String(1000), nullable=False, server_default=""),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["inspection_runs.id"], name=op.f("fk_inspection_results_run_id_inspection_runs"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], name=op.f("fk_inspection_results_asset_id_assets"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inspection_results")),
    )
    op.create_index(op.f("ix_inspection_results_run_id"), "inspection_results", ["run_id"])
    op.create_index(op.f("ix_inspection_results_asset_id"), "inspection_results", ["asset_id"])
    op.create_index(op.f("ix_inspection_results_verdict"), "inspection_results", ["verdict"])
    op.create_index(op.f("ix_inspection_results_observed_at"), "inspection_results", ["observed_at"])


def downgrade() -> None:
    op.drop_table("inspection_results")
    op.drop_table("inspection_runs")
    op.drop_table("inspection_jobs")
    op.drop_table("inspection_templates")
