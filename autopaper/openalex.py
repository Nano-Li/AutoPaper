from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .config import OPENALEX_BASIC_PAGING_LIMIT, Settings


class OpenAlexError(RuntimeError):
    """An actionable error returned by OpenAlex or the network."""


WORK_SELECT_FIELDS = (
    "id",
    "doi",
    "title",
    "display_name",
    "abstract_inverted_index",
    "publication_year",
    "publication_date",
    "type",
    "language",
    "primary_location",
    "authorships",
    "keywords",
    "cited_by_count",
    "is_retracted",
    "open_access",
)


def reconstruct_abstract(index: dict[str, list[int]] | None) -> str | None:
    if not index:
        return None
    positioned_words = sorted(
        (position, word)
        for word, positions in index.items()
        for position in positions
    )
    return " ".join(word for _, word in positioned_words)


def compile_filter(settings: Settings) -> str:
    search = settings.search
    clauses = [
        f"title_and_abstract.search:{search.query}",
        f"from_publication_date:{search.from_year}-01-01",
        f"to_publication_date:{search.to_year}-12-31",
        f"primary_location.source.issn:{'|'.join(j.issn for j in search.journals)}",
        f"type:{'|'.join(search.types)}",
    ]
    if search.require_abstract:
        clauses.append("has_abstract:true")
    if search.exclude_retracted:
        clauses.append("is_retracted:false")
    return ",".join(clauses)


class OpenAlexClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        route = settings.openalex.network_route
        if route == "direct":
            proxy_handler = ProxyHandler({})
        else:
            proxy = settings.openalex.proxy_url
            proxy_handler = ProxyHandler({"http": proxy, "https": proxy})
        self.opener = build_opener(proxy_handler)
        self._last_request_started = 0.0

    def _wait_for_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_started
        remaining = self.settings.openalex.min_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.settings.openalex.base_url}{path}?{urlencode(params)}"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.settings.openalex.api_key}",
                "User-Agent": "AutoPaper/0.1 (academic metadata search)",
            },
            method="GET",
        )

        attempts = self.settings.openalex.max_retries + 1
        for attempt in range(attempts):
            self._wait_for_rate_limit()
            self._last_request_started = time.monotonic()
            try:
                with self.opener.open(
                    request, timeout=self.settings.openalex.timeout_seconds
                ) as response:
                    return json.load(response)
            except HTTPError as exc:
                raw_body = exc.read().decode("utf-8", errors="replace")
                try:
                    error_data = json.loads(raw_body)
                    message = error_data.get("message") or error_data.get("error") or raw_body
                except json.JSONDecodeError:
                    message = raw_body or str(exc)

                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt + 1 < attempts:
                    retry_after = exc.headers.get("Retry-After")
                    delay = min(float(retry_after), 30.0) if retry_after else 2.0 ** attempt
                    time.sleep(delay)
                    continue
                raise OpenAlexError(f"OpenAlex HTTP {exc.code}: {message}") from exc
            except URLError as exc:
                if attempt + 1 < attempts:
                    time.sleep(2.0 ** attempt)
                    continue
                raise OpenAlexError(f"Cannot reach OpenAlex: {exc.reason}") from exc

        raise OpenAlexError("OpenAlex request failed after all retries")

    def search(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        compiled_filter = compile_filter(self.settings)
        raw_works: list[dict[str, Any]] = []
        first_meta: dict[str, Any] = {}
        pages_retrieved = 0

        for page in range(1, self.settings.openalex.max_pages + 1):
            payload = self._get_json(
                "/works",
                {
                    "filter": compiled_filter,
                    "per_page": self.settings.openalex.per_page,
                    "page": page,
                    "select": ",".join(WORK_SELECT_FIELDS),
                },
            )
            pages_retrieved = page
            if page == 1:
                first_meta = payload.get("meta", {})
            batch = payload.get("results", [])
            raw_works.extend(batch)
            if len(batch) < self.settings.openalex.per_page:
                break

        reported_total = first_meta.get("count")
        if not isinstance(reported_total, int):
            reported_total = len(raw_works)
        configured_result_limit = (
            self.settings.openalex.per_page * self.settings.openalex.max_pages
        )
        query_info = {
            "source": "openalex",
            "search_scope": "title_and_abstract",
            "query": self.settings.search.query,
            "from_year": self.settings.search.from_year,
            "to_year": self.settings.search.to_year,
            "journals": [asdict(journal) for journal in self.settings.search.journals],
            "types": list(self.settings.search.types),
            "compiled_filter": compiled_filter,
            "network_route": self.settings.openalex.network_route,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "reported_total": reported_total,
            "retrieved_count": len(raw_works),
            "pages_retrieved": pages_retrieved,
            "per_page": self.settings.openalex.per_page,
            "max_pages": self.settings.openalex.max_pages,
            "configured_result_limit": configured_result_limit,
            "basic_paging_limit": OPENALEX_BASIC_PAGING_LIMIT,
            "truncated": reported_total > len(raw_works),
            "api_cost_usd": first_meta.get("cost_usd"),
        }
        return raw_works, query_info


def normalize_work(work: dict[str, Any], retrieval_rank: int) -> dict[str, Any]:
    primary_location = work.get("primary_location") or {}
    source = primary_location.get("source") or {}
    authors = []
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}
        authors.append(
            {
                "name": author.get("display_name") or authorship.get("raw_author_name"),
                "openalex_id": author.get("id"),
                "orcid": author.get("orcid"),
                "institutions": [
                    institution.get("display_name")
                    for institution in authorship.get("institutions") or []
                    if institution.get("display_name")
                ],
            }
        )

    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
    doi = work.get("doi")
    if isinstance(doi, str) and doi.lower().startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/") :]

    return {
        "openalex_id": work.get("id"),
        "doi": doi,
        "title": work.get("title") or work.get("display_name"),
        "abstract": abstract,
        "abstract_status": "available" if abstract else "missing",
        "publication_year": work.get("publication_year"),
        "publication_date": work.get("publication_date"),
        "type": work.get("type"),
        "language": work.get("language"),
        "journal": {
            "name": source.get("display_name"),
            "openalex_id": source.get("id"),
            "issn_l": source.get("issn_l"),
            "issns": source.get("issn") or [],
        },
        "authors": authors,
        "keywords": [
            {
                "name": keyword.get("display_name"),
                "score": keyword.get("score"),
            }
            for keyword in work.get("keywords") or []
        ],
        "cited_by_count": work.get("cited_by_count"),
        "is_retracted": work.get("is_retracted"),
        "open_access": work.get("open_access"),
        "landing_page_url": primary_location.get("landing_page_url"),
        "pdf_url": primary_location.get("pdf_url"),
        "retrieval_rank": retrieval_rank,
        "metadata_source": "openalex",
    }
