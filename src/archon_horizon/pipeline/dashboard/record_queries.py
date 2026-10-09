"""Explicit, discoverable filters for generic record listings."""

from sqlalchemy import select

from ..errors import DomainError
from ..persistence.records import SCOPE_PARENT
from ..persistence.schema import tables


SCOPES = ("project", "repository", "mission", "run", "assignment", "discussion", "forge_item")


def scope_expression(kind, scope, identifier):
    table = tables[kind]
    if kind == scope:
        return table.c.id == identifier
    key = f"{scope}_id"
    if key in table.c:
        return table.c[key] == identifier
    if kind == "publication" and scope in {"assignment", "run", "mission"}:
        parent, key = "assignment", "requested_by_assignment_id"
    elif kind == "outbox_operation" and scope in {"assignment", "run", "mission"}:
        principal, execution = tables["principal"], tables["execution"]
        expression = scope_expression("execution", scope, identifier)
        return table.c.actor_principal_id.in_(select(principal.c.id).join(execution,
            principal.c.execution_id == execution.c.id).where(expression))
    elif kind in SCOPE_PARENT:
        parent, key = SCOPE_PARENT[kind]
    else:
        raise DomainError("unsupported_filter", f"{kind} does not support {scope}_id", 422)
    expression = scope_expression(parent, scope, identifier)
    return table.c[key].in_(select(tables[parent].c.id).where(expression))


def supported_filters(kind):
    result = ["cursor", "limit"]
    for scope in SCOPES:
        try:
            scope_expression(kind, scope, None)
        except DomainError:
            continue
        result.append(f"{scope}_id")
    if "status" in tables[kind].c:
        result.append("status")
    return result


def validate_query(request, allowed):
    unknown = set(request.query_params) - set(allowed)
    repeated = sorted(key for key in request.query_params if len(request.query_params.getlist(key)) != 1)
    if unknown or repeated:
        raise DomainError("invalid_query", "Unsupported or repeated query parameters", 422,
                          unknown=sorted(unknown), repeated=repeated, allowed=sorted(allowed))
