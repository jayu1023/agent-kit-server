"""Runtime config. One place, env-overridable."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # planner: `heuristic` needs no API key and is deterministic, which is what
    # makes the test suite and the demo possible. `llm` is the real thing.
    planner: Literal["heuristic", "llm"] = "heuristic"
    openai_api_key: str | None = None
    planner_model: str = "gpt-4o-mini"

    # web_search provider. `offline` searches the bundled data/kb corpus so the
    # repo is deterministic and runs with no network. `tavily` is a real search.
    search_provider: Literal["offline", "tavily"] = "offline"
    tavily_api_key: str | None = None

    # guards — every one of these is a graph-level limit, not a per-tool hope
    max_steps: int = 8
    tool_timeout_s: float = 10.0
    tool_retries: int = 2
    retry_backoff_s: float = 0.25

    # data
    kb_dir: Path = ROOT / "data" / "kb"
    tickets_csv: Path = ROOT / "data" / "support_tickets.csv"
    reports_dir: Path = ROOT / "data" / "reports"


@lru_cache
def settings() -> Settings:
    return Settings()
