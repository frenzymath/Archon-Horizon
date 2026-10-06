import asyncio
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from archon_horizon.pipeline.api import create_app
from test_pipeline_api import api, api_database


def test_idle_event_stream_exits_immediately_on_server_shutdown(api):
    client, _, world, _, token, _ = api
    app = client.app
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/v3/events")

    async def check():
        async def receive():
            await asyncio.Event().wait()
        request = Request({"type": "http", "method": "GET", "path": "/api/v3/events", "query_string": b"",
            "headers": [(b"authorization", ("Bearer " + token).encode())]}, receive=receive)
        response = await endpoint(request, after=0, project_id=world.project["id"])
        iterator = response.body_iterator
        assert "event: gap" in await anext(iterator)
        assert "keepalive" in await anext(iterator)
        waiting = asyncio.create_task(anext(iterator))
        await asyncio.sleep(0)
        app.state.shutdown_event.set()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(waiting, timeout=0.5)

    asyncio.run(check())


def test_slow_forge_sync_does_not_block_zulip_or_delivery(api, monkeypatch):
    from archon_horizon.pipeline import connectors, storage
    from archon_horizon.pipeline.scheduler import Scheduler
    _, database, world, _, _, _ = api
    forge_entered, release_forge, zulip_polled, delivered = (threading.Event() for _ in range(4))

    class FakeManager:
        def __init__(self, *args, **kwargs):
            pass

        def sync_all(self, *, kind):
            if kind == "forge":
                forge_entered.set()
                assert release_forge.wait(timeout=5)
            else:
                assert kind == "zulip"
                zulip_polled.set()
            return {kind: "current"}

        def dispatch_one(self):
            delivered.set()

    monkeypatch.setattr(connectors, "ConnectorManager", FakeManager)
    monkeypatch.setattr(Scheduler, "tick", lambda *args: None)
    monkeypatch.setattr(storage, "database_retention_preview", lambda *args: {})
    monkeypatch.setattr(storage, "apply_database_retention", lambda *args: None)
    app = create_app(world.service.config, database=database, background=True)
    with TestClient(app):
        try:
            assert forge_entered.wait(timeout=2)
            assert zulip_polled.wait(timeout=2)
            assert delivered.wait(timeout=2)
        finally:
            release_forge.set()
