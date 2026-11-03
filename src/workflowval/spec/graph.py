"""The internal graph: resources, operations, parameters and producer-to-consumer links (FR-3)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
_PATH_PARAM = re.compile(r"\{([^{}]+)\}")


class Parameter(BaseModel):
    """One operation parameter."""

    name: str
    location: str
    required: bool = False
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")


class Operation(BaseModel):
    """One API call."""

    operation_id: str
    method: str
    path: str
    resource: str
    summary: str = ""
    description: str = ""
    parameters: list[Parameter] = Field(default_factory=list)
    request_schema: dict[str, Any] | None = None
    responses: dict[str, dict[str, Any]] = Field(default_factory=dict)
    security: list[str] = Field(default_factory=list)


class Link(BaseModel):
    """A value one operation returns that another operation consumes."""

    producer: str
    field: str
    consumer: str
    parameter: str


class SpecGraph(BaseModel):
    """Everything the agents and the test generator need to know about an API."""

    title: str = ""
    base_path: str = ""
    resources: list[str] = Field(default_factory=list)
    operations: list[Operation] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    security_schemes: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def operation_at(self, method: str, path: str) -> Operation | None:
        """Find an operation by its method and path template."""
        wanted = method.upper()
        return next(
            (op for op in self.operations if op.path == path and op.method.upper() == wanted),
            None,
        )

    def operation(self, operation_id: str) -> Operation | None:
        """Find an operation by its id."""
        return next((op for op in self.operations if op.operation_id == operation_id), None)


def _synthetic_id(method: str, path: str) -> str:
    return "_".join([method, *re.findall(r"[A-Za-z0-9]+", path)])


def _json_schema(content: dict[str, Any] | None) -> dict[str, Any] | None:
    for media_type, media in (content or {}).items():
        if "json" in media_type:
            schema = media.get("schema")
            return schema if isinstance(schema, dict) else {}
    return None


def _build_operation(
    path: str, method: str, raw: dict[str, Any], inherited: list[Any]
) -> Operation:
    merged = {(p["name"], p["in"]): p for p in [*inherited, *raw.get("parameters", [])]}
    segments = [part for part in path.split("/") if part and not part.startswith("{")]
    request = raw.get("requestBody")
    responses = {
        str(status): {
            "description": response.get("description", ""),
            "schema": _json_schema(response.get("content")),
        }
        for status, response in raw.get("responses", {}).items()
    }
    return Operation(
        operation_id=raw.get("operationId") or _synthetic_id(method, path),
        method=method.upper(),
        path=path,
        resource=segments[0] if segments else "",
        summary=raw.get("summary", ""),
        description=raw.get("description", ""),
        parameters=[
            Parameter(
                name=p["name"],
                location=p["in"],
                required=bool(p.get("required")),
                schema=p.get("schema") or {},
            )
            for p in merged.values()
        ],
        request_schema=_json_schema(request.get("content")) if request else None,
        responses=responses,
        security=[name for option in raw.get("security", []) for name in option],
    )


def _is_success(status: str) -> bool:
    return status == "default" or status.startswith("2")


def _returned_fields(operation: Operation) -> set[str]:
    fields: set[str] = set()
    for status, response in operation.responses.items():
        if _is_success(status):
            fields |= set((response.get("schema") or {}).get("properties", {}))
    return fields


def _singular(word: str) -> str:
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def _matching_field(fields: set[str], parameter: str, collection: str) -> str | None:
    """Find the returned field that feeds a path parameter, most specific first."""
    resource = _singular(collection.rstrip("/").rsplit("/", 1)[-1]).lower()
    wanted = (parameter.lower(), f"{resource}id", f"{resource}_id", "id")
    by_lower = {field.lower(): field for field in fields}
    return next((by_lower[name] for name in wanted if name in by_lower), None)


def _build_links(operations: list[Operation]) -> list[Link]:
    links = []
    for consumer in operations:
        for match in _PATH_PARAM.finditer(consumer.path):
            collection = consumer.path[: match.start()].rstrip("/")
            for producer in operations:
                if producer.method != "POST" or producer.path.rstrip("/") != collection:
                    continue
                field = _matching_field(_returned_fields(producer), match.group(1), collection)
                if field:
                    links.append(
                        Link(
                            producer=producer.operation_id,
                            field=field,
                            consumer=consumer.operation_id,
                            parameter=match.group(1),
                        )
                    )
    return links


def build_graph(spec: dict[str, Any]) -> SpecGraph:
    """Build the graph from a spec whose `$ref`s are already resolved."""
    operations = []
    for path, item in spec.get("paths", {}).items():
        for method in _METHODS:
            if method in item:
                inherited = item.get("parameters", [])
                operations.append(_build_operation(path, method, item[method], inherited))
    servers = spec.get("servers") or [{}]
    return SpecGraph(
        title=spec.get("info", {}).get("title", ""),
        base_path=urlparse(servers[0].get("url", "")).path.rstrip("/"),
        resources=sorted({op.resource for op in operations if op.resource}),
        operations=operations,
        links=_build_links(operations),
        security_schemes=spec.get("components", {}).get("securitySchemes", {}),
    )


def resolve_base_url(base_url: str, graph: SpecGraph) -> str:
    """Add the spec's server path to a base URL, unless the URL already ends with it."""
    url = base_url.rstrip("/")
    if graph.base_path and not urlparse(url).path.rstrip("/").endswith(graph.base_path):
        return url + graph.base_path
    return url
