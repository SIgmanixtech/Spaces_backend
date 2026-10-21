"""Alembic revisions, exercised on the disposable test database only.

Covers what a review of a migration needs to know: that it matches the models,
that rows which existed before it survive the upgrade and a clean downgrade
untouched, and that the downgrade refuses to throw away data created since.
"""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.db.base import Base
from app.db.session import dispose_engine, get_engine
from tests.conftest import BACKEND_ROOT

NEW_SPACE_COLUMNS = {
    "building_id",
    "floor_id",
    "parent_space_id",
    "depth",
    "parent_depth",
    "area_sqft",
    "is_rentable",
}
LEGACY_SPACE_TYPES = ["DESK", "PRIVATE_OFFICE", "CABIN", "MEETING_ROOM", "COWORKING"]


def alembic_config() -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    return config


def current_revision() -> str:
    with get_engine().connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


@pytest.fixture
def migrations(migrated_database: None) -> Iterator[Config]:
    """Hand the test database back empty and at the latest revision, whatever the test did."""
    # Pooled connections may hold plans for a table shape that is about to change.
    dispose_engine()
    try:
        yield alembic_config()
    finally:
        dispose_engine()
        # Upgrade first: a test may leave an older revision, which lacks tables
        # added since (0004's allocations), and the truncate names every table.
        command.upgrade(alembic_config(), "head")
        dispose_engine()
        tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
        with get_engine().begin() as connection:
            connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        dispose_engine()


def insert_legacy_data(connection: Connection) -> None:
    """Rows shaped exactly as revision 0001 stores them: no floors, no hierarchy."""
    property_id = connection.execute(
        text(
            "INSERT INTO properties (name, property_type, category, address, city, state,"
            " postal_code) VALUES ('Legacy Tower', 'OFFICE', 'COMMERCIAL', '1 Old Road',"
            " 'Hyderabad', 'Telangana', '500001') RETURNING id"
        )
    ).scalar_one()
    layout_id = connection.execute(
        text("INSERT INTO layouts (property_id, name) VALUES (:p, 'Old plan') RETURNING id"),
        {"p": property_id},
    ).scalar_one()
    space_ids = [
        connection.execute(
            text(
                "INSERT INTO spaces (property_id, layout_id, name, code, space_type, capacity,"
                " price_amount, price_unit, x, y, width, height, attributes, archived_at)"
                " VALUES (:p, :l, :name, :code, :type, 4, 450.50, 'DAY', :n, 0, 1, 1,"
                " '{\"window\": true}', :archived) RETURNING id"
            ),
            {
                "p": property_id,
                "l": layout_id if number % 2 else None,
                "name": f"Legacy {space_type}",
                "code": f"L-{number}",
                "type": space_type,
                "n": number,
                "archived": "2026-01-01T00:00:00+00:00" if space_type == "COWORKING" else None,
            },
        ).scalar_one()
        for number, space_type in enumerate(LEGACY_SPACE_TYPES)
    ]
    connection.execute(
        text(
            "INSERT INTO bookings (property_id, space_id, customer_name, start_at, end_at, status)"
            " VALUES (:p, :s, 'Priya Nair', '2031-01-06T09:00:00+00:00',"
            " '2031-01-06T10:00:00+00:00', 'CONFIRMED')"
        ),
        {"p": property_id, "s": space_ids[0]},
    )
    tenant_id = connection.execute(
        text("INSERT INTO tenants (name) VALUES ('Acme Ltd') RETURNING id")
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO tenant_occupancies (tenant_id, property_id, space_id, start_date)"
            " VALUES (:t, :p, :s, '2026-01-01')"
        ),
        {"t": tenant_id, "p": property_id, "s": space_ids[1]},
    )


def snapshot(connection: Connection) -> dict[str, list[dict]]:
    """Every row of the tables that reference spaces, as JSON, in a stable order."""
    return {
        table: [
            row[0]
            for row in connection.execute(
                text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY t.id")  # noqa: S608
            )
        ]
        for table in (
            "properties",
            "layouts",
            "spaces",
            "bookings",
            "tenants",
            "tenant_occupancies",
        )
    }


def test_models_and_migrations_describe_the_same_schema(migrations: Config) -> None:
    with get_engine().connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)
    assert differences == []


def test_upgrade_keeps_every_existing_row_and_a_clean_downgrade_restores_them(
    migrations: Config,
) -> None:
    command.downgrade(migrations, "0001")
    assert current_revision() == "0001"
    with get_engine().begin() as connection:
        insert_legacy_data(connection)
        before = snapshot(connection)
    assert len(before["spaces"]) == 5

    command.upgrade(migrations, "0002")
    assert current_revision() == "0002"
    with get_engine().connect() as connection:
        after = snapshot(connection)

    # Nothing but spaces changed shape, and nothing changed content.
    for table in before:
        if table != "spaces":
            assert after[table] == before[table], table
    for old, new in zip(before["spaces"], after["spaces"], strict=True):
        assert {key: value for key, value in new.items() if key not in NEW_SPACE_COLUMNS} == old
        # Every existing space is unassigned: the migration guesses no floor.
        assert {key: new[key] for key in NEW_SPACE_COLUMNS} == {
            "building_id": None,
            "floor_id": None,
            "parent_space_id": None,
            "depth": 0,
            "parent_depth": None,
            "area_sqft": None,
            "is_rentable": True,
        }
    with get_engine().connect() as connection:
        created = connection.execute(
            text("SELECT (SELECT count(*) FROM buildings), (SELECT count(*) FROM floors)")
        ).one()
    assert tuple(created) == (0, 0)  # no inventory was invented

    # Untouched since the upgrade, so the downgrade is allowed and loses nothing.
    command.downgrade(migrations, "0001")
    with get_engine().connect() as connection:
        assert snapshot(connection) == before


def test_downgrade_refuses_to_discard_hierarchy_data(migrations: Config) -> None:
    command.downgrade(migrations, "0002")
    with get_engine().begin() as connection:
        insert_legacy_data(connection)
        property_id = connection.execute(text("SELECT id FROM properties")).scalar_one()
        building_id = connection.execute(
            text("INSERT INTO buildings (property_id, name) VALUES (:p, 'Block A') RETURNING id"),
            {"p": property_id},
        ).scalar_one()
        floor_id = connection.execute(
            text(
                "INSERT INTO floors (building_id, name, level_number)"
                " VALUES (:b, 'Level 1', 1) RETURNING id"
            ),
            {"b": building_id},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO spaces (property_id, building_id, floor_id, name, space_type)"
                " VALUES (:p, :b, :f, 'Whole floor', 'FLOOR')"
            ),
            {"p": property_id, "b": building_id, "f": floor_id},
        )
        before = snapshot(connection)

    with pytest.raises(DBAPIError, match="Cannot downgrade past 0002: 1 space"):
        command.downgrade(migrations, "0001")

    # The refusal rolled the whole downgrade back: same revision, same rows.
    assert current_revision() == "0002"
    with get_engine().connect() as connection:
        assert snapshot(connection) == before


@pytest.mark.parametrize(
    ("change", "refused"),
    [
        ("area_sqft = 1450", True),
        ("is_rentable = false", True),
        ("name = 'Renamed'", False),
    ],
)
def test_downgrade_guard_covers_each_new_column(
    migrations: Config, change: str, refused: bool
) -> None:
    command.downgrade(migrations, "0002")
    with get_engine().begin() as connection:
        insert_legacy_data(connection)
        connection.execute(text(f"UPDATE spaces SET {change} WHERE code = 'L-0'"))  # noqa: S608
    if refused:
        with pytest.raises(DBAPIError, match="Cannot downgrade past 0002"):
            command.downgrade(migrations, "0001")
        assert current_revision() == "0002"
    else:
        command.downgrade(migrations, "0001")
        assert current_revision() == "0001"


def test_revision_0002_renders_as_sql_without_a_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``alembic upgrade 0001:0002 --sql`` is how the change is reviewed before it is applied."""
    command.upgrade(alembic_config(), "0001:0002", sql=True)
    upgrade = capsys.readouterr().out
    command.downgrade(alembic_config(), "0002:0001", sql=True)
    downgrade = capsys.readouterr().out

    assert "ALTER TABLE spaces ADD COLUMN floor_id UUID" in upgrade
    assert "CONSTRAINT fk_spaces_parent_space_id_spaces" in upgrade
    assert "DEFERRABLE INITIALLY DEFERRED" in upgrade
    assert "UPDATE alembic_version SET version_num='0002'" in upgrade
    # Purely additive: nothing is dropped except the type list it replaces, and no rows change.
    assert upgrade.count("DROP ") == 1 and "DROP CONSTRAINT ck_spaces_space_type_valid" in upgrade
    assert "INSERT INTO spaces" not in upgrade and "UPDATE spaces" not in upgrade
    assert "DELETE FROM" not in upgrade and "TRUNCATE" not in upgrade

    assert downgrade.index("Cannot downgrade past 0002") < downgrade.index("DROP COLUMN")


# --- revision 0003: file metadata ---------------------------------------------------

NEW_FILE_COLUMNS = {
    "asset_id",
    "width_px",
    "height_px",
    "caption",
    "description",
    "category",
    "sort_order",
    "is_primary",
}


def insert_files(connection: Connection) -> dict[str, str]:
    """Files shaped as 0002 stores them: two covers, a gallery picture and two plans."""
    property_id = connection.execute(text("SELECT id FROM properties")).scalar_one()
    layout_id = connection.execute(text("SELECT id FROM layouts")).scalar_one()
    made: dict[str, str] = {}
    for name, kind, layout, created in (
        ("old-cover", "MAIN", None, "2026-01-01"),
        ("new-cover", "MAIN", None, "2026-02-01"),
        ("gallery", "GALLERY", None, "2026-03-01"),
        ("old-plan", "FLOOR_PLAN", layout_id, "2026-01-01"),
        ("new-plan", "FLOOR_PLAN", layout_id, "2026-02-01"),
    ):
        made[name] = connection.execute(
            text(
                "INSERT INTO files (property_id, layout_id, kind, bucket, object_path,"
                " original_name, content_type, size_bytes, created_at)"
                " VALUES (:p, :l, :k, 'property-files', :path, :name, 'image/png', 10, :c)"
                " RETURNING id"
            ),
            {
                "p": property_id,
                "l": layout,
                "k": kind,
                "path": f"x/{name}.png",
                "name": name,
                "c": created,
            },
        ).scalar_one()
    return {name: str(file_id) for name, file_id in made.items()}


def files_snapshot(connection: Connection) -> list[dict]:
    return [
        row[0] for row in connection.execute(text("SELECT to_jsonb(f) FROM files f ORDER BY f.id"))
    ]


def test_0003_keeps_files_and_marks_the_covers_and_plans_already_in_use(
    migrations: Config,
) -> None:
    command.downgrade(migrations, "0002")
    with get_engine().begin() as connection:
        insert_legacy_data(connection)
        made = insert_files(connection)
        before = files_snapshot(connection)

    command.upgrade(migrations, "0003")
    with get_engine().connect() as connection:
        after = files_snapshot(connection)
    for old, new in zip(before, after, strict=True):
        assert {key: value for key, value in new.items() if key not in NEW_FILE_COLUMNS} == old
        assert (new["caption"], new["sort_order"], new["asset_id"]) == (None, 0, None)
    primary = {row["original_name"] for row in after if row["is_primary"]}
    # The newest MAIN of the property, and the newest plan of the layout: what the app showed.
    assert primary == {"new-cover", "new-plan"}
    assert made  # ids exist

    command.downgrade(migrations, "0002")
    with get_engine().connect() as connection:
        assert files_snapshot(connection) == before


@pytest.mark.parametrize(
    ("change", "refused"),
    [
        ("caption = 'Lobby'", True),
        ("category = 'Exterior'", True),
        ("description = 'North face'", True),
        ("sort_order = 2", True),
        ("is_primary = false", False),  # a cover choice is dropped, re-derived on upgrade
        ("width_px = 10, height_px = 10", False),
    ],
)
def test_0003_downgrade_refuses_to_discard_file_details(
    migrations: Config, change: str, refused: bool
) -> None:
    command.downgrade(migrations, "0003")
    with get_engine().begin() as connection:
        insert_legacy_data(connection)
        insert_files(connection)
        connection.execute(
            text(f"UPDATE files SET {change} WHERE original_name = 'gallery'")  # noqa: S608
        )
    if refused:
        with pytest.raises(DBAPIError, match="Cannot downgrade past 0003: 1 file"):
            command.downgrade(migrations, "0002")
        assert current_revision() == "0003"
    else:
        command.downgrade(migrations, "0002")
        assert current_revision() == "0002"


def test_0003_downgrade_refuses_while_assets_have_images(migrations: Config) -> None:
    command.downgrade(migrations, "0003")
    with get_engine().begin() as connection:
        insert_legacy_data(connection)
        property_id = connection.execute(text("SELECT id FROM properties")).scalar_one()
        asset_id = connection.execute(
            text(
                "INSERT INTO assets (property_id, name, category) VALUES (:p, 'Desk', 'FURNITURE')"
                " RETURNING id"
            ),
            {"p": property_id},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO files (property_id, asset_id, kind, bucket, object_path,"
                " original_name, content_type, size_bytes)"
                " VALUES (:p, :a, 'ASSET', 'b', 'x/desk.png', 'desk.png', 'image/png', 1)"
            ),
            {"p": property_id, "a": asset_id},
        )
    with pytest.raises(DBAPIError, match="Cannot downgrade past 0003"):
        command.downgrade(migrations, "0002")
    assert current_revision() == "0003"


def test_revision_0003_renders_as_sql_without_a_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "0002:0003", sql=True)
    upgrade = capsys.readouterr().out
    command.downgrade(alembic_config(), "0003:0002", sql=True)
    downgrade = capsys.readouterr().out

    assert "ALTER TABLE files ADD COLUMN asset_id UUID" in upgrade
    assert "NULLS NOT DISTINCT" in upgrade and "WHERE is_primary" in upgrade
    assert "UPDATE alembic_version SET version_num='0003'" in upgrade
    # Additive: only the kind list is replaced; rows are only ever flagged, never removed.
    assert upgrade.count("DROP ") == 1 and "DROP CONSTRAINT ck_files_kind_valid" in upgrade
    assert "DELETE FROM" not in upgrade and "TRUNCATE" not in upgrade
    assert upgrade.index("UPDATE files SET is_primary") < upgrade.index(
        "CREATE UNIQUE INDEX uq_files_primary_per_owner"
    )
    assert downgrade.index("Cannot downgrade past 0003") < downgrade.index("DROP COLUMN")


# --- revision 0004: contracts and the allocation engine --------------------------------


def floor_with_portion(connection: Connection) -> dict[str, object]:
    """Floor 5 > Portion 5A > Desk, shaped as revision 0003 stores them."""
    ids: dict[str, object] = {}
    ids["property"] = connection.execute(
        text(
            "INSERT INTO properties (name, property_type, category, address, city, state,"
            " postal_code) VALUES ('Sigma Hub', 'COMMERCIAL', 'OFFICE', '1 Road', 'Hyderabad',"
            " 'Telangana', '500001') RETURNING id"
        )
    ).scalar_one()
    ids["building"] = connection.execute(
        text("INSERT INTO buildings (property_id, name) VALUES (:p, 'A') RETURNING id"),
        {"p": ids["property"]},
    ).scalar_one()
    ids["floor"] = connection.execute(
        text(
            "INSERT INTO floors (building_id, name, level_number) VALUES (:b, 'Fifth', 5)"
            " RETURNING id"
        ),
        {"b": ids["building"]},
    ).scalar_one()
    ids["root"] = connection.execute(
        text(
            "INSERT INTO spaces (property_id, building_id, floor_id, name, space_type, depth,"
            " is_rentable) VALUES (:p, :b, :f, 'Floor 5', 'FLOOR', 0, false) RETURNING id"
        ),
        {"p": ids["property"], "b": ids["building"], "f": ids["floor"]},
    ).scalar_one()
    ids["5A"] = connection.execute(
        text(
            "INSERT INTO spaces (property_id, building_id, floor_id, parent_space_id, depth,"
            " parent_depth, name, space_type) VALUES (:p, :b, :f, :r, 1, 0, '5A', 'PORTION')"
            " RETURNING id"
        ),
        {"p": ids["property"], "b": ids["building"], "f": ids["floor"], "r": ids["root"]},
    ).scalar_one()
    ids["desk"] = connection.execute(
        text(
            "INSERT INTO spaces (property_id, building_id, floor_id, parent_space_id, depth,"
            " parent_depth, name, space_type) VALUES (:p, :b, :f, :a, 2, 1, 'Desk', 'DESK')"
            " RETURNING id"
        ),
        {"p": ids["property"], "b": ids["building"], "f": ids["floor"], "a": ids["5A"]},
    ).scalar_one()
    ids["tenant"] = connection.execute(
        text("INSERT INTO tenants (name) VALUES ('Acme') RETURNING id")
    ).scalar_one()
    return ids


def legacy_booking(
    connection: Connection,
    ids: dict,
    space: str,
    start: str,
    end: str,
    status: str = "CONFIRMED",
) -> object:
    return connection.execute(
        text(
            "INSERT INTO bookings (property_id, space_id, customer_name, start_at, end_at, status)"
            " VALUES (:p, :s, 'Legacy', :a, :b, :st) RETURNING id"
        ),
        {"p": ids["property"], "s": ids[space], "a": start, "b": end, "st": status},
    ).scalar_one()


def test_0004_turns_occupancies_into_contracts_and_allocates_current_holds(
    migrations: Config,
) -> None:
    command.downgrade(migrations, "0003")
    with get_engine().begin() as connection:
        ids = floor_with_portion(connection)
        future = legacy_booking(
            connection, ids, "desk", "2031-01-06 09:00+00", "2031-01-06 10:00+00"
        )
        legacy_booking(connection, ids, "desk", "2020-01-06 09:00+00", "2020-01-06 10:00+00")
        legacy_booking(
            connection, ids, "5A", "2031-01-06 09:00+00", "2031-01-06 10:00+00", "CANCELLED"
        )
        occupancy = connection.execute(
            text(
                "INSERT INTO tenant_occupancies (tenant_id, property_id, space_id, start_date)"
                " VALUES (:t, :p, :s, '2032-01-01') RETURNING id"
            ),
            {"t": ids["tenant"], "p": ids["property"], "s": ids["5A"]},
        ).scalar_one()

    command.upgrade(migrations, "0004")
    with get_engine().connect() as connection:
        contract = connection.execute(
            text("SELECT reference, status, activated_at IS NOT NULL FROM tenant_occupancies")
        ).one()
        assert contract[0].startswith("CT-") and tuple(contract[1:]) == ("ACTIVE", True)
        allocations = connection.execute(
            text("SELECT source_type, booking_id, contract_id FROM allocations ORDER BY 1")
        ).all()
        # The future booking and the contract; not the past or the cancelled booking.
        assert [(a[0], a[1] or a[2]) for a in allocations] == [
            ("BOOKING", future),
            ("CONTRACT", occupancy),
        ]
        chain = connection.execute(
            text(
                "SELECT s.name, c.claim_mode FROM allocation_claims c"
                " JOIN allocations a ON a.id = c.allocation_id JOIN spaces s ON s.id = c.space_id"
                " WHERE a.booking_id = :b ORDER BY c.claim_mode, s.name"
            ),
            {"b": future},
        ).all()
        assert [tuple(row) for row in chain] == [("Desk", 1), ("5A", 2), ("Floor 5", 2)]


def test_0004_refuses_to_upgrade_over_existing_hierarchy_conflicts(migrations: Config) -> None:
    command.downgrade(migrations, "0003")
    with get_engine().begin() as connection:
        ids = floor_with_portion(connection)
        legacy_booking(connection, ids, "desk", "2031-01-06 09:00+00", "2031-01-06 10:00+00")
        legacy_booking(connection, ids, "5A", "2031-01-06 09:30+00", "2031-01-06 11:00+00")

    with pytest.raises(DBAPIError, match="Cannot upgrade to 0004: 1 pair"):
        command.upgrade(migrations, "0004")
    assert current_revision() == "0003"
    # Resolved deliberately, the upgrade goes through.
    with get_engine().begin() as connection:
        connection.execute(
            text("UPDATE bookings SET status = 'CANCELLED' WHERE space_id = :s"), {"s": ids["5A"]}
        )
    command.upgrade(migrations, "0004")
    assert current_revision() == "0004"


@pytest.mark.parametrize(
    ("change", "refused"),
    [
        ("status = 'PENDING'", True),
        ("status = 'CANCELLED', cancelled_at = now()", True),
        ("terms = 'Lock-in 12 months'", True),
        ("monthly_rent = 50000", True),
        ("notes = 'Plain note'", False),
    ],
)
def test_0004_downgrade_refuses_to_discard_contract_data(
    migrations: Config, change: str, refused: bool
) -> None:
    with get_engine().begin() as connection:
        ids = floor_with_portion(connection)
        connection.execute(
            text(
                "INSERT INTO tenant_occupancies (tenant_id, property_id, start_date)"
                " VALUES (:t, :p, '2032-01-01')"
            ),
            {"t": ids["tenant"], "p": ids["property"]},
        )
        connection.execute(text(f"UPDATE tenant_occupancies SET {change}"))  # noqa: S608
    if refused:
        with pytest.raises(DBAPIError, match="Cannot downgrade past 0004: 1 contract"):
            command.downgrade(migrations, "0003")
        assert current_revision() == "0004"
    else:
        command.downgrade(migrations, "0003")
        assert current_revision() == "0003"


def test_revision_0004_renders_as_sql_without_a_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "0003:0004", sql=True)
    upgrade = capsys.readouterr().out
    command.downgrade(alembic_config(), "0004:0003", sql=True)
    downgrade = capsys.readouterr().out

    assert "CREATE TABLE allocations" in upgrade and "CREATE TABLE allocation_claims" in upgrade
    assert "ex_allocation_claims_mixed" in upgrade and "trg_spaces_guard" in upgrade
    assert "Cannot upgrade to 0004" in upgrade
    assert "UPDATE alembic_version SET version_num='0004'" in upgrade
    # Nothing is deleted; only the old one-occupant exclusion is replaced.
    assert "DELETE FROM" not in upgrade and "TRUNCATE" not in upgrade
    assert downgrade.index("Cannot downgrade past 0004") < downgrade.index("DROP TABLE")
