from datetime import date

import pytest

from support_agent.store import load_store
from support_agent.tools import Session

TODAY = date(2026, 11, 20)


@pytest.fixture
def session() -> Session:
    return Session(store=load_store(), today=TODAY)
