import os
import uuid
import pytest
from pymongo import MongoClient

# Configure test environment variable BEFORE importing app.main
TEST_DB_NAME = f"attendance_test_{uuid.uuid4().hex[:8]}"
os.environ["MONGO_DB"] = TEST_DB_NAME

from app.main import app, client, set_database, create_required_indexes
from fastapi.testclient import TestClient


def assert_safe_test_database(db_instance):
    """Safety guard: Ensure we NEVER touch a non-test database."""
    name = db_instance.name.lower()
    assert "test" in name, f"Safety check failed: database '{name}' does not contain 'test'!"
    assert name not in ("attendance_db", "attendance_dev", "production", "default"), (
        f"Safety check failed: attempted to operate on protected database '{name}'!"
    )


@pytest.fixture(scope="session")
def test_db():
    """Session fixture creating and cleanly tearing down an isolated test database."""
    test_database = client[TEST_DB_NAME]
    assert_safe_test_database(test_database)

    # Initialize indexes and configure app to target test database
    set_database(test_database)
    create_required_indexes(test_database)

    yield test_database

    # Teardown: drop test database
    assert_safe_test_database(test_database)
    client.drop_database(TEST_DB_NAME)


@pytest.fixture(autouse=True)
def clean_collections(test_db):
    """Deterministic fixture: ensure clean collections before each test."""
    assert_safe_test_database(test_db)
    test_db.employees.delete_many({})
    test_db.attendance_logs.delete_many({})
    yield


@pytest.fixture
def client_app(test_db):
    """Provides a TestClient targeting the isolated test database."""
    with TestClient(app) as tc:
        yield tc
