from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .download import DownloadError, load_queue


PUBLISHER_BY_DOI_PREFIX = {
    "10.1103/": "aps",
    "10.1016/": "elsevier",
}


def publisher_for(paper: dict[str, Any]) -> str:
    configured = str(paper.get("publisher") or "").strip().lower()
    if configured:
        return configured
    doi = str(paper["doi"]).lower()
    for prefix, publisher in PUBLISHER_BY_DOI_PREFIX.items():
        if doi.startswith(prefix):
            return publisher
    return "unknown"


def make_label(index: int, paper: dict[str, Any]) -> str:
    journal = re.sub(r"[^A-Za-z0-9]+", "", str(paper.get("journal") or "Paper"))
    title_words = re.findall(r"[A-Za-z0-9]+", str(paper["title"]))[:5]
    short_title = "_".join(title_words) or "Untitled"
    return f"{paper.get('year') or 'Unknown'}_{journal[:18]}_{short_title}_{index:02d}"


def build_validated_queue(papers: list[dict[str, Any]]) -> dict[str, Any]:
    references = []
    for index, paper in enumerate(papers, start=1):
        references.append(
            {
                "id": index,
                "doi": str(paper["doi"]).lower(),
                "status": "verified",
                "label": make_label(index, paper),
                "title": paper["title"],
                "authors": "",
                "year": int(paper.get("year") or 0),
                "journal": str(paper.get("journal") or ""),
                "publisher": publisher_for(paper),
            }
        )
    return {
        "parent_doi": "",
        "parent_title": "AutoPaper custom Edge download test",
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": len(references),
            "verified": len(references),
            "failed": 0,
            "no_doi": 0,
        },
        "references": references,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert an AutoPaper JSONL queue for ref-downloader Mode B."
    )
    parser.add_argument("--queue", default="examples/download_queue.jsonl")
    parser.add_argument("--output-dir", default="paper_inbox/ref_downloader_test")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        papers = load_queue(args.queue)
        if not papers:
            raise DownloadError("The download queue is empty")
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "refs_validated.json"
        output_path.write_text(
            json.dumps(build_validated_queue(papers), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Prepared {len(papers)} papers: {output_path.resolve()}")
        return 0
    except (DownloadError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
