"""Resumable PDF -> Markdown via MinerU v4, integrated with AutoPaper SQLite.

API sequence follows Nano-Li/minerU-kit (db9e99b); implemented here using the
existing curl-cffi dependency. No upstream checkout is required at runtime.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import time
import tomllib
import uuid
import zipfile

from curl_cffi import requests

from .download import _safe_component
from .workflow import (
    _utc_now, connect, load_workflow_settings, paper_code,
    record_markdown, render_status,
)

API = "https://mineru.net/api/v4"


class MinerUError(RuntimeError):
    def __init__(self, message: str, *, stop: bool = False):
        super().__init__(message)
        self.stop = stop


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    output_dir: Path = Path("paper_inbox/markdown")
    model: str = "pipeline"
    language: str = "en"
    ocr: bool = False
    network_mode: str = "direct"
    proxy_url: str = ""
    poll_seconds: int = 10
    timeout_seconds: int = 1800

    def options(self) -> dict:
        return dict(model_version=self.model, language=self.language,
                    enable_formula=True, enable_table=True)


def load_settings(path: str | Path) -> Settings:
    with Path(path).open("rb") as handle:
        data = tomllib.load(handle)["mineru"]
    settings = Settings(**data)
    if settings.model not in {"pipeline", "vlm"}:
        raise ValueError("MinerU model must be pipeline or vlm")
    if settings.network_mode not in {"direct", "proxy"}:
        raise ValueError("MinerU network_mode must be direct or proxy")
    if settings.network_mode == "proxy" and not settings.proxy_url:
        raise ValueError("MinerU proxy mode requires proxy_url")
    if settings.poll_seconds < 3 or settings.timeout_seconds < 1:
        raise ValueError("Invalid MinerU polling interval or timeout")
    return settings


def prepare_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS mineru_jobs (
            paper_id INTEGER PRIMARY KEY REFERENCES papers(id),
            pdf_sha256 TEXT NOT NULL, options_json TEXT NOT NULL,
            output_dir TEXT NOT NULL, batch_id TEXT, upload_url TEXT,
            uploaded INTEGER NOT NULL DEFAULT 0,
            remote_failed INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, updated_at TEXT NOT NULL
        )
    """)
    connection.commit()


@contextmanager
def conversion_lock(database: Path):
    """OS lock is released on process exit, including interrupted runs."""
    path = Path(str(database) + ".mineru.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MinerUError("Another Markdown conversion is using this database") from None
        try:
            yield
        finally:
            handle.seek(0)
            if __import__("os").name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def staging_directory(parent: Path):
    # Ordinary inherited permissions: Windows tempfile's private ACL would follow
    # a renamed output directory and prevent other workspace readers opening it.
    root = parent.resolve()
    staging = root / (".mineru-" + uuid.uuid4().hex)
    staging.mkdir()
    try:
        yield staging
    finally:
        if staging.resolve().parent == root and staging.name.startswith(".mineru-"):
            shutil.rmtree(staging)


class Client:
    def __init__(self, settings: Settings):
        self.settings = settings
        # Explicitly ignore environment/OS proxies unless configured here.
        proxy = settings.proxy_url if settings.network_mode == "proxy" else ""
        self.session = requests.Session(trust_env=False, proxies={"all": proxy})

    def close(self):
        self.session.close()

    def request(self, method: str, url: str, *, authenticated=False, **kwargs):
        if not url.startswith("https://"):
            raise MinerUError("MinerU returned a non-HTTPS URL")
        headers = {"Authorization": f"Bearer {self.settings.token}"} if authenticated else {}
        if method == "PUT":
            # curl-cffi otherwise inserts application/octet-stream for byte bodies.
            headers["Content-Type"] = None
        try:
            response = self.session.request(
                method, url, headers=headers, timeout=120,
                allow_redirects=not authenticated, **kwargs,
            )
        except requests.RequestsError:
            # Exception strings may contain signed URLs. Never log them.
            raise MinerUError("Network request failed; retry to resume the saved task") from None
        if not 200 <= response.status_code < 300:
            code = response.status_code
            response.close()
            raise MinerUError(f"HTTP {code}" + ("; check token/quota" if authenticated else ""),
                              stop=authenticated and code in {401, 403, 429})
        return response

    def api(self, method: str, path: str, **kwargs) -> dict:
        response = self.request(method, API + path, authenticated=True, **kwargs)
        try:
            data = response.json()
        except ValueError:
            raise MinerUError("MinerU returned invalid JSON") from None
        finally:
            response.close()
        if data.get("code") != 0:
            # Codes are enough to diagnose without echoing server-provided secrets.
            raise MinerUError(f"MinerU API error code: {data.get('code')}", stop=True)
        return data.get("data") or {}

    def submit(self, code: str) -> tuple[str, str]:
        body = self.settings.options()
        body["files"] = [{"name": code + ".pdf", "data_id": code,
                          "is_ocr": self.settings.ocr}]
        data = self.api("POST", "/file-urls/batch", json=body)
        if not data.get("batch_id") or len(data.get("file_urls", [])) != 1:
            raise MinerUError("MinerU response lacks a batch ID or upload URL", stop=True)
        return data["batch_id"], data["file_urls"][0]

    def upload(self, url: str, pdf: Path):
        # Signed upload: no Authorization or Content-Type headers (upstream rule).
        response = self.request("PUT", url, data=pdf.read_bytes())
        response.close()

    def result(self, batch_id: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", batch_id):
            raise MinerUError("Invalid saved batch ID")
        data = self.api("GET", "/extract-results/batch/" + batch_id)
        results = data.get("extract_result") or []
        if len(results) > 1:
            raise MinerUError("Unexpected multiple results for a single-paper task")
        return results[0] if results else {}

    def download(self, url: str, path: Path):
        response = self.request("GET", url, stream=True)
        try:
            with path.open("wb") as handle:
                total = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    total += len(chunk)
                    if total > 512 * 1024 * 1024:
                        raise MinerUError("Result archive exceeds 512 MB")
                    handle.write(chunk)
        except requests.RequestsError:
            raise MinerUError("Result download interrupted; retry to resume") from None
        finally:
            response.close()


def extract_result(archive: Path, destination: Path) -> Path:
    """Validate before extracting; never accept archive paths outside output."""
    with zipfile.ZipFile(archive) as bundle:
        members = []
        size = 0
        for info in bundle.infolist():
            name = PurePosixPath(info.filename.replace("\\", "/"))
            if (name.is_absolute() or ".." in name.parts or ":" in str(name)
                    or stat.S_ISLNK(info.external_attr >> 16)):
                raise MinerUError("Unsafe path in MinerU result archive")
            if info.is_dir() or "__MACOSX" in name.parts or name.suffix.lower() == ".pdf":
                continue
            size += info.file_size
            if size > 1024 * 1024 * 1024 or len(members) >= 20000:
                raise MinerUError("MinerU result archive is too large")
            members.append((info, name))
        markdowns = [(info, name) for info, name in members if name.suffix.lower() == ".md"]
        preferred = [item for item in markdowns if item[1].name == "full.md"]
        choices = preferred or markdowns
        if len(choices) != 1 or not bundle.read(choices[0][0]).decode("utf-8-sig").strip():
            raise MinerUError("Result must contain one identifiable, nonempty Markdown document")
        # Validate all names before touching the output directory.
        for info, name in members:
            target = destination.joinpath(*name.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise MinerUError("Unsafe output path")
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    return destination.joinpath(*choices[0][1].parts)


def select_papers(connection, codes=None, limit=None, retry_failed=False):
    states = ["pending", "processing"] + (["failed"] if retry_failed else [])
    query = "SELECT * FROM papers WHERE download_status='downloaded' AND markdown_status IN (" + ",".join("?" for _ in states) + ")"
    params = list(states)
    if codes:
        if any(not re.fullmatch(r"P\d+", code) for code in codes):
            raise ValueError("Paper IDs must look like P0001")
        query += " AND id IN (" + ",".join("?" for _ in codes) + ")"
        params.extend(int(code[1:]) for code in codes)
    query += " ORDER BY id"
    if limit is not None:
        if limit < 1:
            raise ValueError("Limit must be positive")
        query += " LIMIT ?"
        params.append(limit)
    return connection.execute(query, params).fetchall()


def convert_paper(connection, row, settings: Settings, client: Client, *, retry_failed=False):
    code = paper_code(row)
    pdf = Path(row["pdf_path"] or "")
    if not pdf.is_file() or pdf.stat().st_size > 200 * 1024 * 1024:
        raise MinerUError("PDF is missing or exceeds 200 MB")
    with pdf.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise MinerUError("Input is not a PDF")
        handle.seek(0)
        fingerprint = hashlib.file_digest(handle, "sha256").hexdigest()
    options = json.dumps(dict(settings.options(), is_ocr=settings.ocr), sort_keys=True)
    job = connection.execute("SELECT * FROM mineru_jobs WHERE paper_id=?", (row["id"],)).fetchone()
    if job and (job["pdf_sha256"] != fingerprint or job["options_json"] != options):
        raise MinerUError("PDF or parser options changed; restore them before resuming")
    if not job or (retry_failed and job["remote_failed"]):
        # A fresh attempt gets its own folder; existing output is never overwritten.
        folder = (Path(settings.output_dir) / _safe_component(row["journal"] or "Unknown Journal", 60)
                  / str(row["publication_year"] or "unknown") / (code + "-" + uuid.uuid4().hex[:12]))
        with connection:
            connection.execute("""INSERT OR REPLACE INTO mineru_jobs
                (paper_id,pdf_sha256,options_json,output_dir,updated_at) VALUES (?,?,?,?,?)""",
                (row["id"], fingerprint, options, str(folder.resolve()), _utc_now()))
        job = connection.execute("SELECT * FROM mineru_jobs WHERE paper_id=?", (row["id"],)).fetchone()
    record_markdown(connection, code, "processing", None)
    batch_id = job["batch_id"]
    upload_url = job["upload_url"]
    if not batch_id:
        batch_id, upload_url = client.submit(code)
        with connection:
            connection.execute("UPDATE mineru_jobs SET batch_id=?,upload_url=?,last_error=NULL,updated_at=? WHERE paper_id=?",
                               (batch_id, upload_url, _utc_now(), row["id"]))
    if not job["uploaded"]:
        # Recover a crash after upload but before its local checkpoint.
        remote = client.result(batch_id)
        if remote.get("state", "waiting-file") == "waiting-file":
            client.upload(upload_url, pdf)
        with connection:
            connection.execute("UPDATE mineru_jobs SET uploaded=1,upload_url=NULL,updated_at=? WHERE paper_id=?",
                               (_utc_now(), row["id"]))
    deadline = time.monotonic() + settings.timeout_seconds
    previous = None
    while time.monotonic() < deadline:
        result = client.result(batch_id)
        state = result.get("state", "pending")
        if state != previous:
            print(f"{code}: {state}", flush=True)
            previous = state
        if state == "failed":
            with connection:
                connection.execute("UPDATE mineru_jobs SET remote_failed=1 WHERE paper_id=?", (row["id"],))
            raise MinerUError("MinerU parsing failed; see MinerU task dashboard, then --retry-failed")
        if state == "done":
            url = result.get("full_zip_url")
            if not url:
                raise MinerUError("Completed task lacks a result ZIP URL")
            output = Path(job["output_dir"])
            output.parent.mkdir(parents=True, exist_ok=True)
            with staging_directory(output.parent) as staging:
                archive = Path(staging) / "result.zip"
                client.download(url, archive)
                extracted = Path(staging) / "extracted"
                markdown = extract_result(archive, extracted)
                relative = markdown.relative_to(extracted)
                # A crash may leave the finished folder before the DB checkpoint.
                if output.exists():
                    existing = output / relative
                    if not existing.is_file() or existing.read_bytes() != markdown.read_bytes():
                        raise MinerUError("Existing result differs; refusing to overwrite")
                else:
                    extracted.rename(output)
                record_markdown(connection, code, "done", str(output / relative))
            with connection:
                connection.execute("UPDATE mineru_jobs SET last_error=NULL,updated_at=? WHERE paper_id=?", (_utc_now(), row["id"]))
            return "done"
        time.sleep(min(settings.poll_seconds, max(0, deadline - time.monotonic())))
    # Timeout is not a failed remote task. The next run polls the same batch.
    print(f"{code}: waiting (saved; rerun to resume)", flush=True)
    return "processing"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.local.toml")
    parser.add_argument("--mineru-config", default="config/mineru.local.toml")
    parser.add_argument("--paper-id", nargs="+")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Only show counts; no uploads")
    args = parser.parse_args(argv)
    client = None
    connection = None
    try:
        workflow = load_workflow_settings(args.config)
        settings = load_settings(args.mineru_config)
        if not workflow.database.is_file():
            raise ValueError("Workflow database does not exist; no PDF queue available")
        with conversion_lock(workflow.database):
            connection = connect(workflow.database)
            rows = select_papers(connection, args.paper_id, args.limit, args.retry_failed)
            print(f"Queued PDFs: {len(rows)}; route: {settings.network_mode}; model: {settings.model}", flush=True)
            if args.dry_run or not rows:
                return 0
            if not settings.token.strip():
                raise ValueError("Set token in config/mineru.local.toml")
            prepare_schema(connection)
            client = Client(settings)
            counts = {"done": 0, "processing": 0, "failed": 0}
            for row in rows:
                code = paper_code(row)
                stop = False
                try:
                    outcome = convert_paper(connection, row, settings, client, retry_failed=args.retry_failed)
                except (MinerUError, OSError, ValueError, zipfile.BadZipFile) as exc:
                    # Only our controlled API errors are safe to display verbatim.
                    message = str(exc) if isinstance(exc, MinerUError) else type(exc).__name__
                    record_markdown(connection, code, "failed", None)
                    with connection:
                        connection.execute("UPDATE mineru_jobs SET last_error=?,updated_at=? WHERE paper_id=?", (message, _utc_now(), row["id"]))
                    print(f"{code}: failed ({message})", flush=True)
                    outcome = "failed"
                    stop = isinstance(exc, MinerUError) and exc.stop
                counts[outcome] += 1
                render_status(connection, workflow.status_file)
                if stop:
                    break
            print(json.dumps(counts), flush=True)
            return 2 if counts["failed"] or counts["processing"] else 0
    except KeyboardInterrupt:
        print("Interrupted; saved remote tasks will resume on the next run.", flush=True)
        return 130
    except (MinerUError, ValueError, OSError) as exc:
        print(f"Cannot start: {exc}" if isinstance(exc, (MinerUError, ValueError)) else f"Cannot start: {type(exc).__name__}")
        return 2
    finally:
        if client:
            client.close()
        if connection:
            if client:
                render_status(connection, workflow.status_file)
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
