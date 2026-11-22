import json
from datetime import datetime
from pathlib import Path

import pytest

from radar.config import load_yaml
from radar.db import make_engine, make_session_factory
from radar.profile import load_profile

FIX = Path(__file__).parent / "fixtures"
TODAY = datetime(2026, 11, 21, 12, 0)


@pytest.fixture(scope="session")
def profile():
    return load_profile()


@pytest.fixture()
def settings():
    return load_yaml("settings.yaml")


@pytest.fixture()
def session():
    eng = make_engine("sqlite:///:memory:")
    S = make_session_factory(eng)
    with S() as s:
        yield s


@pytest.fixture(scope="session")
def postings():
    return json.loads((FIX / "real_postings.json").read_text(encoding="utf-8"))
