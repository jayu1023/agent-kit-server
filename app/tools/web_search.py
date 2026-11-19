"""`web_search` with a pluggable provider.

**The default provider is offline** — it searches the bundled `data/kb/` corpus,
not the internet. That is a deliberate trade and it is stated in the README:
a deterministic provider is what makes the test suite assert on tool *results*
and the demo run with no network and no API key. `SEARCH_PROVIDER=tavily` with
a key makes it a real web search; the tool name, schema and result shape do not
change, which is the whole point of putting it behind a provider.

Ranking is BM25-ish: IDF-weighted term overlap with length normalisation. Not
a vector search — for a 6-document fixture corpus an embedding model would be
ceremony, and keyword matching is the honest baseline.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from ..config import settings

_WORD = re.compile(r"\w+", re.UNICODE)
SNIPPET_CHARS = 280
STOPWORDS = {"the", "a", "an", "of", "and", "or", "to", "in", "is", "are", "for", "on", "what"}


def _terms(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in STOPWORDS]


@dataclass(frozen=True)
class Doc:
    doc_id: str
    title: str
    url: str
    text: str


@lru_cache
def _corpus() -> tuple[Doc, ...]:
    docs: list[Doc] = []
    for path in sorted(settings().kb_dir.glob("*.md")):
        body = path.read_text(encoding="utf-8")
        first = next((ln for ln in body.splitlines() if ln.startswith("# ")), path.stem)
        docs.append(
            Doc(
                doc_id=path.stem,
                title=first.lstrip("# ").strip(),
                # Dart's Citation.url is non-nullable, so a local file still
                # needs a real url rather than "".
                url=path.resolve().as_uri(),
                text=body,
            )
        )
    if not docs:
        raise RuntimeError(f"no .md files in {settings().kb_dir}")
    return tuple(docs)


@lru_cache
def _idf() -> dict[str, float]:
    docs = _corpus()
    n = len(docs)
    seen: dict[str, int] = {}
    for doc in docs:
        for term in set(_terms(doc.text)):
            seen[term] = seen.get(term, 0) + 1
    return {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in seen.items()}


def _snippet(text: str, query_terms: set[str]) -> str:
    """The sentence with the most query terms, so the citation chip in the
    Flutter client shows the matching line and not just the document's opening."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) > 30]
    if not sentences:
        return text[:SNIPPET_CHARS]
    best = max(sentences, key=lambda s: len(query_terms & set(_terms(s))))
    return best[:SNIPPET_CHARS]


def _offline_search(query: str, limit: int) -> list[dict]:
    idf = _idf()
    query_terms = _terms(query)
    unique = set(query_terms)
    scored: list[tuple[float, Doc]] = []

    for doc in _corpus():
        doc_terms = _terms(doc.text)
        counts: dict[str, int] = {}
        for term in doc_terms:
            counts[term] = counts.get(term, 0) + 1
        length = max(len(doc_terms), 1)
        score = sum(
            idf.get(term, 0.0) * (counts.get(term, 0) / length) ** 0.5 for term in unique
        )
        if score > 0:
            scored.append((score, doc))

    scored.sort(key=lambda p: p[0], reverse=True)
    return [
        {
            "source_id": doc.doc_id,
            "title": doc.title,
            "url": doc.url,
            "snippet": _snippet(doc.text, unique),
            "score": round(score, 4),
        }
        for score, doc in scored[:limit]
    ]


def _tavily_search(query: str, limit: int) -> list[dict]:
    import json
    import urllib.request

    cfg = settings()
    if not cfg.tavily_api_key:
        raise RuntimeError("SEARCH_PROVIDER=tavily but TAVILY_API_KEY is empty")
    request = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps(
            {"api_key": cfg.tavily_api_key, "query": query, "max_results": limit}
        ).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=cfg.tool_timeout_s) as response:
        payload = json.load(response)
    return [
        {
            "source_id": item.get("url", ""),
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": (item.get("content") or "")[:SNIPPET_CHARS],
            "score": item.get("score"),
        }
        for item in payload.get("results", [])
    ]


class SearchArgs(BaseModel):
    query: str = Field(description="Natural-language search query.")
    limit: int = Field(default=3, ge=1, le=10, description="Maximum results.")


@tool("web_search", args_schema=SearchArgs)
def web_search(query: str, limit: int = 3) -> list[dict]:
    """Search the knowledge base for passages relevant to a query. Returns a list
    of {source_id, title, url, snippet, score}. Use this before answering any
    question about policy, process or documented behaviour — never answer those
    from memory."""
    if not query.strip():
        raise ValueError("query must not be empty")
    provider = _tavily_search if settings().search_provider == "tavily" else _offline_search
    return provider(query, limit)
