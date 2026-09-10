from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

try:
    from curl_cffi.requests import Session as CurlSession
    from curl_cffi.requests.errors import RequestsError as CurlError
except ImportError:  # pragma: no cover - exercised by the user-facing startup check
    CurlSession = None
    CurlError = Exception


class DownloadError(RuntimeError):
    """A download or configuration error suitable for the user-facing log."""


@dataclass(frozen=True)
class DownloadSettings:
    network_route: str
    proxy_url: str
    output_dir: Path
    manifest_file: Path
    failures_file: Path
    manual_file: Path
    timeout_seconds: float
    min_interval_seconds: float
    max_retries: int
    min_pdf_bytes: int


@dataclass(frozen=True)
class CandidateURL:
    url: str
    source: str
    referer: str | None = None


class PDFLinkParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value for key, value in attrs if value is not None}
        if tag.lower() == "meta":
            name = (attributes.get("name") or attributes.get("property") or "").lower()
            if name in {
                "citation_pdf_url",
                "fulltext_pdf_url",
                "eprints.document_url",
            }:
                self._add(attributes.get("content"))
        elif tag.lower() in {"a", "link"}:
            href = attributes.get("href")
            kind = (attributes.get("type") or "").lower()
            rel = (attributes.get("rel") or "").lower()
            if href and (
                ".pdf" in href.lower()
                or kind == "application/pdf"
                or "alternate" in rel and kind == "application/pdf"
            ):
                self._add(href)

    def _add(self, value: str | None) -> None:
        if value:
            absolute = urljoin(self.base_url, value)
            if absolute not in self.urls:
                self.urls.append(absolute)


def load_download_settings(config_path: str | Path) -> DownloadSettings:
    path = Path(config_path)
    if not path.is_file():
        raise DownloadError(f"Config file does not exist: {path}")
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    section = data.get("download")
    if not isinstance(section, dict):
        raise DownloadError("Missing [download] section in config")

    route = str(section.get("network_route", "direct")).lower()
    if route not in {"direct", "proxy"}:
        raise DownloadError("[download] network_route must be 'direct' or 'proxy'")
    return DownloadSettings(
        network_route=route,
        proxy_url=str(section.get("proxy_url", "http://127.0.0.1:7890")),
        output_dir=Path(str(section.get("output_dir", "paper_inbox/pdf"))),
        manifest_file=Path(
            str(section.get("manifest_file", "paper_inbox/download_manifest.jsonl"))
        ),
        failures_file=Path(
            str(section.get("failures_file", "paper_inbox/download_failures.csv"))
        ),
        manual_file=Path(
            str(section.get("manual_file", "paper_inbox/manual_download.html"))
        ),
        timeout_seconds=float(section.get("timeout_seconds", 90)),
        min_interval_seconds=max(0.0, float(section.get("min_interval_seconds", 3.0))),
        max_retries=max(0, int(section.get("max_retries", 1))),
        min_pdf_bytes=max(1024, int(section.get("min_pdf_bytes", 10000))),
    )


def load_queue(path: str | Path) -> list[dict[str, Any]]:
    queue_path = Path(path)
    if not queue_path.is_file():
        raise DownloadError(f"Queue file does not exist: {queue_path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        queue_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DownloadError(f"Invalid JSON on queue line {line_number}: {exc}") from exc
        if not record.get("doi") or not record.get("title"):
            raise DownloadError(f"Queue line {line_number} needs doi and title")
        records.append(record)
    return records


def _safe_component(value: str, maximum: int = 100) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    value = re.sub(r"\s+", " ", value)
    return (value[:maximum].rstrip(" .") or "unknown")


def target_path(settings: DownloadSettings, paper: dict[str, Any]) -> Path:
    journal = _safe_component(str(paper.get("journal") or "Unknown Journal"))
    year = _safe_component(str(paper.get("year") or "Unknown Year"))
    doi_name = _safe_component(str(paper["doi"]).lower(), maximum=140)
    return settings.output_dir / journal / year / f"{doi_name}.pdf"


def build_candidate_urls(paper: dict[str, Any]) -> list[CandidateURL]:
    candidates: list[CandidateURL] = []
    landing_url = paper.get("landing_url") or f"https://doi.org/{paper['doi']}"

    for url in paper.get("candidate_pdf_urls") or []:
        candidates.append(CandidateURL(str(url), "metadata_pdf", landing_url))

    publisher = str(paper.get("publisher") or "").lower()
    doi = str(paper["doi"])
    if publisher == "aps" or doi.lower().startswith("10.1103/"):
        candidates.append(
            CandidateURL(f"https://link.aps.org/pdf/{doi}", "aps", landing_url)
        )

    pii = paper.get("pii")
    if publisher == "elsevier" and pii:
        article_url = f"https://www.sciencedirect.com/science/article/pii/{pii}"
        # Establish the publisher session before requesting the entitlement PDF.
        candidates.append(CandidateURL(str(landing_url), "elsevier_landing", None))
        candidates.append(
            CandidateURL(
                f"{article_url}/pdfft?isDTMRedir=true&download=true",
                "elsevier_pdfft",
                landing_url,
            )
        )

    for url in paper.get("candidate_landing_urls") or []:
        candidates.append(CandidateURL(str(url), "metadata_landing", landing_url))
    candidates.append(CandidateURL(str(landing_url), "publisher_landing", None))

    unique: list[CandidateURL] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized_url = candidate.url.replace("http://link.aps.org/", "https://link.aps.org/")
        if normalized_url not in seen:
            seen.add(normalized_url)
            unique.append(
                CandidateURL(normalized_url, candidate.source, candidate.referer)
            )
    return unique


def validate_pdf(path: Path, minimum_bytes: int) -> tuple[bool, str]:
    size = path.stat().st_size
    if size < minimum_bytes:
        return False, f"too_small:{size}"
    with path.open("rb") as handle:
        header = handle.read(8)
    if not header.startswith(b"%PDF-"):
        return False, "missing_pdf_signature"
    return True, "ok"


class DirectDownloader:
    def __init__(self, settings: DownloadSettings):
        self.settings = settings
        if CurlSession is None:
            raise DownloadError(
                "Missing curl-cffi. Run setup_download.bat once while Clash is available."
            )
        self.session = CurlSession(impersonate="chrome", trust_env=False)
        self.proxies = (
            None
            if settings.network_route == "direct"
            else {"http": settings.proxy_url, "https": settings.proxy_url}
        )
        self.last_request_started = 0.0

    def _wait(self) -> None:
        remaining = self.settings.min_interval_seconds - (
            time.monotonic() - self.last_request_started
        )
        if remaining > 0:
            time.sleep(remaining)

    def _open(self, candidate: CandidateURL):
        headers = {
            "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if candidate.referer:
            headers["Referer"] = candidate.referer
        attempts = self.settings.max_retries + 1
        for attempt in range(attempts):
            self._wait()
            self.last_request_started = time.monotonic()
            try:
                response = self.session.get(
                    candidate.url,
                    headers=headers,
                    proxies=self.proxies,
                    verify=True,
                    allow_redirects=True,
                    timeout=self.settings.timeout_seconds,
                )
                retryable = response.status_code == 429 or 500 <= response.status_code < 600
                if retryable and attempt + 1 < attempts:
                    retry_after = response.headers.get("Retry-After")
                    delay = min(float(retry_after), 30.0) if retry_after else 2.0**attempt
                    time.sleep(delay)
                    continue
                if not response.ok:
                    raise DownloadError(f"HTTP {response.status_code}")
                return response
            except CurlError as exc:
                if attempt + 1 < attempts:
                    time.sleep(2.0**attempt)
                    continue
                raise DownloadError(f"network_error:{exc}") from exc
        raise DownloadError("request_failed")

    def try_candidate(
        self, candidate: CandidateURL, destination: Path
    ) -> tuple[bool, dict[str, Any], list[CandidateURL]]:
        discovered: list[CandidateURL] = []
        part_path = destination.with_suffix(destination.suffix + ".part")
        try:
            response = self._open(candidate)
            final_url = response.url
            content_type = (response.headers.get("Content-Type") or "").lower()
            content = response.content
            looks_pdf = content.startswith(b"%PDF-") or "application/pdf" in content_type
            if looks_pdf:
                destination.parent.mkdir(parents=True, exist_ok=True)
                part_path.write_bytes(content)
                valid, reason = validate_pdf(part_path, self.settings.min_pdf_bytes)
                if not valid:
                    part_path.unlink(missing_ok=True)
                    return False, {
                        "url": candidate.url,
                        "final_url": final_url,
                        "source": candidate.source,
                        "result": reason,
                    }, discovered
                os.replace(part_path, destination)
                return True, {
                    "url": candidate.url,
                    "final_url": final_url,
                    "source": candidate.source,
                    "result": "downloaded",
                }, discovered

            parser = PDFLinkParser(final_url)
            parser.feed(content[: 5 * 1024 * 1024].decode("utf-8", errors="replace"))
            discovered = [
                CandidateURL(url, "html_discovery", final_url) for url in parser.urls[:10]
            ]
            return False, {
                "url": candidate.url,
                "final_url": final_url,
                "source": candidate.source,
                "result": f"not_pdf:{content_type or 'unknown'}",
                "discovered_pdf_links": len(discovered),
            }, discovered
        except DownloadError as exc:
            part_path.unlink(missing_ok=True)
            return False, {
                "url": candidate.url,
                "source": candidate.source,
                "result": str(exc),
            }, discovered

    def download_paper(self, paper: dict[str, Any]) -> dict[str, Any]:
        destination = target_path(self.settings, paper)
        if destination.is_file():
            valid, reason = validate_pdf(destination, self.settings.min_pdf_bytes)
            if valid:
                return self._success_record(paper, destination, "existing", [], "skipped")
            return {
                "doi": paper["doi"],
                "title": paper["title"],
                "journal": paper.get("journal"),
                "year": paper.get("year"),
                "status": "manual_pending",
                "landing_url": paper.get("landing_url") or f"https://doi.org/{paper['doi']}",
                "attempts": [{"source": "existing", "result": f"invalid_existing:{reason}"}],
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }

        queue = build_candidate_urls(paper)
        seen: set[str] = set()
        attempts: list[dict[str, Any]] = []
        while queue:
            candidate = queue.pop(0)
            if candidate.url in seen:
                continue
            seen.add(candidate.url)
            success, attempt, discovered = self.try_candidate(candidate, destination)
            attempts.append(attempt)
            queue[0:0] = [item for item in discovered if item.url not in seen]
            if success:
                return self._success_record(
                    paper, destination, attempt["source"], attempts, "downloaded"
                )

        return {
            "doi": paper["doi"],
            "title": paper["title"],
            "journal": paper.get("journal"),
            "year": paper.get("year"),
            "status": "manual_pending",
            "landing_url": paper.get("landing_url") or f"https://doi.org/{paper['doi']}",
            "attempts": attempts,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _success_record(
        paper: dict[str, Any],
        destination: Path,
        source: str,
        attempts: list[dict[str, Any]],
        status: str,
    ) -> dict[str, Any]:
        hasher = hashlib.sha256()
        with destination.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        return {
            "doi": paper["doi"],
            "title": paper["title"],
            "journal": paper.get("journal"),
            "year": paper.get("year"),
            "status": status,
            "source": source,
            "path": str(destination),
            "bytes": destination.stat().st_size,
            "sha256": digest,
            "attempts": attempts,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }


def _append_manifest(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_failures(path: Path, failures: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["doi", "title", "journal", "year", "landing_url", "last_error"],
        )
        writer.writeheader()
        for failure in failures:
            attempts = failure.get("attempts") or []
            writer.writerow(
                {
                    "doi": failure["doi"],
                    "title": failure["title"],
                    "journal": failure.get("journal"),
                    "year": failure.get("year"),
                    "landing_url": failure.get("landing_url"),
                    "last_error": attempts[-1]["result"] if attempts else "not_attempted",
                }
            )


def write_manual_page(path: Path, failures: list[dict[str, Any]]) -> None:
    rows = []
    for failure in failures:
        url = html.escape(str(failure.get("landing_url") or ""), quote=True)
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(failure.get('year') or ''))}</td>"
            f"<td>{html.escape(str(failure.get('journal') or ''))}</td>"
            f"<td>{html.escape(str(failure['title']))}</td>"
            f"<td><code>{html.escape(str(failure['doi']))}</code></td>"
            f'<td><a href="{url}">打开出版社页面</a></td>'
            "</tr>"
        )
    document = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>AutoPaper 人工下载队列</title>
<style>body{font-family:sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:.6rem;text-align:left}th{background:#f3f3f3}tr:nth-child(even){background:#fafafa}</style>
</head><body><h1>AutoPaper 人工下载队列</h1>
<p>以下论文未能通过直连脚本自动获得 PDF。请在校园网络直连状态下打开出版社页面。</p>
<table><thead><tr><th>年份</th><th>期刊</th><th>标题</th><th>DOI</th><th>入口</th></tr></thead><tbody>
""" + "\n".join(rows) + "\n</tbody></table></body></html>\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download queued papers using a direct connection.")
    parser.add_argument("--config", default="config/config.local.toml")
    parser.add_argument("--queue", default="examples/download_queue.jsonl")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_download_settings(args.config)
        papers = load_queue(args.queue)
        if args.limit is not None:
            papers = papers[: max(0, args.limit)]

        print(f"Network route: {settings.network_route}")
        print(f"Queued papers: {len(papers)}")
        for index, paper in enumerate(papers, start=1):
            print(f"[{index}] {paper['doi']} -> {target_path(settings, paper)}")
        if args.dry_run:
            print("Dry run only; no network requests were made.")
            return 0

        downloader = DirectDownloader(settings)
        failures: list[dict[str, Any]] = []
        downloaded = 0
        skipped = 0
        for index, paper in enumerate(papers, start=1):
            print(f"[{index}/{len(papers)}] {paper['title']}")
            result = downloader.download_paper(paper)
            _append_manifest(settings.manifest_file, result)
            if result["status"] == "downloaded":
                downloaded += 1
                print(f"  downloaded: {result['path']}")
            elif result["status"] == "skipped":
                skipped += 1
                print(f"  skipped existing: {result['path']}")
            else:
                failures.append(result)
                print("  manual_pending")

        write_failures(settings.failures_file, failures)
        write_manual_page(settings.manual_file, failures)
        print(f"Downloaded: {downloaded}; skipped: {skipped}; manual: {len(failures)}")
        print(f"Manifest: {settings.manifest_file.resolve()}")
        print(f"Manual page: {settings.manual_file.resolve()}")
        return 0
    except (DownloadError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
