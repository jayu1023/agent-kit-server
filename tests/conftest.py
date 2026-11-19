import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_env(tmp_path_factory):
    """Keep tests off the dev config and out of data/reports."""
    os.environ["PLANNER"] = "heuristic"
    os.environ["SEARCH_PROVIDER"] = "offline"
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ["REPORTS_DIR"] = str(tmp_path_factory.mktemp("reports"))

    from app import config

    config.settings.cache_clear()
    yield


@pytest.fixture
def cfg():
    return {"configurable": {"thread_id": "test-thread"}}
