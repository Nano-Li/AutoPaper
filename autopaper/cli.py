from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ConfigError, Settings, load_settings
from .openalex import OpenAlexClient, OpenAlexError, normalize_work


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug[:50] or "search"


def _normalized_title(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(character for character in normalized if character.isalnum())


def _candidate_quality(candidate: dict[str, Any]) -> tuple[int, int, int, int]:
    """Prefer the richest record when OpenAlex exposes duplicate manifestations."""
    doi = (candidate.get("doi") or "").lower()
    return (
        int(candidate.get("abstract_status") == "available"),
        int(doi.startswith("10.1103/")),
        len(candidate.get("abstract") or ""),
        int(candidate.get("cited_by_count") or 0),
    )


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def _write_csv(path: Path, candidates: list[dict[str, Any]]) -> None:
    fieldnames = [
        "retrieval_rank",
        "doi",
        "title",
        "abstract",
        "abstract_status",
        "publication_year",
        "publication_date",
        "journal",
        "authors",
        "cited_by_count",
        "openalex_id",
        "landing_page_url",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(
                {
                    "retrieval_rank": candidate["retrieval_rank"],
                    "doi": candidate["doi"],
                    "title": candidate["title"],
                    "abstract": candidate["abstract"],
                    "abstract_status": candidate["abstract_status"],
                    "publication_year": candidate["publication_year"],
                    "publication_date": candidate["publication_date"],
                    "journal": candidate["journal"]["name"],
                    "authors": "; ".join(
                        author["name"] for author in candidate["authors"] if author["name"]
                    ),
                    "cited_by_count": candidate["cited_by_count"],
                    "openalex_id": candidate["openalex_id"],
                    "landing_page_url": candidate["landing_page_url"],
                }
            )


def run(settings: Settings) -> Path:
    client = OpenAlexClient(settings)
    raw_works, query_info = client.search()

    doi_to_index: dict[str, int] = {}
    title_to_index: dict[tuple[str, int | None, str | None], int] = {}
    candidates: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for rank, work in enumerate(raw_works, start=1):
        candidate = normalize_work(work, rank)
        doi = (candidate["doi"] or "").lower()
        title_key = (
            _normalized_title(candidate["title"]),
            candidate["publication_year"],
            candidate["journal"]["openalex_id"] or candidate["journal"]["name"],
        )

        existing_index = doi_to_index.get(doi) if doi else None
        if existing_index is None and title_key[0]:
            existing_index = title_to_index.get(title_key)
        if existing_index is not None:
            existing = candidates[existing_index]
            if _candidate_quality(candidate) > _candidate_quality(existing):
                duplicates.append({"kept": candidate, "merged": existing})
                if existing["doi"]:
                    doi_to_index.pop(existing["doi"].lower(), None)
                old_title_key = (
                    _normalized_title(existing["title"]),
                    existing["publication_year"],
                    existing["journal"]["openalex_id"] or existing["journal"]["name"],
                )
                title_to_index.pop(old_title_key, None)
                candidates[existing_index] = candidate
                if doi:
                    doi_to_index[doi] = existing_index
                if title_key[0]:
                    title_to_index[title_key] = existing_index
            else:
                duplicates.append({"kept": existing, "merged": candidate})
            continue

        candidate_index = len(candidates)
        candidates.append(candidate)
        if doi:
            doi_to_index[doi] = candidate_index
        if title_key[0]:
            title_to_index[title_key] = candidate_index
    candidates.sort(key=lambda candidate: candidate["retrieval_rank"])

    missing_abstract = [
        candidate for candidate in candidates if candidate["abstract_status"] == "missing"
    ]
    query_info["normalized_count"] = len(candidates)
    query_info["duplicate_count"] = len(duplicates)
    query_info["with_abstract_count"] = len(candidates) - len(missing_abstract)
    query_info["missing_abstract_count"] = len(missing_abstract)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = settings.search.output_dir / f"{timestamp}-{_safe_slug(settings.search.query)}"
    run_dir.mkdir(parents=True, exist_ok=False)

    _write_json(run_dir / "query.json", query_info)
    _write_jsonl(run_dir / "raw_openalex.jsonl", raw_works)
    _write_jsonl(run_dir / "candidates.jsonl", candidates)
    _write_jsonl(run_dir / "duplicates.jsonl", duplicates)
    _write_jsonl(run_dir / "missing_abstract.jsonl", missing_abstract)
    _write_csv(run_dir / "candidates.csv", candidates)

    print(f"OpenAlex reported: {query_info['reported_total']}")
    print(f"OpenAlex retrieved: {query_info['retrieved_count']}")
    if query_info["truncated"]:
        print(
            "Notice: results were truncated at the configured/basic paging limit; "
            "narrow the query or use cursor paging for more than 10,000 records."
        )
    print(f"Saved candidates: {len(candidates)}")
    print(f"Merged duplicates: {len(duplicates)}")
    print(f"Candidates with abstract: {query_info['with_abstract_count']}")
    print(f"Missing abstract: {len(missing_abstract)}")
    print(f"Output: {run_dir.resolve()}")
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autopaper", description="Search OpenAlex and save structured paper metadata."
    )
    parser.add_argument(
        "--config",
        default="config/config.local.toml",
        help="Path to the local TOML config (default: config/config.local.toml)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        run(settings)
        return 0
    except (ConfigError, OpenAlexError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
