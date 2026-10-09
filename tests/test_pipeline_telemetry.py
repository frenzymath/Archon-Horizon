from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
import pytest

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from archon_horizon.pipeline.operations.telemetry import RequestTelemetry, make_tracer_provider


def test_request_trace_uses_route_template_and_excludes_sensitive_input(caplog):
    provider = make_tracer_provider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    sent = []

    async def application(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/api/v3/assignments/{assignment_id}")
        await send({"type": "http.response.start", "status": 503, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"sensitive-body"}

    with caplog.at_level(logging.INFO):
        asyncio.run(RequestTelemetry(application, provider=provider)(
            {"type": "http", "method": "GET", "path": "/api/v3/assignments/private-id",
             "query_string": b"secret=private-query", "headers": [(b"authorization", b"private-token")]},
            receive, send))
    span, = exporter.get_finished_spans()
    assert span.name == "GET /api/v3/assignments/{assignment_id}"
    assert span.status.status_code.name == "ERROR"
    assert dict(sent[0]["headers"])[b"x-request-id"].decode() == span.attributes["horizon.request_id"]
    assert caplog.records[-1].trace_id == format(span.context.trace_id, "032x")
    assert "private" not in str(span.attributes) + caplog.text
    assert "sensitive-body" not in str(span.attributes) + caplog.text
    provider.shutdown()


def test_exception_trace_records_type_without_exception_text(caplog):
    provider = make_tracer_provider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    async def application(scope, receive, send):
        raise ValueError("private-token-in-provider-failure")

    async def noop(*args):
        pass

    with caplog.at_level(logging.INFO), pytest.raises(ValueError):
        asyncio.run(RequestTelemetry(application, provider=provider)(
            {"type": "http", "method": "GET", "path": "/unknown/private-value"}, noop, noop))
    span, = exporter.get_finished_spans()
    assert span.attributes["error.type"] == "ValueError"
    assert span.name == "GET unmatched"
    assert not span.events
    assert "private" not in str(span.attributes) + caplog.text
    provider.shutdown()
