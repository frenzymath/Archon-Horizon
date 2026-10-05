import json
from datetime import datetime, timezone

import httpx
from sqlalchemy import update

from archon_horizon.pipeline.auth import Actor
from archon_horizon.pipeline.models import ForgeEdit
from archon_horizon.pipeline.records import create, get, snapshot, transaction_lock
from archon_horizon.pipeline.reviews import queue_edit
from archon_horizon.pipeline.schema import tables
from test_pipeline_connectors import connector_world, manager


def test_retarget_lost_response_reconciles_once_and_invalidates_gate(connector_world):
    world = connector_world
    head = "a" * 40
    now = datetime.now(timezone.utc)
    actor = Actor(world.actor["id"], "human", {"username":"maintainer"}, "api_key")
    with world.database.transaction() as conn:
        transaction_lock(conn)
        item = create(conn,"forge_item",repository_id=world.repository["id"],remote_number=1,kind="pull_request",
            title="Proof",status="open",head_commit_oid=head,target_branch="staging",observed_at=now)
        policy = create(conn,"review_policy",project_id=world.project["id"],slug="library",phases=["postprocessing"],instructions="Review")
        revision = snapshot(conn,"review_policy",policy,actor.id)
        review = create(conn,"forge_review",forge_item_id=item["id"],remote_id="1",reviewer_remote_id="1",
            verdict="approved",summary="Old diff",commit_oid=head,observed_at=now)
        gate = create(conn,"review_gate",forge_item_id=item["id"],policy_id=policy["id"],policy_revision_id=revision,
            maintainer_review_id=review["id"],status="accepted",accepted_commit_oid=head,evaluated_at=now)
        operation = queue_edit(conn,actor,ForgeEdit(forge_item_id=item["id"],expected_head_oid=head,
            expected_base="staging",base="main"),"retarget-once")
    pull = {"number":1,"title":"Proof","state":"open","head":{"sha":head},"base":{"ref":"staging"},"user":{"id":1}}
    patches = []

    def handler(request):
        if request.method == "PATCH":
            patches.append(request)
            pull["base"]["ref"] = json.loads(request.content)["base"]
            raise httpx.ReadTimeout("accepted, response lost",request=request)
        return httpx.Response(200,json=pull)

    connector = manager(world,handler)
    assert connector.dispatch_one() == "uncertain"
    with world.database.transaction() as conn:
        conn.execute(update(tables["outbox_operation"]).where(tables["outbox_operation"].c.id==operation["id"]).values(retry_at=None))
    assert connector.dispatch_one() == "completed"
    assert len(patches) == 1
    with world.database.transaction() as conn:
        assert get(conn,"forge_item",item["id"])["target_branch"] == "main"
        assert get(conn,"review_gate",gate["id"])["status"] == "stale"
