from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tomllib
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


SCREENING_DECISIONS = {"pending", "include", "exclude", "uncertain"}
HUMAN_DECISIONS = {"pending", "approved", "rejected", "not_required"}
DOWNLOAD_STATUSES = {
    "not_queued",
    "queued",
    "downloading",
    "downloaded",
    "failed",
    "manual_pending",
}
MARKDOWN_STATUSES = {"not_ready", "pending", "processing", "done", "failed"}


@dataclass(frozen=True)
class WorkflowSettings:
    database: Path
    review_file: Path
    status_file: Path
    download_queue_file: Path
    criteria: str
    decision_prompt: str
    model: str
    reasoning_effort: str
    max_parallel_agents: int
    supplementary: bool
    review_include_excluded: bool = False
    review_source_run: Path | None = None
    review_archive_dir: Path = Path("workspace/reviews")


@dataclass(frozen=True)
class ScreeningProfile:
    id: int
    profile_key: str
    criteria: str
    decision_prompt: str
    created_at: str


@dataclass(frozen=True)
class ImportSummary:
    total: int
    inserted: int
    matched_existing: int
    reused_screening: int
    pending_screening: int
    reset_for_rescreening: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalized_identity_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(normalized.split())


def _identity_key(title: str, journal: str | None) -> str:
    identity = "\0".join(
        (_normalized_identity_text(journal), _normalized_identity_text(title))
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _contains_chinese(value: str) -> bool:
    return any("\u3400" <= character <= "\u9fff" for character in value)


def _ensure_column(
    connection: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    columns = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def load_workflow_settings(path: str | Path) -> WorkflowSettings:
    with Path(path).open("rb") as handle:
        data = tomllib.load(handle)
    screening = data.get("screening", {})
    workflow = data.get("workflow", {})
    download = data.get("download", {})
    max_parallel = int(screening.get("max_parallel_agents", 3))
    if not 1 <= max_parallel <= 3:
        raise ValueError("[screening] max_parallel_agents must be between 1 and 3")
    model = str(screening.get("model", "gpt-5.6-luna")).strip()
    effort = str(screening.get("reasoning_effort", "low")).strip().lower()
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Unsupported [screening] reasoning_effort")
    return WorkflowSettings(
        database=Path(str(workflow.get("database", "workspace/state.sqlite3"))),
        review_file=Path(str(workflow.get("review_file", "workspace/review.md"))),
        status_file=Path(str(workflow.get("status_file", "workspace/STATUS.md"))),
        download_queue_file=Path(
            str(workflow.get("download_queue_file", "workspace/download_queue.jsonl"))
        ),
        criteria=str(screening.get("criteria", "")).strip(),
        decision_prompt=str(screening.get("decision_prompt", "")).strip(),
        model=model,
        reasoning_effort=effort,
        max_parallel_agents=max_parallel,
        supplementary=bool(download.get("supplementary", False)),
        review_include_excluded=bool(
            workflow.get("review_include_excluded", False)
        ),
        review_source_run=(
            Path(str(workflow["review_source_run"])).resolve()
            if workflow.get("review_source_run")
            else None
        ),
        review_archive_dir=Path(
            str(workflow.get("review_archive_dir", "workspace/reviews"))
        ),
    )


def connect(database: str | Path) -> sqlite3.Connection:
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS papers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_key TEXT NOT NULL UNIQUE,
            identity_key TEXT,
            doi TEXT,
            title TEXT NOT NULL,
            abstract TEXT,
            journal TEXT,
            publication_year INTEGER,
            landing_url TEXT,
            source_run TEXT,
            retrieval_rank INTEGER,
            screening_status TEXT NOT NULL DEFAULT 'pending',
            screening_reason TEXT,
            screening_model TEXT,
            screening_profile_id INTEGER,
            screening_reused INTEGER NOT NULL DEFAULT 0,
            screened_at TEXT,
            human_status TEXT NOT NULL DEFAULT 'pending',
            human_decided_at TEXT,
            download_status TEXT NOT NULL DEFAULT 'not_queued',
            pdf_path TEXT,
            pdf_bytes INTEGER,
            pdf_validated_at TEXT,
            markdown_status TEXT NOT NULL DEFAULT 'not_ready',
            markdown_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_papers_screening ON papers(screening_status);
        CREATE INDEX IF NOT EXISTS idx_papers_human ON papers(human_status);
        CREATE INDEX IF NOT EXISTS idx_papers_download ON papers(download_status);
        CREATE INDEX IF NOT EXISTS idx_papers_markdown ON papers(markdown_status);
        CREATE TABLE IF NOT EXISTS screening_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_key TEXT NOT NULL UNIQUE,
            criteria TEXT NOT NULL,
            decision_prompt TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS screening_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id INTEGER NOT NULL REFERENCES papers(id),
            profile_id INTEGER NOT NULL REFERENCES screening_profiles(id),
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            model TEXT NOT NULL,
            screened_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_screening_results_paper
            ON screening_results(paper_id, profile_id);
        CREATE TABLE IF NOT EXISTS workflow_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    _ensure_column(connection, "papers", "identity_key", "TEXT")
    _ensure_column(connection, "papers", "screening_profile_id", "INTEGER")
    _ensure_column(
        connection, "papers", "screening_reused", "INTEGER NOT NULL DEFAULT 0"
    )
    rows = connection.execute(
        "SELECT id, title, journal FROM papers WHERE identity_key IS NULL"
    ).fetchall()
    with connection:
        connection.executemany(
            "UPDATE papers SET identity_key = ? WHERE id = ?",
            [(_identity_key(row["title"], row["journal"]), row["id"]) for row in rows],
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_papers_identity ON papers(identity_key)"
        )
    return connection


def paper_code(row_or_id: sqlite3.Row | int) -> str:
    value = row_or_id["id"] if isinstance(row_or_id, sqlite3.Row) else row_or_id
    return f"P{int(value):04d}"


def _paper_key(candidate: dict[str, Any]) -> str:
    doi = str(candidate.get("doi") or "").strip().lower()
    if doi:
        return f"doi:{doi}"
    openalex_id = str(candidate.get("openalex_id") or "").strip().lower()
    if openalex_id:
        return f"openalex:{openalex_id}"
    journal = candidate.get("journal") or {}
    journal_name = journal.get("name") if isinstance(journal, dict) else journal
    fallback = "|".join(
        [
            str(candidate.get("title") or "").strip().casefold(),
            str(journal_name or "").strip().casefold(),
            str(candidate.get("publication_year") or ""),
        ]
    )
    return "fallback:" + hashlib.sha256(fallback.encode("utf-8")).hexdigest()


def _screening_profile_key(settings: WorkflowSettings) -> str:
    payload = json.dumps(
        {
            "criteria": settings.criteria,
            "decision_prompt": settings.decision_prompt,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ensure_screening_profile(
    connection: sqlite3.Connection, settings: WorkflowSettings
) -> ScreeningProfile:
    profile_key = _screening_profile_key(settings)
    stored = connection.execute(
        "SELECT * FROM screening_profiles ORDER BY id"
    ).fetchall()
    if len(stored) > 1:
        raise ValueError(
            "This database contains multiple screening profiles; choose a specific "
            "database/profile before continuing"
        )
    if stored and stored[0]["profile_key"] != profile_key:
        raise ValueError(
            "The configured screening prompt does not match this database's stored "
            f"profile (stored {stored[0]['profile_key'][:12]}, current {profile_key[:12]}). "
            "Choose the intended database or create a new database for the new prompt."
        )
    now = _utc_now()
    with connection:
        if not stored:
            connection.execute(
                """
                INSERT INTO screening_profiles (
                    profile_key, criteria, decision_prompt, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (profile_key, settings.criteria, settings.decision_prompt, now),
            )
        profile = connection.execute(
            "SELECT * FROM screening_profiles WHERE profile_key = ?", (profile_key,)
        ).fetchone()
        assert profile is not None
        connection.execute(
            """
            INSERT INTO screening_results (
                paper_id, profile_id, decision, reason, model, screened_at
            )
            SELECT p.id, ?, p.screening_status, p.screening_reason,
                   COALESCE(p.screening_model, 'legacy'),
                   COALESCE(p.screened_at, p.updated_at)
            FROM papers AS p
            WHERE p.screening_status != 'pending'
              AND p.screening_reason IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM screening_results AS r
                  WHERE r.paper_id = p.id AND r.profile_id = ?
              )
            """,
            (profile["id"], profile["id"]),
        )
        connection.execute(
            """
            UPDATE papers SET screening_profile_id = ?
            WHERE screening_status != 'pending' AND screening_profile_id IS NULL
            """,
            (profile["id"],),
        )
    return ScreeningProfile(
        id=profile["id"],
        profile_key=profile["profile_key"],
        criteria=profile["criteria"],
        decision_prompt=profile["decision_prompt"],
        created_at=profile["created_at"],
    )


def _set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO workflow_meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def _get_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute(
        "SELECT value FROM workflow_meta WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def active_source_run(connection: sqlite3.Connection) -> str | None:
    return _get_meta(connection, "active_source_run")


def _iter_candidates(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    if path.suffix.casefold() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for line_number, row in enumerate(csv.DictReader(handle), start=2):
                candidate: dict[str, Any] = dict(row)
                candidate["journal"] = {"name": row.get("journal")}
                for key in ("publication_year", "retrieval_rank", "cited_by_count"):
                    raw_value = row.get(key)
                    candidate[key] = int(raw_value) if raw_value else None
                yield line_number, candidate
        return
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                yield line_number, json.loads(line)


def import_candidates(
    connection: sqlite3.Connection,
    candidates_path: str | Path,
    reuse_existing: bool = True,
) -> ImportSummary:
    path = Path(candidates_path)
    if path.suffix.casefold() not in {".jsonl", ".csv"}:
        raise ValueError("Candidate input must be .jsonl or .csv")
    total = 0
    inserted = 0
    matched_existing = 0
    reused_screening = 0
    reset_for_rescreening = 0
    now = _utc_now()
    resolved_path = str(path.resolve())
    with connection:
        for line_number, candidate in _iter_candidates(path):
            total += 1
            title = str(candidate.get("title") or "").strip()
            if not title:
                raise ValueError(f"Candidate on line {line_number} has no title")
            journal = candidate.get("journal") or {}
            journal_name = journal.get("name") if isinstance(journal, dict) else journal
            journal_name = str(journal_name or "").strip() or None
            values = {
                "paper_key": _paper_key(candidate),
                "identity_key": _identity_key(title, journal_name),
                "doi": str(candidate.get("doi") or "").strip() or None,
                "title": title,
                "abstract": candidate.get("abstract"),
                "journal": journal_name,
                "publication_year": candidate.get("publication_year")
                or candidate.get("year"),
                "landing_url": candidate.get("landing_page_url")
                or candidate.get("landing_url"),
                "source_run": resolved_path,
                "retrieval_rank": candidate.get("retrieval_rank"),
                "now": now,
            }
            existing = connection.execute(
                "SELECT * FROM papers WHERE identity_key = ? ORDER BY id LIMIT 1",
                (values["identity_key"],),
            ).fetchone()
            if existing is None:
                existing = connection.execute(
                    "SELECT * FROM papers WHERE paper_key = ?", (values["paper_key"],)
                ).fetchone()
            if existing is not None:
                matched_existing += 1
                was_screened = existing["screening_status"] != "pending"
                reused = bool(reuse_existing and was_screened)
                reused_screening += int(reused)
                if not reuse_existing and was_screened:
                    reset_for_rescreening += 1
                connection.execute(
                    """
                    UPDATE papers SET
                        identity_key = :identity_key,
                        doi = COALESCE(:doi, doi),
                        title = :title,
                        abstract = COALESCE(:abstract, abstract),
                        journal = COALESCE(:journal, journal),
                        publication_year = COALESCE(:publication_year, publication_year),
                        landing_url = COALESCE(:landing_url, landing_url),
                        source_run = :source_run,
                        retrieval_rank = :retrieval_rank,
                        screening_status = CASE
                            WHEN :reuse_existing THEN screening_status ELSE 'pending' END,
                        screening_reason = CASE
                            WHEN :reuse_existing THEN screening_reason ELSE NULL END,
                        screening_model = CASE
                            WHEN :reuse_existing THEN screening_model ELSE NULL END,
                        screening_profile_id = CASE
                            WHEN :reuse_existing THEN screening_profile_id ELSE NULL END,
                        screening_reused = :screening_reused,
                        screened_at = CASE
                            WHEN :reuse_existing THEN screened_at ELSE NULL END,
                        human_status = CASE
                            WHEN :reuse_existing THEN human_status
                            ELSE 'pending' END,
                        human_decided_at = CASE
                            WHEN :reuse_existing THEN human_decided_at ELSE NULL END,
                        updated_at = :now
                    WHERE id = :existing_id
                    """,
                    {
                        **values,
                        "reuse_existing": int(reuse_existing),
                        "screening_reused": int(reused),
                        "existing_id": existing["id"],
                    },
                )
            else:
                inserted += 1
                connection.execute(
                    """
                    INSERT INTO papers (
                        paper_key, identity_key, doi, title, abstract, journal,
                        publication_year, landing_url, source_run, retrieval_rank,
                        created_at, updated_at
                    ) VALUES (
                        :paper_key, :identity_key, :doi, :title, :abstract, :journal,
                        :publication_year, :landing_url, :source_run, :retrieval_rank,
                        :now, :now
                    )
                    """,
                    values,
                )
        _set_meta(connection, "active_source_run", resolved_path)
    pending_screening = connection.execute(
        """
        SELECT COUNT(*) FROM papers
        WHERE source_run = ? AND screening_status = 'pending'
        """,
        (resolved_path,),
    ).fetchone()[0]
    return ImportSummary(
        total=total,
        inserted=inserted,
        matched_existing=matched_existing,
        reused_screening=reused_screening,
        pending_screening=pending_screening,
        reset_for_rescreening=reset_for_rescreening,
    )


def _resolve_paper(connection: sqlite3.Connection, code: str) -> sqlite3.Row:
    normalized = code.strip().upper()
    if normalized.startswith("P"):
        normalized = normalized[1:]
    if not normalized.isdigit():
        raise ValueError(f"Invalid paper id: {code}")
    row = connection.execute("SELECT * FROM papers WHERE id = ?", (int(normalized),)).fetchone()
    if row is None:
        raise ValueError(f"Unknown paper id: {code}")
    return row


def pending_screening_tasks(
    connection: sqlite3.Connection, settings: WorkflowSettings, limit: int | None = None
) -> list[dict[str, Any]]:
    requested = settings.max_parallel_agents if limit is None else limit
    if not 1 <= requested <= settings.max_parallel_agents:
        raise ValueError(
            f"limit must be between 1 and configured max {settings.max_parallel_agents}"
        )
    source_run = active_source_run(connection)
    if source_run is None:
        rows = connection.execute(
            """
            SELECT id, title, abstract
            FROM papers
            WHERE screening_status = 'pending'
            ORDER BY COALESCE(retrieval_rank, id), id
            LIMIT ?
            """,
            (requested,),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT id, title, abstract
            FROM papers
            WHERE screening_status = 'pending' AND source_run = ?
            ORDER BY COALESCE(retrieval_rank, id), id
            LIMIT ?
            """,
            (source_run, requested),
        ).fetchall()
    return [
        {
            "paper_id": paper_code(row),
            "title": row["title"],
            "abstract": row["abstract"],
            "criteria": settings.criteria,
            "instructions": settings.decision_prompt,
            "required_output": {
                "paper_id": paper_code(row),
                "decision": "include | exclude | uncertain",
                "reason": "one or two concise sentences",
            },
        }
        for row in rows
    ]


def record_screening(
    connection: sqlite3.Connection,
    code: str,
    decision: str,
    reason: str,
    model: str,
    profile_id: int,
) -> None:
    decision = decision.strip().lower()
    if decision not in SCREENING_DECISIONS - {"pending"}:
        raise ValueError("Screening decision must be include, exclude, or uncertain")
    compact_reason = " ".join(reason.split())
    if not compact_reason:
        raise ValueError("Screening reason cannot be empty")
    if not _contains_chinese(compact_reason):
        raise ValueError("Screening reason must contain a concise Chinese explanation")
    row = _resolve_paper(connection, code)
    human_status = "not_required" if decision == "exclude" else "pending"
    if row["human_status"] in {"approved", "rejected"}:
        human_status = row["human_status"]
    now = _utc_now()
    with connection:
        connection.execute(
            """
            INSERT INTO screening_results (
                paper_id, profile_id, decision, reason, model, screened_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (row["id"], profile_id, decision, compact_reason, model, now),
        )
        connection.execute(
            """
            UPDATE papers
            SET screening_status = ?, screening_reason = ?, screening_model = ?,
                screening_profile_id = ?, screening_reused = 0,
                screened_at = ?, human_status = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                decision,
                compact_reason,
                model,
                profile_id,
                now,
                human_status,
                now,
                row["id"],
            ),
        )


def set_human_decision(
    connection: sqlite3.Connection, codes: Iterable[str], decision: str
) -> int:
    decision = decision.strip().lower()
    if decision not in {"approved", "rejected"}:
        raise ValueError("Human decision must be approved or rejected")
    now = _utc_now()
    changed = 0
    with connection:
        for code in codes:
            row = _resolve_paper(connection, code)
            if row["screening_status"] not in {"include", "uncertain"}:
                raise ValueError(
                    f"{paper_code(row)} is not shown for human review and cannot be approved"
                )
            connection.execute(
                """
                UPDATE papers
                SET human_status = ?, human_decided_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (decision, now, now, row["id"]),
            )
            changed += 1
    return changed


def approve_all_included(connection: sqlite3.Connection) -> int:
    now = _utc_now()
    with connection:
        cursor = connection.execute(
            """
            UPDATE papers
            SET human_status = 'approved', human_decided_at = ?, updated_at = ?
            WHERE screening_status = 'include' AND human_status = 'pending'
            """,
            (now, now),
        )
    return cursor.rowcount


def _counts(
    connection: sqlite3.Connection,
    column: str,
    source_run: str | Path | None = None,
) -> dict[str, int]:
    if column not in {
        "screening_status",
        "human_status",
        "download_status",
        "markdown_status",
    }:
        raise ValueError(f"Unsupported status column: {column}")
    if source_run is None:
        rows = connection.execute(
            f"SELECT {column}, COUNT(*) FROM papers GROUP BY {column}"
        ).fetchall()
    else:
        rows = connection.execute(
            f"SELECT {column}, COUNT(*) FROM papers "
            f"WHERE source_run = ? GROUP BY {column}",
            (str(Path(source_run).resolve()),),
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def render_review(
    connection: sqlite3.Connection,
    output_path: str | Path,
    include_excluded: bool = False,
    source_run: str | Path | None = None,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    resolved_source_run = str(Path(source_run).resolve()) if source_run else None
    if resolved_source_run is None:
        total = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    else:
        total = connection.execute(
            "SELECT COUNT(*) FROM papers WHERE source_run = ?",
            (resolved_source_run,),
        ).fetchone()[0]
    screening = _counts(connection, "screening_status", source_run)
    human = _counts(connection, "human_status", source_run)
    if resolved_source_run is None:
        reused_screening = connection.execute(
            "SELECT COUNT(*) FROM papers WHERE screening_reused = 1"
        ).fetchone()[0]
    else:
        reused_screening = connection.execute(
            """
            SELECT COUNT(*) FROM papers
            WHERE screening_reused = 1 AND source_run = ?
            """,
            (resolved_source_run,),
        ).fetchone()[0]
    lines = [
        "# AutoPaper 人工复核",
        "",
        f"生成时间：{_utc_now()}",
        "",
        "## 总览",
        "",
        "| 项目 | 数量 |",
        "| --- | ---: |",
        f"| 候选论文 | {total} |",
        f"| 待筛选 | {screening.get('pending', 0)} |",
        f"| 建议下载 | {screening.get('include', 0)} |",
        f"| 需要确认 | {screening.get('uncertain', 0)} |",
        f"| 已由 Sub Agent 排除 | {screening.get('exclude', 0)} |",
        f"| 复用历史筛选结果 | {reused_screening} |",
        f"| 人工批准 | {human.get('approved', 0)} |",
        f"| 人工排除 | {human.get('rejected', 0)} |",
        "",
        (
            "> 本页由脚本生成，展示建议下载、需要确认和已由 Sub Agent 排除的论文。"
            if include_excluded
            else "> 本页由脚本生成，只展示建议下载和需要确认的论文。"
        )
        + "摘要原样来自检索结果。",
    ]
    sections = [("include", "建议下载"), ("uncertain", "需要确认")]
    if include_excluded:
        sections.append(("exclude", "Sub Agent 排除"))
    for decision, heading in sections:
        lines.extend(["", f"## {heading}", ""])
        query = """
            SELECT id, title, abstract, screening_reason
            FROM papers
            WHERE screening_status = ?
        """
        params: tuple[Any, ...] = (decision,)
        if resolved_source_run is not None:
            query += " AND source_run = ?"
            params += (resolved_source_run,)
        query += " ORDER BY COALESCE(retrieval_rank, id), id"
        rows = connection.execute(query, params).fetchall()
        if not rows:
            lines.append("（无）")
            continue
        for row in rows:
            lines.extend(
                [
                    f"### {paper_code(row)}",
                    "",
                    f"**标题：** {row['title']}",
                    "",
                    "**摘要：**",
                    "",
                    row["abstract"] or "（检索结果未提供摘要）",
                    "",
                    f"**判断理由：** {row['screening_reason'] or '（尚未填写）'}",
                    "",
                ]
            )
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return output


def _status_rows(connection: sqlite3.Connection, where: str) -> list[sqlite3.Row]:
    return connection.execute(
        f"SELECT id, title, download_status, markdown_status FROM papers WHERE {where} ORDER BY id"
    ).fetchall()


def render_status(connection: sqlite3.Connection, output_path: str | Path) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    total = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    screening = _counts(connection, "screening_status")
    human = _counts(connection, "human_status")
    download = _counts(connection, "download_status")
    markdown = _counts(connection, "markdown_status")
    waiting_download = connection.execute(
        """
        SELECT COUNT(*) FROM papers
        WHERE human_status = 'approved' AND download_status != 'downloaded'
        """
    ).fetchone()[0]
    lines = [
        "# AutoPaper 状态",
        "",
        f"更新时间：{_utc_now()}",
        "",
        "| 项目 | 数量 |",
        "| --- | ---: |",
        f"| 候选论文 | {total} |",
        f"| 待筛选 | {screening.get('pending', 0)} |",
        f"| 建议下载 | {screening.get('include', 0)} |",
        f"| 需要确认 | {screening.get('uncertain', 0)} |",
        f"| Sub Agent 排除 | {screening.get('exclude', 0)} |",
        f"| 人工批准 | {human.get('approved', 0)} |",
        f"| 待下载 | {waiting_download} |",
        f"| 已下载 | {download.get('downloaded', 0)} |",
        f"| 下载失败 | {download.get('failed', 0)} |",
        f"| 等待人工下载 | {download.get('manual_pending', 0)} |",
        f"| 待 Markdown 化 | {markdown.get('pending', 0)} |",
        f"| Markdown 处理中 | {markdown.get('processing', 0)} |",
        f"| Markdown 已完成 | {markdown.get('done', 0)} |",
        f"| Markdown 失败 | {markdown.get('failed', 0)} |",
    ]
    sections = [
        (
            "待下载",
            "human_status = 'approved' AND download_status != 'downloaded'",
            "download_status",
        ),
        ("已下载", "download_status = 'downloaded'", "markdown_status"),
        (
            "待 Markdown 化",
            "download_status = 'downloaded' AND markdown_status = 'pending'",
            "markdown_status",
        ),
    ]
    for heading, where, status_column in sections:
        lines.extend(["", f"## {heading}", ""])
        rows = _status_rows(connection, where)
        if rows:
            lines.extend(
                f"- {paper_code(row)}｜{row['title']}｜{row[status_column]}" for row in rows
            )
        else:
            lines.append("（无）")
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return output


def export_download_queue(
    connection: sqlite3.Connection, output_path: str | Path
) -> tuple[Path, int]:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = connection.execute(
        """
        SELECT * FROM papers
        WHERE screening_status IN ('include', 'uncertain')
          AND human_status = 'approved' AND download_status != 'downloaded'
        ORDER BY id
        """
    ).fetchall()
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            record = {
                "paper_id": paper_code(row),
                "doi": row["doi"],
                "title": row["title"],
                "journal": row["journal"],
                "year": row["publication_year"],
                "landing_url": row["landing_url"],
                "candidate_pdf_urls": [],
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    now = _utc_now()
    with connection:
        connection.execute(
            """
            UPDATE papers SET download_status = 'queued', updated_at = ?
            WHERE screening_status IN ('include', 'uncertain')
              AND human_status = 'approved' AND download_status != 'downloaded'
            """,
            (now,),
        )
    return output, len(rows)


def record_download(
    connection: sqlite3.Connection,
    code: str,
    status: str,
    pdf_path: str | None,
    pdf_bytes: int | None,
) -> None:
    status = status.strip().lower()
    if status not in DOWNLOAD_STATUSES:
        raise ValueError(f"Unsupported download status: {status}")
    row = _resolve_paper(connection, code)
    now = _utc_now()
    markdown_status = row["markdown_status"]
    validated_at = row["pdf_validated_at"]
    if status == "downloaded":
        if not pdf_path:
            raise ValueError("Downloaded papers require --pdf-path")
        if markdown_status not in {"processing", "done"}:
            markdown_status = "pending"
        validated_at = now
    with connection:
        connection.execute(
            """
            UPDATE papers
            SET download_status = ?, pdf_path = COALESCE(?, pdf_path),
                pdf_bytes = COALESCE(?, pdf_bytes), pdf_validated_at = ?,
                markdown_status = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, pdf_path, pdf_bytes, validated_at, markdown_status, now, row["id"]),
        )


def record_markdown(
    connection: sqlite3.Connection, code: str, status: str, markdown_path: str | None
) -> None:
    status = status.strip().lower()
    if status not in MARKDOWN_STATUSES:
        raise ValueError(f"Unsupported Markdown status: {status}")
    row = _resolve_paper(connection, code)
    if row["download_status"] != "downloaded" and status != "not_ready":
        raise ValueError("A paper must be downloaded before Markdown processing")
    with connection:
        connection.execute(
            """
            UPDATE papers SET markdown_status = ?, markdown_path = COALESCE(?, markdown_path),
                updated_at = ? WHERE id = ?
            """,
            (status, markdown_path, _utc_now(), row["id"]),
        )


def _render_all(connection: sqlite3.Connection, settings: WorkflowSettings) -> None:
    review_source_run = settings.review_source_run or active_source_run(connection)
    review = render_review(
        connection,
        settings.review_file,
        include_excluded=settings.review_include_excluded,
        source_run=review_source_run,
    )
    status = render_status(connection, settings.status_file)
    print(f"Review: {review.resolve()}")
    print(f"Status: {status.resolve()}")


def _review_archive_path(settings: WorkflowSettings, source_run: str | Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_name = Path(source_run).parent.name or Path(source_run).stem or "screening"
    run_name = re.sub(r"[^\w.-]+", "-", run_name, flags=re.UNICODE).strip("-._")
    run_name = (run_name or "screening")[:80]
    settings.review_archive_dir.mkdir(parents=True, exist_ok=True)
    base_name = f"{timestamp}-{run_name}-review"
    candidate = settings.review_archive_dir / f"{base_name}.md"
    suffix = 2
    while candidate.exists():
        candidate = settings.review_archive_dir / f"{base_name}-{suffix:02d}.md"
        suffix += 1
    return candidate


def finalize_screening(
    connection: sqlite3.Connection, settings: WorkflowSettings
) -> Path:
    """Archive the completed active run and refresh the mutable current reports."""
    source_run = settings.review_source_run or active_source_run(connection)
    if source_run is None:
        raise ValueError("No active screening run is available to finalize")
    resolved_source_run = str(Path(source_run).resolve())
    row = connection.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN screening_status = 'pending' THEN 1 ELSE 0 END) AS pending
        FROM papers WHERE source_run = ?
        """,
        (resolved_source_run,),
    ).fetchone()
    total = int(row["total"] or 0)
    pending = int(row["pending"] or 0)
    if total == 0:
        raise ValueError("The active screening run contains no papers")
    if pending:
        raise ValueError(
            f"Cannot finalize screening: {pending} paper(s) are still pending"
        )

    archive = _review_archive_path(settings, resolved_source_run)
    render_review(
        connection,
        archive,
        include_excluded=settings.review_include_excluded,
        source_run=resolved_source_run,
    )
    settings.review_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(archive, settings.review_file)
    render_status(connection, settings.status_file)
    return archive


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage resumable AutoPaper workflow state.")
    parser.add_argument("--config", default="config/config.local.toml")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init")
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--candidates", required=True)
    import_parser.add_argument(
        "--no-reuse",
        action="store_true",
        help="Send all imported candidates for fresh Sub Agent screening",
    )
    pending_parser = subparsers.add_parser("pending")
    pending_parser.add_argument("--limit", type=int)
    screening_parser = subparsers.add_parser("record-screening")
    screening_parser.add_argument("--paper-id", required=True)
    screening_parser.add_argument("--decision", required=True)
    screening_parser.add_argument("--reason", required=True)
    screening_parser.add_argument("--model")
    subparsers.add_parser("screening-profile")
    subparsers.add_parser("finalize-screening")
    human_parser = subparsers.add_parser("decide")
    group = human_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", nargs="+")
    group.add_argument("--reject", nargs="+")
    subparsers.add_parser("approve-included")
    subparsers.add_parser("render")
    subparsers.add_parser("export-download-queue")
    download_parser = subparsers.add_parser("record-download")
    download_parser.add_argument("--paper-id", required=True)
    download_parser.add_argument("--status", required=True)
    download_parser.add_argument("--pdf-path")
    download_parser.add_argument("--pdf-bytes", type=int)
    markdown_parser = subparsers.add_parser("record-markdown")
    markdown_parser.add_argument("--paper-id", required=True)
    markdown_parser.add_argument("--status", required=True)
    markdown_parser.add_argument("--markdown-path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_workflow_settings(args.config)
        connection = connect(settings.database)
        try:
            profile = None
            if args.command in {
                "init",
                "import",
                "pending",
                "record-screening",
                "screening-profile",
                "finalize-screening",
            }:
                profile = ensure_screening_profile(connection, settings)
            if args.command == "init":
                _render_all(connection, settings)
            elif args.command == "import":
                summary = import_candidates(
                    connection,
                    args.candidates,
                    reuse_existing=not args.no_reuse,
                )
                print(f"Candidates in current search: {summary.total}")
                print(f"New database records: {summary.inserted}")
                print(f"Existing database records: {summary.matched_existing}")
                print(f"Reused screening results: {summary.reused_screening}")
                print(f"Require Sub Agents: {summary.pending_screening}")
                if summary.reset_for_rescreening:
                    print(
                        "Reset for fresh screening: "
                        f"{summary.reset_for_rescreening}"
                    )
                _render_all(connection, settings)
            elif args.command == "pending":
                tasks = pending_screening_tasks(connection, settings, args.limit)
                for task in tasks:
                    print(json.dumps(task, ensure_ascii=False))
            elif args.command == "record-screening":
                record_screening(
                    connection,
                    args.paper_id,
                    args.decision,
                    args.reason,
                    args.model or settings.model,
                    profile.id,
                )
                _render_all(connection, settings)
            elif args.command == "screening-profile":
                screened = connection.execute(
                    """
                    SELECT COUNT(DISTINCT paper_id) FROM screening_results
                    WHERE profile_id = ?
                    """,
                    (profile.id,),
                ).fetchone()[0]
                print(
                    json.dumps(
                        {
                            "database": str(settings.database.resolve()),
                            "profile_id": profile.id,
                            "profile_key": profile.profile_key[:12],
                            "created_at": profile.created_at,
                            "screened_papers": screened,
                        },
                        ensure_ascii=False,
                    )
                )
            elif args.command == "finalize-screening":
                archive = finalize_screening(connection, settings)
                print(f"Archived review: {archive.resolve()}")
                print(f"Current review: {settings.review_file.resolve()}")
                print(f"Status: {settings.status_file.resolve()}")
            elif args.command == "decide":
                codes = args.approve or args.reject
                decision = "approved" if args.approve else "rejected"
                print(f"Updated: {set_human_decision(connection, codes, decision)}")
                _render_all(connection, settings)
            elif args.command == "approve-included":
                print(f"Approved: {approve_all_included(connection)}")
                _render_all(connection, settings)
            elif args.command == "render":
                _render_all(connection, settings)
            elif args.command == "export-download-queue":
                queue, count = export_download_queue(connection, settings.download_queue_file)
                print(f"Queued: {count}; file: {queue.resolve()}")
                _render_all(connection, settings)
            elif args.command == "record-download":
                record_download(
                    connection,
                    args.paper_id,
                    args.status,
                    args.pdf_path,
                    args.pdf_bytes,
                )
                _render_all(connection, settings)
            elif args.command == "record-markdown":
                record_markdown(
                    connection,
                    args.paper_id,
                    args.status,
                    args.markdown_path,
                )
                _render_all(connection, settings)
        finally:
            connection.close()
        return 0
    except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
