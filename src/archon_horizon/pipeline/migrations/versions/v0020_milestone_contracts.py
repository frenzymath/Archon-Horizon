"""Objective-scoped milestone checks and authenticated baseline acceptance."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0020_milestone_contracts"
down_revision = "0019_assignment_checkpoint"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("project", sa.Column("workflow", sa.Text(), nullable=False, server_default="legacy"))
    op.create_check_constraint("ck_project_workflow_values", "project", "workflow IN ('legacy','milestones')")
    op.create_table("milestone_check",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("project_id", pg.UUID(as_uuid=True), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("repository_id", pg.UUID(as_uuid=True), sa.ForeignKey("repository.id"), nullable=False),
        sa.Column("principal_id", pg.UUID(as_uuid=True), sa.ForeignKey("principal.id"), nullable=False),
        sa.Column("source_commit_oid", sa.Text(), nullable=False),
        sa.Column("base_commit_oid", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("report", pg.JSONB(), nullable=False),
        sa.Column("report_sha256", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('route','contract','graph','proof')", name="ck_milestone_check_kind_values"),
        sa.UniqueConstraint("repository_id", "report_sha256"))
    op.create_table("milestone_acceptance",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("project_id", pg.UUID(as_uuid=True), sa.ForeignKey("project.id"), nullable=False),
        sa.Column("snapshot_id", pg.UUID(as_uuid=True), sa.ForeignKey("roadmap_snapshot.id"), nullable=False),
        sa.Column("principal_id", pg.UUID(as_uuid=True), sa.ForeignKey("principal.id"), nullable=False),
        sa.Column("check_id", pg.UUID(as_uuid=True), sa.ForeignKey("milestone_check.id"), nullable=False),
        sa.Column("previous_snapshot_id", pg.UUID(as_uuid=True), sa.ForeignKey("roadmap_snapshot.id")),
        sa.Column("note", sa.Text(), nullable=False),
        sa.UniqueConstraint("snapshot_id"))
    for table in ("milestone_check", "milestone_acceptance"):
        op.execute(f"CREATE TRIGGER immutable_record BEFORE UPDATE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION horizon_immutable_record()")
        for column in ("project_id", "principal_id"):
            op.create_index(f"ix_{table}_{column}", table, [column])
    op.create_index("ix_milestone_check_repository_id", "milestone_check", ["repository_id"])
    for column in ("snapshot_id", "check_id", "previous_snapshot_id"):
        op.create_index(f"ix_milestone_acceptance_{column}", "milestone_acceptance", [column])
    op.create_table('milestone_job',
        sa.Column('id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('revision', sa.BigInteger(), nullable=False, server_default='1'),
        sa.Column('project_id', pg.UUID(as_uuid=True), sa.ForeignKey('project.id'), nullable=False),
        sa.Column('host_id', pg.UUID(as_uuid=True), sa.ForeignKey('host.id'), nullable=False),
        sa.Column('workspace_id', pg.UUID(as_uuid=True), sa.ForeignKey('workspace.id'), nullable=False),
        sa.Column('principal_id', pg.UUID(as_uuid=True), sa.ForeignKey('principal.id'), nullable=False),
        sa.Column('request', pg.JSONB(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False, server_default='queued'),
        sa.Column('attempts', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('claim_token', pg.UUID(as_uuid=True)),
        sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('check_id', pg.UUID(as_uuid=True), sa.ForeignKey('milestone_check.id')),
        sa.Column('error', sa.Text()),
        sa.CheckConstraint("status IN ('queued','running','completed','failed')", name='ck_milestone_job_status_values'),
        sa.CheckConstraint('revision > 0', name='ck_milestone_job_positive_revision'))
    for column in ('project_id', 'host_id', 'workspace_id', 'principal_id', 'check_id'):
        op.create_index('ix_milestone_job_' + column, 'milestone_job', [column])
    op.execute('CREATE TRIGGER touch_revision BEFORE UPDATE ON milestone_job '
               'FOR EACH ROW EXECUTE FUNCTION horizon_touch_revision()')
    op.execute("CREATE TRIGGER immutable_fields BEFORE UPDATE ON milestone_job FOR EACH ROW "
               "EXECUTE FUNCTION horizon_immutable_fields('id','created_at','project_id','host_id','workspace_id','principal_id','request')")


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
