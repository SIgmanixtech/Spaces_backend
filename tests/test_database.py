from sqlalchemy import text

from app.db.session import get_engine
from tests.conftest import requires_database


@requires_database
def test_database_connectivity() -> None:
    with get_engine().connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1
        version = connection.execute(text("SHOW server_version_num")).scalar_one()

    # Exclusion constraints on booking ranges rely on a modern PostgreSQL.
    assert int(version) >= 140000
