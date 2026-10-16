"""Uploaded images and documents: validation, storage, signed links and deletion."""

import json
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.endpoints.files import get_storage
from app.core.config import get_settings
from app.main import app
from app.models import ActivityLog, StoredFile
from app.services.files import clean_name, sniff
from app.storage import LocalStorage, StorageError, SupabaseStorage
from tests import images
from tests.test_properties import create_property
from tests.test_resources import V1, post

FILES = f"{V1}/files"
PNG = images.png()
JPEG = images.jpeg()
WEBP = images.webp()
PDF = images.pdf()


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[LocalStorage]:
    local = LocalStorage(tmp_path, "http://testserver")
    app.dependency_overrides[get_storage] = lambda: local
    yield local
    app.dependency_overrides.pop(get_storage, None)


@pytest.fixture
def place(api: TestClient) -> dict:
    prop = create_property(api)
    building = post(api, "buildings", {"property_id": prop["id"], "name": "Tower A"})
    floor = post(api, "floors", {"building_id": building["id"], "name": "Fifth", "level_number": 5})
    root = post(
        api,
        "spaces",
        {
            "property_id": prop["id"],
            "floor_id": floor["id"],
            "name": "Fifth",
            "space_type": "FLOOR",
        },
    )
    cabin = post(
        api,
        "spaces",
        {
            "property_id": prop["id"],
            "parent_space_id": root["id"],
            "name": "Cabin 1",
            "space_type": "CABIN",
        },
    )
    layout = post(
        api,
        "layouts",
        {
            "property_id": prop["id"],
            "building_id": building["id"],
            "floor_id": floor["id"],
            "name": "Fifth floor plan",
        },
    )
    return {"property": prop, "building": building, "space": cabin, "layout": layout}


def upload(
    api: TestClient,
    content: bytes,
    kind: str,
    expected: int = 201,
    name: str = "photo.png",
    **owners: str,
) -> dict:
    response = api.post(
        FILES,
        data={"kind": kind, **owners},
        files={"file": (name, content, "application/octet-stream")},
    )
    assert response.status_code == expected, response.text
    return response.json()


def test_building_and_space_images_are_stored_and_listed(
    api: TestClient, storage: LocalStorage, place: dict, db_session: Session
) -> None:
    prop_id = place["property"]["id"]
    building = upload(
        api,
        PNG,
        "BUILDING",
        property_id=prop_id,
        building_id=place["building"]["id"],
        name="front.png",
    )
    space = upload(
        api,
        JPEG,
        "INTERIOR",
        property_id=prop_id,
        space_id=place["space"]["id"],
        name="C:\\photos\\cabin.jpg",
    )

    assert building["content_type"] == "image/png"
    assert building["building_id"] == place["building"]["id"]
    assert building["size_bytes"] == len(PNG)
    assert space["content_type"] == "image/jpeg"  # read from the bytes, not the name
    assert space["original_name"] == "cabin.jpg"  # no client path kept

    # The bytes are really in storage, and the signed link returns them.
    stored = db_session.scalars(select(StoredFile).order_by(StoredFile.created_at)).all()
    assert [s.object_path.split("/")[1] for s in stored] == ["building", "interior"]
    assert (storage.root / stored[0].object_path).read_bytes() == PNG
    fetched = api.get(space["url"].removeprefix("http://testserver"))
    assert fetched.status_code == 200
    assert fetched.content == JPEG

    def listed(**params: str) -> list[str]:
        return [f["id"] for f in api.get(FILES, params=params).json()["items"]]

    assert listed(property_id=prop_id) == [building["id"], space["id"]]
    assert listed(building_id=place["building"]["id"]) == [building["id"]]
    assert listed(space_id=place["space"]["id"]) == [space["id"]]
    assert listed(kind="INTERIOR") == [space["id"]]
    assert api.get(f"{FILES}/{space['id']}").json()["original_name"] == "cabin.jpg"
    assert (
        db_session.scalar(
            select(func.count()).select_from(ActivityLog).where(ActivityLog.action == "uploaded")
        )
        == 2
    )


def test_floor_plans_may_be_pdf_but_photos_may_not(
    api: TestClient, storage: LocalStorage, place: dict
) -> None:
    prop_id = place["property"]["id"]
    plan = upload(
        api,
        PDF,
        "FLOOR_PLAN",
        property_id=prop_id,
        layout_id=place["layout"]["id"],
        name="sketch.pdf",
    )
    assert plan["content_type"] == "application/pdf"
    assert plan["layout_id"] == place["layout"]["id"]
    upload(api, WEBP, "FLOOR_PLAN", property_id=prop_id, layout_id=place["layout"]["id"])

    refused = upload(api, PDF, "BUILDING", 415, property_id=prop_id)
    assert refused["detail"] == (
        "Upload a complete JPG, PNG or WebP image. The file's content is not one, or is damaged."
    )
    # A renamed text file is caught by its content.
    upload(api, b"just text, not an image", "GALLERY", 415, name="fake.png", property_id=prop_id)


def test_invalid_uploads_are_rejected_and_nothing_is_stored(
    api: TestClient, storage: LocalStorage, place: dict, db_session: Session
) -> None:
    prop_id = place["property"]["id"]
    other = create_property(api, name="Other Tower")
    other_building = post(api, "buildings", {"property_id": other["id"], "name": "Elsewhere"})

    assert upload(api, b"", "GALLERY", 400, property_id=prop_id)["detail"] == "The file is empty."
    mismatch = upload(
        api, PNG, "BUILDING", 400, property_id=prop_id, building_id=other_building["id"]
    )
    assert mismatch["detail"] == "The building does not belong to this property."
    missing = upload(
        api,
        PNG,
        "INTERIOR",
        404,
        property_id=prop_id,
        space_id="00000000-0000-0000-0000-000000000000",
    )
    assert missing["detail"] == "Space not found."
    assert api.post(FILES, data={"kind": "GALLERY", "property_id": prop_id}).status_code == 422
    assert upload(api, PNG, "SELFIE", 422, property_id=prop_id)["code"] == "validation_error"

    assert db_session.scalar(select(func.count()).select_from(StoredFile)) == 0
    assert not any(storage.root.rglob("*.*"))


def test_upload_size_limit(api: TestClient, storage: LocalStorage, place: dict) -> None:
    small = get_settings().model_copy(update={"max_upload_mb": 1})
    app.dependency_overrides[get_settings] = lambda: small
    try:
        too_big = PNG + b"\x00" * (1024 * 1024)
        refused = upload(api, too_big, "GALLERY", 413, property_id=place["property"]["id"])
        assert refused["detail"] == "The file is larger than 1 MB."
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_delete_removes_the_record_and_the_bytes(
    api: TestClient, storage: LocalStorage, place: dict, db_session: Session
) -> None:
    uploaded = upload(api, PNG, "GALLERY", property_id=place["property"]["id"])
    path = db_session.scalar(select(StoredFile.object_path))
    assert (storage.root / path).exists()

    assert api.delete(f"{FILES}/{uploaded['id']}").status_code == 204
    assert not (storage.root / path).exists()
    assert api.get(f"{FILES}/{uploaded['id']}").status_code == 404
    assert api.delete(f"{FILES}/{uploaded['id']}").status_code == 404


def test_signed_links_cannot_be_forged_or_reused_after_expiry(
    api: TestClient, storage: LocalStorage, place: dict, db_session: Session
) -> None:
    uploaded = upload(api, PNG, "GALLERY", property_id=place["property"]["id"])
    url = uploaded["url"].removeprefix("http://testserver")
    assert api.get(url).status_code == 200
    # Links are public on purpose (an <img> cannot send a token); the signature is the lock.
    assert api.get(url[:-4] + "0000").status_code == 404
    path = db_session.scalar(select(StoredFile.object_path))
    old = storage.signed_url(path, -5).url.removeprefix("http://testserver")
    assert api.get(old).status_code == 404
    assert api.get(
        f"{FILES}/local/../../etc/passwd?expires=9999999999&signature=x"
    ).status_code in (
        404,
        422,
    )


def test_a_storage_failure_stores_no_record(
    api: TestClient, place: dict, db_session: Session
) -> None:
    class Broken(LocalStorage):
        def put(self, path: str, data: bytes, content_type: str) -> None:
            raise StorageError("File storage answered 500.")

    app.dependency_overrides[get_storage] = lambda: Broken(Path("unused"), "http://testserver")
    try:
        refused = upload(api, PNG, "GALLERY", 503, property_id=place["property"]["id"])
    finally:
        app.dependency_overrides.pop(get_storage, None)
    assert refused["detail"] == "The file was not stored: File storage answered 500."
    assert db_session.scalar(select(func.count()).select_from(StoredFile)) == 0


def test_without_storage_configured_uploads_answer_503(api: TestClient, place: dict) -> None:
    # The test settings use Supabase Storage with no secret key.
    refused = upload(api, PNG, "GALLERY", 503, property_id=place["property"]["id"])
    assert "not configured" in refused["detail"]
    assert api.get(FILES).status_code == 503


def test_files_require_an_admin(client: TestClient) -> None:
    assert client.get(FILES).status_code == 401
    assert client.post(FILES, data={"kind": "GALLERY"}).status_code == 401


# --- unit level -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (WEBP, "image/webp"),
        (PDF, "application/pdf"),
        (b"GIF89a", None),
        (b"<svg/>", None),
        (b"", None),
    ],
)
def test_content_type_comes_from_the_bytes(data: bytes, expected: str | None) -> None:
    assert sniff(data) == expected


def test_file_names_are_display_only() -> None:
    assert clean_name("../../etc/passwd") == "passwd"
    assert clean_name("a\x00b\nc.png") == "abc.png"
    assert clean_name("") == "file"
    assert len(clean_name("x" * 400)) == 255


def test_supabase_storage_calls_the_storage_api() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "/object/sign/" in request.url.path:
            assert json.loads(request.content) == {"expiresIn": 600}
            return httpx.Response(200, json={"signedURL": "/object/sign/b/p/x.png?token=abc"})
        return httpx.Response(200, json={})

    storage = SupabaseStorage(
        "https://ref.supabase.co", "sb_secret_test", "b", transport=httpx.MockTransport(handler)
    )
    storage.put("p/x.png", PNG, "image/png")
    link = storage.signed_url("p/x.png", 600)
    storage.delete("p/x.png")

    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/storage/v1/object/b/p/x.png"),
        ("POST", "/storage/v1/object/sign/b/p/x.png"),
        ("DELETE", "/storage/v1/object/b/p/x.png"),
    ]
    assert seen[0].headers["apikey"] == "sb_secret_test"
    assert "authorization" not in seen[0].headers  # a new-style key is not a JWT
    assert seen[0].headers["content-type"] == "image/png"
    assert seen[0].content == PNG
    assert link.url == "https://ref.supabase.co/storage/v1/object/sign/b/p/x.png?token=abc"
    assert link.expires_at > time.time()


def test_supabase_storage_errors_do_not_leak_details() -> None:
    storage = SupabaseStorage(
        "https://ref.supabase.co",
        "legacy.jwt.key",
        "b",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(404, json={"message": "Bucket not found: b"})
        ),
    )
    with pytest.raises(StorageError) as raised:
        storage.put("p/x.png", PNG, "image/png")
    assert str(raised.value) == "File storage answered 404."
