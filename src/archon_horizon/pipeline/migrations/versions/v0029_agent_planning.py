"""Agent-authored graph planning without rewriting existing project policy."""

from alembic import op
import sqlalchemy as sa

revision = "0029_agent_planning"
down_revision = "0028_objective_queues"
branch_labels = None
depends_on = None


def upgrade():
    # Existing rows keep legacy/milestones, including accepted evidence. New
    # projects use graph; an operator can explicitly switch an idle project.
    op.drop_constraint("ck_project_workflow_values", "project", type_="check")
    op.create_check_constraint("ck_project_workflow_values", "project",
                               "workflow IN ('graph','legacy','milestones')")
    op.alter_column("project", "workflow", server_default=sa.text("'graph'"))
    op.drop_constraint("ck_run_adopted_baseline", "run", type_="check")
    op.create_check_constraint("ck_run_adopted_baseline", "run",
        "(phase->>'kind' <> 'formalization' AND adopted_roadmap_snapshot_id IS NULL) OR "
        "(phase->>'kind' = 'formalization' AND "
        "(adopted_roadmap_snapshot_id IS NOT NULL OR orchestration = 'objective'))")
    # Phase used to be immutable because a legacy run represented one phase.
    # Objective runs can advance only to the maintainer's already-recorded
    # pending decision. Preserve the old boundary for saved legacy runs.
    op.execute("DROP TRIGGER immutable_fields ON run")
    op.execute("CREATE TRIGGER immutable_fields BEFORE UPDATE ON run FOR EACH ROW "
        "EXECUTE FUNCTION horizon_immutable_fields('id','created_at','mission_id','number',"
        "'objective_id','orchestration','requested_phases')")
    op.execute("""
        CREATE FUNCTION horizon_run_phase_transition() RETURNS trigger AS $$
        BEGIN
            IF OLD.phase IS DISTINCT FROM NEW.phase THEN
                IF OLD.orchestration <> 'objective'
                    OR OLD.pending_phase IS NULL
                    OR (OLD.pending_phase - '_owner_session_id') IS DISTINCT FROM NEW.phase
                    OR NEW.pending_phase IS NOT NULL
                    OR NEW.phase_history IS DISTINCT FROM OLD.phase_history
                    OR jsonb_array_length(OLD.phase_history) = 0
                    OR OLD.phase_history->-1->'phase' IS DISTINCT FROM OLD.phase
                THEN
                    RAISE EXCEPTION 'phase transition requires a recorded objective acceptance'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("CREATE TRIGGER accepted_phase BEFORE UPDATE ON run FOR EACH ROW "
               "EXECUTE FUNCTION horizon_run_phase_transition()")


def downgrade():
    raise RuntimeError("destructive pipeline downgrade is unsupported; restore an explicit backup instead")
