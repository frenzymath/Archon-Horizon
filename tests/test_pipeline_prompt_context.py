import json
import re
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select

from archon_horizon.pipeline import models
from archon_horizon.pipeline.instructions import prompts
from archon_horizon.pipeline.instructions.prompts import LEDGER_PROMPT_ROWS, goal
from archon_horizon.pipeline.persistence.records import change, create, get, json_value
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_reviewer_invocations import review  # noqa: F401
from test_pipeline_service import service_database, world


def test_oversized_prompt_is_bounded_and_links_exact_full_revision(world):
    mission = change(world.conn, "mission", world.mission["id"],
                     objective="Current mathematical scope.\n" + "Old evidence details.\n" * 4000 + "FINAL CONSTRAINT")
    run = world.run()
    world.disable_automations(run)
    instructions = "Scoped implementation directions.\n" * 2000 + "LAST INSTRUCTION"
    assignment = world.assignment(run, instructions=instructions)
    original_ledger = world.ledger(assignment["id"])
    prompt = goal(world.conn, world.service, assignment, run, mission, initial=False)
    assert len(prompt) < 15000
    assert prompt.count("Current mathematical scope.") == 1
    assert "this obligation remains open" in prompt
    assert "Some record text is excerpted, not removed from the objective" in prompt
    artifact_id = re.search(r"GET /api/v3/artifacts/([\w-]+)/content", prompt).group(1)
    artifact = get(world.conn, "artifact", artifact_id)
    full = json.loads(world.service.store.read(artifact["content"]["sha256"]))
    assert full["mission"]["revision"] == mission["revision"]
    assert full["mission"]["objective"] == mission["objective"]
    assert full["assignment"]["instructions"] == instructions
    assert full["assignment"]["revision"] == assignment["revision"]
    assert full["obligations"][0]["description"] == mission["objective"]
    assert get(world.conn, "mission", mission["id"]) == mission
    assert world.ledger(assignment["id"]) == original_ledger
    before = world.conn.execute(select(func.count()).select_from(tables["artifact"])).scalar_one()
    assert goal(world.conn, world.service, assignment, run, mission, initial=False) == prompt
    assert world.conn.execute(select(func.count()).select_from(tables["artifact"])).scalar_one() == before


def test_short_objective_is_not_repeated_as_instructions_or_seed_obligation(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, instructions=world.mission["objective"])
    prompt = goal(world.conn, world.service, assignment, run, world.mission, initial=False)
    assert prompt.count(world.mission["objective"]) == 1
    assert str(world.ledger(assignment["id"])[0]["id"]) in prompt
    assert "this obligation remains open" in prompt
    assert "prompt-context snapshot" not in prompt


def test_large_ledger_preserves_open_items_with_explicit_overflow_and_snapshot(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    for number in range(2, LEDGER_PROMPT_ROWS + 5):
        create(world.conn, "obligation", assignment_id=assignment["id"], number=number,
               description=f"Concrete independent requirement {number}: " + "evidence detail " * 1000)
    prompt = goal(world.conn, world.service, assignment, run, world.mission, initial=False)
    assert len(prompt) < 20000
    assert f"Ledger excerpt capped at {LEDGER_PROMPT_ROWS}" in prompt
    assert f"/api/v3/records/obligation?assignment_id={assignment['id']}" in prompt
    artifact_id = re.search(r"GET /api/v3/artifacts/([\w-]+)/content", prompt).group(1)
    artifact = get(world.conn, "artifact", artifact_id)
    full = json.loads(world.service.store.read(artifact["content"]["sha256"]))
    assert len(full["obligations"]) == LEDGER_PROMPT_ROWS
    assert all(len(item["description"]) > 10000 for item in full["obligations"][1:])
    assert len(world.ledger(assignment["id"])) == LEDGER_PROMPT_ROWS + 4


def test_continuation_preserves_changed_nonseed_obligation_without_skill_reload(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    seed = world.ledger(assignment["id"])[0]
    change(world.conn, "obligation", seed["id"], description="Check a distinct consumer before finishing")
    prompt = goal(world.conn, world.service, assignment, run, world.mission, initial=False,
                  findings=["The consumer remains unchecked."])
    assert "Check a distinct consumer before finishing" in prompt
    assert "The consumer remains unchecked." in prompt
    assert "Continue the retained provider context" in prompt
    assert "On initial entry" not in prompt


def test_postprocessing_continuation_keeps_phase_scope_without_repeating_startup(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    # A retained provider thread keeps its pinned bundle, while the live goal
    # still communicates current phase operation without a skill reload.
    run = {**run, "phase": {"kind": "postprocessing", "target_repository_id": str(world.workspace_repo["id"])}}
    change(world.conn, "provider_thread", claim["provider_thread_record_id"], provider_thread_id="retained", status="available")
    prompt = goal(world.conn, world.service, assignment, run, world.mission, initial=False)
    assert prompts.PHASE_CONTRACTS["postprocessing"] in prompt
    assert prompts.CORE not in prompt
    assert "Available Skills" not in prompt
    assert "Run coordination handoff" not in prompt
    maintainer_prompt = goal(world.conn, world.service, {**assignment, "role": "maintainer"},
                             run, world.mission, initial=False)
    assert prompts.PHASE_CONTRACTS["postprocessing"] in maintainer_prompt
    assert prompts.MAINTAINER not in maintainer_prompt
    assert len(maintainer_prompt) < 7000


def test_postprocessing_maintainer_sees_current_pr_readiness_without_a_second_batch_policy(world):
    run = world.run()
    world.disable_automations(run)
    repository = world.workspace_repo
    items = []
    for number in (101, 102):
        items.append(create(world.conn, "forge_item", repository_id=repository["id"], remote_number=number,
               kind="pull_request", origin_run_id=run["id"], review_phase="postprocessing",
               target_branch="main", title=f"Port family {number}", status="open",
               head_commit_oid=f"{number:040x}", labels=["awaiting-review"], observed_at=func.now()))
    assignment = world.assignment(run, role="maintainer")
    post_run = {**run, "phase": {"kind": "postprocessing", "target_repository_id": str(repository["id"])} }
    prompt = goal(world.conn, world.service, assignment, post_run, world.mission, initial=False)
    assert "Current exact-head review readiness" in prompt
    for item in items:
        assert str(item["id"]) in prompt and item["head_commit_oid"] in prompt
    assert "Current post-processing review batch" not in prompt
    worker_prompt = goal(world.conn, world.service, {**assignment, "role": "worker"},
                         post_run, world.mission, initial=False)
    assert "Current exact-head review readiness" not in worker_prompt


def test_completion_rule_reaches_retained_contexts_in_every_phase(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    world.claim()
    for initial in (True, False):
        for role in ("worker", "maintainer"):
            prompt = goal(world.conn, world.service, {**assignment, "role": role},
                          run, world.mission, initial=initial)
            assert prompts.SESSION_HANDOFF in prompt
            assert "A delivered worker PR need not be accepted" in prompt
            assert "Collect native children before returning" in prompt
            assert "Checkpointing releases execution" in prompt


def test_initial_and_retained_goals_keep_child_integration_separate_from_root_closure(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, role="maintainer")
    child = world.service.mission(world.conn, world.actor, models.MissionCreate(
        project_id=world.project["id"], parent_id=world.mission["id"],
        expected_parent_revision=world.mission["revision"], title="Integrate accepted contracts",
        objective="Merge the reviewed contracts and deliver the approval packet",
        acceptance_criteria=["Deliver exact accepted commit and packet"],
        delegation_note="Root maintainer owns final root closure"))
    for initial in (True, False):
        for mission in (world.mission, child):
            prompt = goal(world.conn, world.service, {**assignment, "mission_id": mission["id"]},
                          run, mission, initial=initial)
            assert "a child can close its own subtree, never an ancestor" in prompt
            assert "root maintainer owns final phase acceptance and run draining" in prompt
            assert "event\ncondition including failure/cancellation" in prompt
            assert "reuse its accepted evidence for root closure" in prompt


def test_orchestrator_prompt_is_control_plane_only(world):
    run = world.run(orchestrated=True)
    world.disable_automations(run)
    run = {**run, "phase": {"kind": "preprocessing", "orchestrated": True,
                             "roadmap_document_id": world.document["id"]}}
    assignment = world.assignment(run, role="maintainer", functions=["orchestrator"])
    prompt = goal(world.conn, world.service, assignment, run, world.mission, initial=True)
    assert "control-plane supervisor" in prompt
    assert "Do not run `lake`, `lean`, `git`" in prompt
    assert "GET /api/v3/records/automation?run_id=<run_id>" in prompt
    assert "No repository, mathematical, or Forge implementation work" in prompt
    assert "Execution counts and journal reconciliations are activity, not progress" in prompt
    assert "maintainer implementing a bounded repair" in prompt
    assert "concrete executable scope that has no existing owner" in prompt
    assert "a child\ncannot close siblings or ancestors" in prompt
    assert "do not suppress\nthat administrative owner as duplicate integration" in prompt
    assert "POST /api/v3/notifications/<notice-id>/disposition" in prompt
    assert '"disposition":"handled"' in prompt
    assert "four contract-checking builds" not in prompt


def test_orchestrator_initial_and_retained_prompts_exclude_mathematical_context(world):
    run = world.run(orchestrated=True)
    world.disable_automations(run)
    mission = change(world.conn, "mission", world.mission["id"], objective="PRIVATE MATHEMATICAL BODY " * 10000)
    assignment = world.assignment(run, role="maintainer", functions=["orchestrator"],
                                  instructions="OLD MATHEMATICAL INSTRUCTIONS " * 1000)
    for initial in (True, False):
        prompt = goal(world.conn, world.service, assignment, run, mission, initial=initial)
        assert len(prompt.encode()) < 22000
        assert "PRIVATE MATHEMATICAL BODY" not in prompt
        assert "OLD MATHEMATICAL INSTRUCTIONS" not in prompt
        assert "Current obligations:" not in prompt
        assert "Session completion rule" not in prompt
        assert "SKILLS.md" not in prompt
        assert "On initial entry" not in prompt
        assert '"operation":"defer_automation"' in prompt
        assert "POST /api/v3/messages/read" in prompt


def test_orchestrator_context_reports_profiles_binding_and_project_backlog(world):
    run = world.run(orchestrated=True)
    world.disable_automations(run)
    supervisor = world.assignment(run, role="maintainer", functions=["orchestrator"])
    maintainer = world.assignment(run, role="maintainer")
    integration = create(world.conn, "integration", kind="zulip", endpoint="https://zulip.invalid",
                         credential_ref="secret:not-for-context")
    discussion = create(world.conn, "discussion", project_id=world.project["id"], integration_id=integration["id"],
                        channel_remote_id="10", topic="Horizon operations", observed_at=func.now())
    create(world.conn, "forge_item", repository_id=world.document["source_repository_id"], remote_number=1,
           kind="pull_request", title="Ready contract", status="open", head_commit_oid="a" * 40,
           observed_at=func.now())
    create(world.conn, "forge_item", repository_id=world.workspace_repo["id"], remote_number=1,
           kind="pull_request", title="Unrelated workspace PR", status="open", head_commit_oid="b" * 40,
           observed_at=func.now())
    for view in ("brief", "full"):
        context = world.service.context(world.conn, world.actor, supervisor["id"], view=view)
        assert context["view"] == "orchestrator"
        assert "objective" not in context["mission"]
        assert "obligations" not in context
        owners = {row["id"]: row for row in context["assignments"]}
        assert owners[supervisor["id"]]["profile"] == "orchestrator"
        assert owners[maintainer["id"]]["profile"] == "maintainer"
        automations = {row["name"]: row for row in context["automations"]}
        assert automations["maintainer"]["profile"] == "maintainer"
        assert automations["orchestrator"]["profile"] == "orchestrator"
        assert context["operations_reporting"]["discussion_id"] == discussion["id"]
        assert context["forge"]["actionable_pull_requests"] == 1
        assert "secret:not-for-context" not in json.dumps(context, default=str)


def test_orchestrator_does_not_guess_operations_topic_or_expand_context(world):
    run = world.run(orchestrated=True)
    world.disable_automations(run)
    supervisor = world.assignment(run, role="maintainer", functions=["orchestrator"])
    integration = create(world.conn, "integration", kind="zulip", endpoint="https://zulip.invalid",
                         credential_ref="secret:test")
    create(world.conn, "discussion", project_id=world.project["id"], integration_id=integration["id"],
           channel_remote_id="10", topic="Mathematical discussion", observed_at=func.now())
    for _ in range(20):
        world.assignment(run, instructions="Long worker directions " * 1000)
    context = world.service.context(world.conn, world.actor, supervisor["id"])
    assert context["operations_reporting"]["status"] == "unconfigured"
    assert context["operations_reporting"]["discussion_id"] is None
    assert context["collections"]["assignments"]["truncated"]
    assert "Mathematical discussion" not in json.dumps(context, default=str)
    from archon_horizon.pipeline.execution.context_briefing import size
    assert size(context) <= prompts.ORCHESTRATOR_CONTEXT_BYTES


def test_orchestrator_sees_bounded_current_productive_ownership(world):
    run = world.run(orchestrated=True)
    world.disable_automations(run)
    supervisor = world.assignment(run, role="maintainer", functions=["orchestrator"],
                                  instructions="SUPERVISOR PRIVATE MATHEMATICAL INSTRUCTIONS")
    parent = get(world.conn, "mission", world.mission["id"])
    mission = world.service.mission(world.conn, world.actor, models.MissionCreate(
        project_id=world.project["id"], parent_id=parent["id"], expected_parent_revision=parent["revision"],
        title="Repair and review the spectral statement",
        objective="Resolve the current-head contract objections. " + "Scoped evidence. " * 1000,
        acceptance_criteria=["Current-head review accepts the repaired statement"],
        delegation_note="Own this bounded contract repair and its review."))
    maintainer = world.service.assignment(world.conn, world.actor, models.AssignmentCreate(
        run_id=run["id"], role="maintainer", mission_id=mission["id"],
        instructions="Publish the repair and prepare its reviewers. " + "Current task detail. " * 1000))
    change(world.conn, "assignment", maintainer["id"], status="running")

    context = world.service.context(world.conn, world.actor, supervisor["id"])
    owners = {row["id"]: row for row in context["assignments"]}
    actual = owners[maintainer["id"]]
    assert actual["profile"] == "maintainer" and actual["status"] == "running"
    assert actual["instructions"].startswith("Publish the repair and prepare its reviewers.")
    assert actual["detail_url"] == f"/api/v3/records/assignment/{maintainer['id']}"
    assert actual["mission"]["id"] == mission["id"]
    assert actual["mission"]["title"] == mission["title"]
    assert actual["mission"]["objective"].startswith("Resolve the current-head contract objections.")
    assert actual["mission"]["detail_url"] == f"/api/v3/records/mission/{mission['id']}"
    assert "instructions" in actual["truncated_fields"]
    assert "objective" in actual["mission"]["truncated_fields"]
    assert len(actual["instructions"].encode()) <= 512
    assert len(actual["mission"]["objective"].encode()) <= 384
    assert "instructions" not in owners[supervisor["id"]]
    assert "mission" not in owners[supervisor["id"]]
    assert len(json.dumps(json_value(context), indent=2).encode()) <= prompts.ORCHESTRATOR_CONTEXT_BYTES


def test_planner_recovery_authority_reaches_initial_and_retained_contexts(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, functions=["planner"])
    world.claim()
    for initial in (True, False):
        prompt = goal(world.conn, world.service, assignment, run, world.mission, initial=initial)
        assert prompt.count(prompts.PLANNER_RECOVERY) == 1
        assert "its own obligation ledger" in prompt
        assert "identified maintainer" in prompt


def test_orchestrator_unicode_context_fits_prompt_and_agent_cli_byte_budgets(world):
    from archon_horizon.pipeline.execution.notifications import OperatorNotice, operator_notice

    run = world.run(orchestrated=True)
    world.disable_automations(run)
    supervisor = world.assignment(run, role="maintainer", functions=["orchestrator"])
    mission = change(world.conn, "mission", world.mission["id"], title="\u03b1" * 50)
    for number in range(12):
        operator_notice(world.conn, world.actor, supervisor["id"],
            OperatorNotice(message=f"Incident {number}: " + "\u03b1" * 2000))
        world.assignment(run)

    context = world.service.context(world.conn, world.actor, supervisor["id"])
    cli_output = json.dumps(json_value(context), indent=2) + "\n"
    assert len(cli_output.encode("utf-8")) <= prompts.ORCHESTRATOR_CONTEXT_BYTES
    assert context["control_notices"]["pending"] == 12
    prompt = goal(world.conn, world.service, supervisor, run, mission)
    rendered_snapshot = prompt.split("Current control snapshot:\n", 1)[1]
    assert len(rendered_snapshot.encode("utf-8")) <= prompts.ORCHESTRATOR_CONTEXT_BYTES
    assert "\u03b1" * 50 in rendered_snapshot
    assert json.loads(rendered_snapshot)["mission"]["title"] == mission["title"]


def test_orchestrator_formalization_backlog_uses_adopted_roadmap_repository(world):
    artifact = create(world.conn, "artifact", project_id=world.project["id"], kind="blob",
        content={"sha256": "a" * 64, "size_bytes": 0, "media_type": "application/json"})
    baseline = create(world.conn, "roadmap_snapshot", project_id=world.project["id"],
        roadmap_document_id=world.document["id"], source_commit_oid="a" * 40,
        graph_manifest_artifact_id=artifact["id"])
    run = world.scheduler.run(world.conn, world.actor, models.RunCreate(orchestration="legacy", mission_id=world.mission["id"],
        phase={"kind": "formalization", "roadmap_snapshot_id": baseline["id"], "orchestrated": True},
        host_ids=[world.host["id"]]))
    world.disable_automations(run)
    supervisor = world.assignment(run, role="maintainer", functions=["orchestrator"])
    for repository_id in (world.document["source_repository_id"], world.workspace_repo["id"]):
        create(world.conn, "forge_item", repository_id=repository_id, remote_number=1,
            kind="pull_request", title="Open change", status="open", head_commit_oid="a" * 40,
            observed_at=func.now())

    context = world.service.context(world.conn, world.actor, supervisor["id"])
    assert context["forge"] == {"open_pull_requests": 1, "actionable_pull_requests": 1,
                                "scope": "adopted roadmap repository"}


def test_orchestrator_context_includes_project_verification_jobs_without_inputs(world):
    run = world.run(orchestrated=True)
    world.disable_automations(run)
    supervisor = world.assignment(run, role="maintainer", functions=["orchestrator"])
    workspace = world.conn.execute(select(tables["workspace"]).where(
        tables["workspace"].c.project_id == world.project["id"]).limit(1)).mappings().one()
    now = world.conn.execute(select(func.now())).scalar_one()
    active = create(world.conn, "milestone_job", project_id=world.project["id"], host_id=world.host["id"],
        workspace_id=workspace["id"], principal_id=world.actor.id, status="running", attempts=2,
        lease_until=now + timedelta(seconds=300), claim_token=uuid4(),
        request={"source": "PRIVATE VERIFICATION INPUT"})
    for _ in range(9):
        create(world.conn, "milestone_job", project_id=world.project["id"], host_id=world.host["id"],
            workspace_id=workspace["id"], principal_id=world.actor.id, status="failed", attempts=1,
            error="Build diagnostic " * 100, request={"source": "PRIVATE VERIFICATION INPUT"})

    other = world.service.project(world.conn, world.actor,
        models.ProjectCreate(slug="other_" + world.project["slug"], title="Other project", workflow="legacy"))
    other_repository = create(world.conn, "repository", project_id=other["id"], slug="workspace",
        integration_id=world.workspace_repo["integration_id"], remote_id="other", default_branch="main", purpose="workspace")
    other_workspace = create(world.conn, "workspace", project_id=other["id"], host_id=world.host["id"],
        repository_id=other_repository["id"], path=world.host["workspace_root"] + "/other",
        branch_name="other", base_commit_oid="a" * 40, status="ready")
    foreign = create(world.conn, "milestone_job", project_id=other["id"], host_id=world.host["id"],
        workspace_id=other_workspace["id"], principal_id=world.actor.id, status="running", attempts=1,
        request={"source": "OTHER PROJECT PRIVATE INPUT"})

    context = world.service.context(world.conn, world.actor, supervisor["id"])
    jobs = {row["id"]: row for row in context["milestone_jobs"]}
    assert active["id"] in jobs and foreign["id"] not in jobs
    assert jobs[active["id"]]["host_id"] == world.host["id"]
    assert jobs[active["id"]]["workspace_id"] == workspace["id"]
    assert jobs[active["id"]]["lease_until"] == active["lease_until"]
    assert jobs[active["id"]]["attempts"] == 2
    assert jobs[active["id"]]["status"] == "running"
    assert jobs[active["id"]]["detail_url"] == f"/api/v3/milestones/verifications/{active['id']}"
    collection = context["collections"]["milestone_jobs"]
    assert collection["scope"] == "project" and collection["project_id"] == world.project["id"]
    assert collection["total"] == 10 and collection["by_status"] == {"failed": 9, "running": 1}
    assert collection["included"] == len(jobs) <= 8 and collection["truncated"]
    assert all(len((row["error"] or "").encode("utf-8")) <= 320 for row in jobs.values())
    assert all("request" not in row and "claim_token" not in row for row in jobs.values())
    rendered = json.dumps(json_value(context), indent=2)
    assert "PRIVATE INPUT" not in rendered and "PRIVATE VERIFICATION INPUT" not in rendered
    assert len(rendered.encode("utf-8")) <= prompts.ORCHESTRATOR_CONTEXT_BYTES


def test_initial_worker_prompt_keeps_catalogs_and_global_audit_on_demand(world, monkeypatch):
    from archon_horizon.pipeline.execution import coordination_memory

    def unexpected_memory(*args, **kwargs):
        raise AssertionError("Rendering a worker task must not assemble global coordination history")

    monkeypatch.setattr(coordination_memory, "memory", unexpected_memory)
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, instructions="Deliver the scoped theorem statement")
    prompt = goal(world.conn, world.service, assignment, run, world.mission)
    assert "Deliver the scoped theorem statement" in prompt
    assert "$HORIZON_SKILLS_DIR/operations/horizon-pipeline/SKILL.md" in prompt
    assert "SUBAGENTS.md" in prompt
    assert "# Available Skills" not in prompt and "# Available Subagents" not in prompt
    assert len(prompt) < 7000


def test_prepared_reviewer_receives_only_its_packet_and_completion_notices(world, review):
    from archon_horizon.pipeline.review.invocations import prepare_assignment
    from test_pipeline_reviewer_reports import reviewer_account

    reviewer_account(world, review)
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], invocation="assignment")
    prepared = prepare_assignment(world.conn, review["actor"], review["data"], world.service)
    assignment = prepared["assignment"]
    mission = get(world.conn, "mission", assignment["mission_id"])
    prompt = goal(world.conn, world.service, assignment, review["run"], mission)
    assert "Exact-head review packet" in prompt
    assert "Durable reviewer lifecycle" in prompt
    assert "Pinned head: " + review["item"]["head_commit_oid"] in prompt
    assert prompts.CORE not in prompt and prompts.SESSION_HANDOFF not in prompt
    assert "Phase outcome:" not in prompt
    assert "Run coordination handoff" not in prompt
    continuation = goal(world.conn, world.service, assignment, review["run"], mission,
                        initial=False, findings=["Reconcile receipt for the submitted review"])
    assert "Reconcile receipt for the submitted review" in continuation
    assert "Exact-head review packet" not in continuation
    assert len(continuation) < 1000


def test_orchestrator_does_not_inherit_math_obligation_completion_gate(world):
    run = world.run(orchestrated=True)
    world.disable_automations(run)
    assignment = world.assignment(run, role="maintainer", functions=["orchestrator"])
    assert not world.service.completion_findings(world.conn, assignment["id"])
