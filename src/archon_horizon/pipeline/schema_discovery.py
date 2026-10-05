"""Resolve published GET templates and explain rejected schema lookups."""

from __future__ import annotations

import re
import shlex
from urllib.parse import urlsplit

from .errors import DomainError


def _route_parts(name):
    method, separator, target = name.partition(" ")
    if not separator or method != "GET":
        return None
    try:
        parsed = urlsplit(target)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/api/v3/"):
        return None
    return parsed


def canonical_query_name(contracts: dict, name: str) -> str | None:
    """Match only existing OpenAPI queries, preserving ambiguity and known kinds."""
    queries = contracts["queries"]
    if name in queries:
        return name
    requested = _route_parts(name)
    if requested is None:
        return None
    parts = requested.path.split("/")
    candidates = []
    for canonical in queries:
        route = _route_parts(canonical)
        if route is None:
            continue
        expected = route.path.split("/")
        if len(parts) != len(expected):
            continue
        for actual, template in zip(parts, expected, strict=True):
            if actual == template:
                continue
            parameter = re.fullmatch(r"\{([A-Za-z_][A-Za-z_0-9]*)\}", template)
            if parameter is None:
                break
            if re.fullmatch(r"\{[A-Za-z_][A-Za-z_0-9]*\}", actual):
                continue
            if parameter[1] == "kind" and actual in contracts["record_filters"]:
                continue
            break
        else:
            candidates.append(canonical)
    return candidates[0] if len(candidates) == 1 else None


def canonicalize_published_queries(contracts: dict) -> None:
    """Publish FastAPI's parameter names while retaining query examples."""
    for operation, route in contracts["routes"].items():
        canonical = canonical_query_name(contracts, route)
        if canonical:
            query = _route_parts(route).query
            contracts["routes"][operation] = canonical + ("?" + query if query else "")


def lookup_error(contracts: dict, section: str | None, name: str | None) -> DomainError:
    if not section:
        message = "name requires a schema section"
        allowed = sorted(contracts)
    elif section not in contracts:
        message = "Unknown schema section"
        allowed = sorted(contracts)
    else:
        message = "Unknown schema entry"
        selected = contracts[section]
        allowed = sorted(selected) if isinstance(selected, dict) else []

    target_section = section if section in contracts else "routes"
    target_name = None
    if name:
        routes = contracts["routes"]
        route_name = next((key for key, route in routes.items() if route == name), None)
        entry = route_name or name
        query_name = canonical_query_name(contracts, name) or canonical_query_name(contracts, routes.get(name, ""))
        if query_name:
            target_section, target_name = "queries", query_name
        elif route_name and name.startswith("GET "):
            target_section, target_name = "routes", route_name
        else:
            # Request-body contracts take precedence over the route with the same name.
            for candidate in ("operations", "command_args", "create"):
                if entry in contracts[candidate]:
                    target_section, target_name = candidate, entry
                    break
            else:
                if entry == "commands" and route_name:
                    target_section = "command"
                elif entry in contracts:
                    target_section = entry
                elif entry in routes:
                    target_section, target_name = "routes", entry

    args = ["horizon-pipeline", "agent", "schema", "--section", target_section]
    correction = {"section": target_section}
    if target_name is not None:
        args.extend(["--name", target_name])
        correction["name"] = target_name
    correction["command"] = shlex.join(args)
    return DomainError("invalid_query", f"{message}. Use: {correction['command']}", 422,
        correction=correction,
        tip="operations names endpoint request bodies; commands lists command names and targets; "
            "command_args names arguments for POST /api/v3/commands; create names record kinds; "
            "queries accepts published GET templates, equivalent parameter names, and known record kinds. "
            "Use --section routes to discover operation names and HTTP routes.",
        allowed=allowed)
