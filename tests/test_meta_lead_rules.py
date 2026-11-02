"""Meta Ads lead intake rules (Phase 3B-2). No database and no network needed."""

from datetime import UTC, datetime

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import BadRequestError
from app.services import meta_leads
from app.services.meta_leads import (
    MAPPABLE,
    SUGGESTED,
    GraphMetaClient,
    MetaClientError,
    answers_of,
    build_note,
    check_mapping,
    client_from_settings,
    meta_time,
    missing_settings,
)

PAYLOAD = {
    "id": "1234567890",
    "created_time": "2026-10-31T10:15:00+0000",
    "field_data": [
        {"name": "full_name", "values": ["Priya Nair"]},
        {"name": "email", "values": ["priya@acme.in"]},
        {"name": "when_do_you_need_space", "values": ["Next month", " "]},
        {"name": "desks", "values": ["4", "6"]},
        {"name": "", "values": ["ignored"]},
        "not a field",
    ],
}


def settings(**values: object) -> Settings:
    return Settings(
        database_url="postgresql://x@127.0.0.1:1/x",  # type: ignore[arg-type]
        supabase_url="http://127.0.0.1:9",  # type: ignore[arg-type]
        **values,  # type: ignore[arg-type]
    )


def test_answers_are_read_from_field_data() -> None:
    assert answers_of(PAYLOAD) == [
        ("full_name", "Priya Nair"),
        ("email", "priya@acme.in"),
        ("when_do_you_need_space", "Next month"),
        ("desks", "4, 6"),
    ]
    assert answers_of({}) == []


def test_meta_times() -> None:
    assert meta_time("2026-10-31T10:15:00+0000") == datetime(2026, 10, 31, 10, 15, tzinfo=UTC)
    assert meta_time("2026-10-31T10:15:00.123+0000") is not None
    assert meta_time("yesterday") is None and meta_time(None) is None


def test_mapping_rules() -> None:
    assert "owner_id" not in MAPPABLE  # approved: Meta leads stay unassigned
    assert set(SUGGESTED.values()) <= set(MAPPABLE)
    assert check_mapping({" email ": "contact_email", "desks": None}) == {"email": "contact_email"}
    with pytest.raises(BadRequestError, match="owner cannot be mapped"):
        check_mapping({"rep": "owner_id"})
    with pytest.raises(BadRequestError, match="Unknown lead field"):
        check_mapping({"x": "source"})
    with pytest.raises(BadRequestError, match="Map each field once"):
        check_mapping({"a": "contact_email", "b": "contact_email"})
    with pytest.raises(BadRequestError, match="1 to 200"):
        check_mapping({" ": "contact_email"})


def test_unmapped_answers_become_the_first_note() -> None:
    assert build_note(None, []) is None
    assert build_note("Call after 3", []) == "Call after 3"
    note = build_note("Call after 3", [("desks", "4, 6"), ("empty", "")])
    assert note == "Answers from the Meta lead form:\nCall after 3\ndesks: 4, 6"
    long = build_note(None, [("story", "x" * 6000)])
    assert long is not None and len(long) == 5000 and long.endswith("…")


def test_the_integration_is_off_until_configured() -> None:
    off = settings()
    assert missing_settings(off) == [
        "META_GRAPH_API_VERSION",
        "META_PAGE_ACCESS_TOKEN",
        "META_APP_SECRET",
    ]
    assert client_from_settings(off) is None
    on = settings(
        meta_graph_api_version="v0.0-test",
        meta_page_access_token="page-token-secret",
        meta_app_secret="app-secret",
    )
    assert missing_settings(on) == []
    assert isinstance(client_from_settings(on), GraphMetaClient)


def test_the_graph_client_pages_signs_and_never_leaks_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "after=2" in str(request.url):
            return httpx.Response(200, json={"data": [{"id": "2"}]})
        return httpx.Response(
            200,
            json={"data": [{"id": "1"}], "paging": {"next": "https://graph.example/next?after=2"}},
        )

    real = httpx.Client
    monkeypatch.setattr(
        meta_leads.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    client = GraphMetaClient(
        settings(
            meta_graph_api_version="v0.0-test",
            meta_page_access_token="page-token-secret",
            meta_app_secret="app-secret",
        )
    )
    since = datetime(2026, 10, 31, tzinfo=UTC)
    assert client.fetch_leads("555", since) == [{"id": "1"}, {"id": "2"}]
    first = seen[0].url
    assert first.path == "/v0.0-test/555/leads"
    assert first.params["appsecret_proof"] and first.params["appsecret_proof"] != "app-secret"
    assert str(int(since.timestamp())) in first.params["filtering"]

    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Invalid token page-token-secret",
                    "type": "OAuthException",
                    "code": 190,
                }
            },
        )

    monkeypatch.setattr(
        meta_leads.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(refused), **kw)
    )
    with pytest.raises(MetaClientError) as error:
        client.fetch_leads("555", None)
    assert str(error.value) == "Meta refused the request (HTTP 400, OAuthException code 190)."
    assert "page-token-secret" not in str(error.value)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    monkeypatch.setattr(
        meta_leads.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(down), **kw)
    )
    with pytest.raises(MetaClientError, match="could not be reached"):
        client.fetch_leads("555", None)
