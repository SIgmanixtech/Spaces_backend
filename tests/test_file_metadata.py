"""Revision 0003 of files: content checks, captions and order, one primary file
per owner, asset images, property covers, per-owner limits and orphan cleanup."""

# The storage and place fixtures are imported from test_files and used as arguments.
# ruff: noqa: F811

import json
import os
import struct
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import app
from app.models import StoredFile
from app.services.files import MAX_PIXELS, FileService, clean_text, inspect_content
from app.storage import LocalStorage, SupabaseStorage
from tests import images
from tests.test_files import FILES, place, storage, upload  # noqa: F401
from tests.test_properties import create_property
from tests.test_resources import V1, post

PNG = images.png()
PDF = images.pdf()


def ids(response: httpx.Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [f["id"] for f in response.json()["items"]]


# --- content inspection (unit) ---------------------------------------------------


@pytest.mark.parametrize(
    ("data", "content_type", "size"),
    [
        (images.png(7, 3), "image/png", (7, 3)),
        (images.jpeg(640, 480), "image/jpeg", (640, 480)),
        (images.webp(1200, 800), "image/webp", (1200, 800)),
        (PDF, "application/pdf", (None, None)),
    ],
)
def test_real_files_are_recognised_with_their_size(
    data: bytes, content_type: str, size: tuple[int | None, int | None]
) -> None:
    content = inspect_content(data)
    assert content is not None
    assert (content.content_type, content.width, content.height) == (content_type, *size)


@pytest.mark.parametrize(
    "data",
    [
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,  # signature only
        images.png()[:-12],  # no IEND: truncated
        b"\xff\xd8\xff\xe0" + b"\x00" * 64,  # signature only
        images.jpeg()[:-2],  # no end-of-image marker
        b"\xff\xd8\xff\xda\x00\x02" + b"\x00" * 8 + b"\xff\xd9",  # scan before any frame
        images.webp()[:-4],  # shorter than its RIFF size
        b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64,
        b"%PDF-1.7\n" + b"\x00" * 64,  # no %%EOF
        images.png(0, 4),  # zero width
        b"MZ\x90\x00" + b"\x00" * 64,  # a Windows executable
        b"GIF89a" + b"\x00" * 64,
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",
    ],
)
def test_incomplete_damaged_or_other_files_are_refused(data: bytes) -> None:
    assert inspect_content(data) is None


def test_display_text_is_cleaned() -> None:
    assert clean_text("  Lobby\x00 and\t\treception \n", 300) == "Lobby and reception"
    assert clean_text("   ", 300) is None
    assert clean_text(None, 300) is None
    assert clean_text("x" * 400, 300) == "x" * 300
    assert clean_text("Line one\r\n\x07Line   two", 2000, multiline=True) == "Line one\nLine two"


# --- upload and metadata ----------------------------------------------------------


def test_an_oversized_image_in_pixels_is_refused(
    api: TestClient, storage: LocalStorage, place: dict
) -> None:
    side = int(MAX_PIXELS**0.5) + 1
    # A small file whose header claims a huge picture: the claim alone is refused.
    huge = bytearray(images.png())
    huge[16:24] = struct.pack(">II", side, side)
    refused = upload(api, bytes(huge), "GALLERY", 415, property_id=place["property"]["id"])
    assert "megapixels" in refused["detail"]
    assert not any(storage.root.rglob("*.*"))


def test_upload_keeps_caption_category_order_and_size(
    api: TestClient, storage: LocalStorage, place: dict
) -> None:
    response = api.post(
        FILES,
        data={
            "kind": "BUILDING",
            "property_id": place["property"]["id"],
            "building_id": place["building"]["id"],
            "caption": "  Main\x00 entrance  ",
            "description": "Facing the ring road.\nPhotographed in 2026.",
            "category": "Entrance",
            "sort_order": "3",
        },
        files={"file": ("entrance.png", images.png(40, 30), "image/png")},
    )
    assert response.status_code == 201, response.text
    stored = response.json()
    assert stored["caption"] == "Main entrance"
    assert stored["description"] == "Facing the ring road.\nPhotographed in 2026."
    assert stored["category"] == "Entrance"
    assert stored["sort_order"] == 3
    assert (stored["width_px"], stored["height_px"]) == (40, 30)
    assert stored["is_primary"] is False  # only MAIN is primary unless asked


def test_one_primary_file_per_owner(api: TestClient, storage: LocalStorage, place: dict) -> None:
    prop_id = place["property"]["id"]
    building = {"property_id": prop_id, "building_id": place["building"]["id"]}
    first = upload(api, PNG, "MAIN", property_id=prop_id)
    assert first["is_primary"] is True  # MAIN is the cover by default
    second = upload(api, PNG, "MAIN", property_id=prop_id)
    assert second["is_primary"] is True
    gallery = upload(api, PNG, "GALLERY", property_id=prop_id)
    # The building is another owner: its cover does not touch the property's.
    building_cover = api.post(
        FILES,
        data={"kind": "BUILDING", "is_primary": "true", **building},
        files={"file": ("b.png", PNG, "image/png")},
    ).json()
    assert building_cover["is_primary"] is True

    primaries = ids(api.get(FILES, params={"property_id": prop_id, "is_primary": True}))
    assert sorted(primaries) == sorted([second["id"], building_cover["id"]])

    # Switching the cover clears the old one in the same request.
    switched = api.patch(f"{FILES}/{gallery['id']}", json={"is_primary": True})
    assert switched.status_code == 200 and switched.json()["is_primary"] is True
    assert api.get(f"{FILES}/{second['id']}").json()["is_primary"] is False
    assert api.get(f"{FILES}/{building_cover['id']}").json()["is_primary"] is True

    unset = api.patch(f"{FILES}/{gallery['id']}", json={"is_primary": False})
    assert unset.json()["is_primary"] is False
    assert ids(api.get(FILES, params={"property_id": prop_id, "is_primary": True})) == [
        building_cover["id"]
    ]


def test_the_database_allows_one_primary_per_owner(
    api: TestClient, storage: LocalStorage, place: dict, db_session: Session
) -> None:
    prop_id = place["property"]["id"]
    upload(api, PNG, "MAIN", property_id=prop_id)
    other = upload(api, PNG, "GALLERY", property_id=prop_id)
    with pytest.raises(IntegrityError, match="uq_files_primary_per_owner"):
        db_session.execute(
            text("UPDATE files SET is_primary = true WHERE id = :id"), {"id": other["id"]}
        )
        db_session.flush()
    db_session.rollback()


def test_documents_cannot_be_primary(api: TestClient, storage: LocalStorage, place: dict) -> None:
    prop_id = place["property"]["id"]
    response = api.post(
        FILES,
        data={"kind": "DOCUMENT", "property_id": prop_id, "is_primary": "true"},
        files={"file": ("lease.pdf", PDF, "application/pdf")},
    )
    assert response.status_code == 400
    doc = upload(api, PDF, "DOCUMENT", property_id=prop_id, name="lease.pdf")
    assert api.patch(f"{FILES}/{doc['id']}", json={"is_primary": True}).status_code == 400


def test_update_changes_only_display_details(
    api: TestClient, storage: LocalStorage, place: dict
) -> None:
    stored = upload(api, PNG, "GALLERY", property_id=place["property"]["id"])
    changed = api.patch(
        f"{FILES}/{stored['id']}",
        json={"caption": "Reception", "category": "Lobby", "sort_order": 2},
    )
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert (body["caption"], body["category"], body["sort_order"]) == ("Reception", "Lobby", 2)
    assert body["url"]  # a fresh signed link

    cleared = api.patch(f"{FILES}/{stored['id']}", json={"caption": None}).json()
    assert cleared["caption"] is None and cleared["category"] == "Lobby"

    for invalid in (
        {"sort_order": None},
        {"sort_order": -1},
        {"is_primary": None},
        {"kind": "MAIN"},  # the kind, owner and bytes never change
        {"space_id": place["space"]["id"]},
        {"caption": "x" * 301},
    ):
        assert api.patch(f"{FILES}/{stored['id']}", json=invalid).status_code == 422, invalid
    missing = "00000000-0000-0000-0000-000000000000"
    assert api.patch(f"{FILES}/{missing}", json={"caption": "x"}).status_code == 404


def test_lists_follow_sort_order_and_can_be_limited_to_one_owner(
    api: TestClient, storage: LocalStorage, place: dict
) -> None:
    prop_id = place["property"]["id"]
    late = upload(api, PNG, "GALLERY", property_id=prop_id)
    early = upload(api, PNG, "GALLERY", property_id=prop_id)
    api.patch(f"{FILES}/{late['id']}", json={"sort_order": 5})
    space_photo = upload(api, PNG, "INTERIOR", property_id=prop_id, space_id=place["space"]["id"])

    assert ids(api.get(FILES, params={"property_id": prop_id})) == [
        early["id"],
        space_photo["id"],
        late["id"],
    ]
    # direct: the property's own pictures, not those of its spaces.
    assert ids(api.get(FILES, params={"property_id": prop_id, "direct": True})) == [
        early["id"],
        late["id"],
    ]
    assert ids(api.get(FILES, params={"space_id": place["space"]["id"], "direct": True})) == [
        space_photo["id"]
    ]


def test_asset_images_belong_to_the_asset(
    api: TestClient, storage: LocalStorage, place: dict
) -> None:
    prop_id = place["property"]["id"]
    asset = post(
        api,
        "assets",
        {"property_id": prop_id, "name": "Projector", "category": "IT_EQUIPMENT"},
    )
    photo = upload(api, PNG, "ASSET", property_id=prop_id, asset_id=asset["id"])
    assert photo["asset_id"] == asset["id"] and photo["space_id"] is None
    assert ids(api.get(FILES, params={"asset_id": asset["id"]})) == [photo["id"]]
    # A room's gallery does not pick up the asset's photo.
    assert ids(api.get(FILES, params={"property_id": prop_id, "direct": True})) == []

    assert upload(api, PNG, "ASSET", 400, property_id=prop_id)["detail"].startswith("Asset images")
    assert upload(api, PNG, "GALLERY", 400, property_id=prop_id, asset_id=asset["id"])

    other = create_property(api, name="Other Tower")
    foreign = post(api, "assets", {"property_id": other["id"], "name": "Chair", "category": "X"})
    refused = upload(api, PNG, "ASSET", 400, property_id=prop_id, asset_id=foreign["id"])
    assert refused["detail"] == "The asset does not belong to this property."

    # The image keeps the asset from being deleted out from under it.
    assert api.delete(f"{V1}/assets/{asset['id']}").status_code == 409
    assert api.delete(f"{FILES}/{photo['id']}").status_code == 204
    assert api.delete(f"{V1}/assets/{asset['id']}").status_code == 204


def test_covers_are_each_propertys_own_primary_image(
    api: TestClient, storage: LocalStorage, place: dict
) -> None:
    first = place["property"]["id"]
    second = create_property(api, name="Second Tower")["id"]
    third = create_property(api, name="Third Tower")["id"]
    cover = upload(api, PNG, "MAIN", property_id=first)
    # A building cover is not the property's cover.
    api.post(
        FILES,
        data={
            "kind": "BUILDING",
            "is_primary": "true",
            "property_id": first,
            "building_id": place["building"]["id"],
        },
        files={"file": ("b.png", PNG, "image/png")},
    )
    upload(api, PNG, "GALLERY", property_id=second)  # not primary: no cover

    response = api.get(f"{FILES}/covers", params={"property_id": [first, second, third]})
    assert response.status_code == 200, response.text
    assert [(c["property_id"], c["id"]) for c in response.json()] == [(first, cover["id"])]

    many = [("property_id", first)] * 101
    assert api.get(f"{FILES}/covers", params=many).status_code == 422
    assert api.get(f"{FILES}/covers").status_code == 422


def test_each_owner_holds_a_limited_number_of_files(
    api: TestClient, storage: LocalStorage, place: dict, db_session: Session
) -> None:
    limited = get_settings().model_copy(update={"max_files_per_owner": 2})
    app.dependency_overrides[get_settings] = lambda: limited
    try:
        prop_id = place["property"]["id"]
        upload(api, PNG, "GALLERY", property_id=prop_id)
        upload(api, PNG, "GALLERY", property_id=prop_id)
        refused = upload(api, PNG, "GALLERY", 409, property_id=prop_id)
        assert "the most allowed" in refused["detail"]
        # Another owner in the same property has its own allowance.
        upload(api, PNG, "BUILDING", property_id=prop_id, building_id=place["building"]["id"])
    finally:
        app.dependency_overrides.pop(get_settings, None)
    assert db_session.scalar(select(func.count()).select_from(StoredFile)) == 3
    assert len([p for p in storage.root.rglob("*") if p.is_file()]) == 3


def test_new_endpoints_require_an_admin(client: TestClient) -> None:
    some = "00000000-0000-0000-0000-000000000000"
    assert client.patch(f"{FILES}/{some}", json={"caption": "x"}).status_code == 401
    assert client.get(f"{FILES}/covers", params={"property_id": some}).status_code == 401


# --- orphan cleanup ---------------------------------------------------------------


def test_orphaned_objects_are_found_and_removed(
    api: TestClient, storage: LocalStorage, place: dict, db_session: Session
) -> None:
    kept = upload(api, PNG, "GALLERY", property_id=place["property"]["id"])
    kept_path = db_session.scalar(select(StoredFile.object_path))
    old = time.time() - 7200
    os.utime(storage.root / kept_path, (old, old))
    stale = storage.root / "abandoned" / "gallery" / "left-behind.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(PNG)
    os.utime(stale, (old, old))
    fresh = storage.root / "abandoned" / "gallery" / "upload-in-flight.png"
    fresh.write_bytes(PNG)

    service = FileService(db_session, storage)
    assert service.purge_orphans() == ["abandoned/gallery/left-behind.png"]
    assert stale.exists()  # a dry run removes nothing

    assert service.purge_orphans(dry_run=False) == ["abandoned/gallery/left-behind.png"]
    assert not stale.exists()
    assert fresh.exists()  # too young: may be an upload about to save its record
    assert (storage.root / kept_path).exists()
    assert api.get(f"{FILES}/{kept['id']}").status_code == 200


def test_supabase_storage_lists_objects_through_folders() -> None:
    listed: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        prefix = json.loads(request.content)["prefix"]
        listed.append({"path": request.url.path, "prefix": prefix})
        if prefix == "":
            return httpx.Response(200, json=[{"name": "prop", "id": None}])
        if prefix == "prop":
            return httpx.Response(
                200,
                json=[
                    {"name": "a.png", "id": "1", "updated_at": "2026-01-01T00:00:00Z"},
                    {"name": "main", "id": None},
                ],
            )
        return httpx.Response(200, json=[{"name": "b.jpg", "id": "2", "created_at": None}])

    supabase = SupabaseStorage(
        "https://ref.supabase.co", "sb_secret_test", "b", transport=httpx.MockTransport(handler)
    )
    objects = list(supabase.list_objects())
    assert [o.path for o in objects] == ["prop/a.png", "prop/main/b.jpg"]
    assert objects[0].modified_at < time.time() - 86400
    assert objects[1].modified_at > time.time() - 60  # unknown age counts as new
    assert {entry["path"] for entry in listed} == {"/storage/v1/object/list/b"}
