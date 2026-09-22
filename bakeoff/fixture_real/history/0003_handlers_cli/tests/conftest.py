from __future__ import annotations

import pytest
from jobsched import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path / "test.db"))
    db.apply_migrations(connection)
    yield connection
    connection.close()
