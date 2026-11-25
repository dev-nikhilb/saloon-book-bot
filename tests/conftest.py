import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def tenant_id():
    return uuid.uuid4()


@pytest.fixture
def another_tenant_id():
    return uuid.uuid4()
