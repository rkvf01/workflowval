from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from workflowval.spec import (
    SpecError,
    build_graph,
    load_spec,
    parse_spec,
    resolve_base_url,
    resolve_refs,
    spec_hash,
)

FIXTURES = Path(__file__).parent / "fixtures"
PETSTORE = str(FIXTURES / "petstore.json")
BOOKER = str(Path(__file__).parent.parent / "examples" / "restful-booker" / "openapi.json")


def minimal(**changes: Any) -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {},
        **changes,
    }


@pytest.mark.parametrize("source", [PETSTORE, BOOKER])
def test_loads_the_demo_specs_with_every_ref_resolved(source: str) -> None:
    spec = load_spec(source)
    assert spec["paths"]
    assert '"$ref"' not in json.dumps(spec["paths"])


def test_missing_file_gives_a_clear_error() -> None:
    with pytest.raises(SpecError, match=r"not found: nope\.json"):
        load_spec("nope.json")


def test_yaml_gets_a_clear_error() -> None:
    with pytest.raises(SpecError, match="convert YAML to JSON"):
        parse_spec("openapi: 3.0.3\n", "spec.yaml")


def test_non_object_json_is_refused() -> None:
    with pytest.raises(SpecError, match="must be a JSON object"):
        parse_spec("[]", "spec.json")


@pytest.mark.parametrize("version", ["2.0", "4.0.0"])
def test_only_openapi_3_is_supported(version: str) -> None:
    with pytest.raises(SpecError, match=r"only OpenAPI 3\.0 and 3\.1"):
        parse_spec(json.dumps(minimal(openapi=version)), "s.json")


def test_swagger_2_is_refused_clearly() -> None:
    with pytest.raises(SpecError, match="no 'openapi' field"):
        parse_spec(json.dumps({"swagger": "2.0"}), "s.json")


def test_invalid_spec_names_the_bad_path() -> None:
    bad = minimal(paths={"/pets": {"get": {"responses": "not-an-object"}}})
    with pytest.raises(SpecError, match=r"invalid spec at paths//pets/get/responses"):
        parse_spec(json.dumps(bad), "s.json")


def test_url_sources_are_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **_: Any) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(minimal()), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    assert load_spec("http://example.test/openapi.json")["info"]["title"] == "t"


def test_url_failure_gives_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **_: Any) -> httpx.Response:
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(SpecError, match="can't fetch spec"):
        load_spec("http://example.test/openapi.json")


def test_recursive_refs_do_not_loop_forever() -> None:
    spec = {
        "a": {"$ref": "#/components/schemas/Node"},
        "components": {
            "schemas": {"Node": {"properties": {"next": {"$ref": "#/components/schemas/Node"}}}}
        },
    }
    resolved = resolve_refs(spec)
    assert resolved["a"]["properties"]["next"] == {"$ref": "#/components/schemas/Node"}


@pytest.mark.parametrize(
    ("ref", "message"), [("other.json#/a", "only local"), ("#/missing/thing", "points at nothing")]
)
def test_bad_refs_are_refused(ref: str, message: str) -> None:
    with pytest.raises(SpecError, match=message):
        resolve_refs({"a": {"$ref": ref}})


def test_escaped_ref_paths_work() -> None:
    spec = {"a": {"$ref": "#/paths/~1pets"}, "paths": {"/pets": {"x": 1}}}
    assert resolve_refs(spec)["a"] == {"x": 1}


def test_hash_ignores_key_order_but_sees_changes() -> None:
    assert spec_hash({"a": 1, "b": 2}) == spec_hash({"b": 2, "a": 1})
    assert spec_hash({"a": 1}) != spec_hash({"a": 2})


def test_petstore_graph_links_add_pet_to_get_pet_by_id() -> None:
    graph = build_graph(load_spec(PETSTORE))
    assert any(
        (link.producer, link.field, link.consumer, link.parameter)
        == ("addPet", "id", "getPetById", "petId")
        for link in graph.links
    )
    assert graph.resources == ["pet", "store", "user"]
    operation = graph.operation("getPetById")
    assert operation is not None
    assert (operation.method, operation.path) == ("GET", "/pet/{petId}")
    assert operation.parameters[0].location == "path"
    assert operation.responses["200"]["schema"]["properties"]["name"]


def test_booker_links_use_the_resource_id_field() -> None:
    graph = build_graph(load_spec(BOOKER))
    assert any(
        (link.producer, link.field, link.consumer) == ("createBooking", "bookingid", "getBooking")
        for link in graph.links
    )
    assert graph.security_schemes["cookieToken"]["in"] == "cookie"


def test_path_level_parameters_are_inherited() -> None:
    graph = build_graph(load_spec(BOOKER))
    operation = graph.operation("deleteBooking")
    assert operation is not None
    assert [parameter.name for parameter in operation.parameters] == ["id"]
    assert operation.security == ["cookieToken"]


def test_operations_without_an_id_get_one() -> None:
    spec = minimal(paths={"/a/{b}": {"get": {"responses": {"200": {"description": "ok"}}}}})
    assert build_graph(spec).operations[0].operation_id == "get_a_b"


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://h:1", "http://h:1/api/v3"),
        ("http://h:1/", "http://h:1/api/v3"),
        ("http://h:1/api/v3", "http://h:1/api/v3"),
        ("http://h:1/api/v3/", "http://h:1/api/v3"),
    ],
)
def test_base_url_gets_the_server_path_once(base_url: str, expected: str) -> None:
    graph = build_graph(minimal(servers=[{"url": "/api/v3"}]))
    assert resolve_base_url(base_url, graph) == expected


def test_absolute_server_urls_contribute_their_path() -> None:
    graph = build_graph(minimal(servers=[{"url": "https://prod.example.com/v2"}]))
    assert resolve_base_url("http://localhost:1", graph) == "http://localhost:1/v2"


def test_no_servers_leaves_the_base_url_alone() -> None:
    assert resolve_base_url("http://h:1/", build_graph(minimal())) == "http://h:1"
