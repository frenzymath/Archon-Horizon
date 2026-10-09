"""Bounded server tracing without request bodies, credentials, or raw URLs."""
from __future__ import annotations

import logging
from time import monotonic
from uuid import uuid4

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.trace import SpanKind, Status, StatusCode

log = logging.getLogger(__name__)


def make_tracer_provider() -> TracerProvider:
    # Exporters are deliberately opt-in; request handling never needs a collector.
    return TracerProvider(
        resource=Resource({"service.name": "archon-horizon-pipeline"}),
        span_limits=SpanLimits(max_attributes=16, max_events=0, max_links=0,
                               max_attribute_length=256),
    )


class RequestTelemetry:
    def __init__(self, app, *, provider: TracerProvider):
        self.app = app
        self.tracer = provider.get_tracer("archon_horizon.pipeline")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id, started, status = str(uuid4()), monotonic(), 500
        method = scope.get("method", "OTHER")
        method = method if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "OTHER"
        with self.tracer.start_as_current_span(
            "HTTP " + method, kind=SpanKind.SERVER, record_exception=False,
            set_status_on_exception=False, attributes={"http.request.method": method,
                                                      "horizon.request_id": request_id},
        ) as span:
            async def tracked_send(message):
                nonlocal status
                if message["type"] == "http.response.start":
                    status = int(message["status"])
                    headers = [(key, value) for key, value in message.get("headers", [])
                               if key.lower() != b"x-request-id"]
                    message = {**message, "headers": [*headers, (b"x-request-id", request_id.encode())]}
                await send(message)

            try:
                await self.app(scope, receive, tracked_send)
            except BaseException as error:
                span.set_attribute("error.type", type(error).__name__[:128])
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                # The router adds its template after matching; unmatched paths stay generic.
                route = str(getattr(scope.get("route"), "path", "unmatched"))[:256]
                span.update_name(f"{method} {route}")
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status)
                if status >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                log.info("HTTP request completed", extra={
                    "request_id": request_id,
                    "trace_id": format(span.get_span_context().trace_id, "032x"),
                    "http_method": method, "http_route": route, "http_status_code": status,
                    "duration_ms": round((monotonic() - started) * 1000, 3),
                })
