"""Standalone, main-PDF-only Edge downloads with per-paper SQLite checkpoints."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .download import load_queue, validate_pdf
from .ref_downloader_adapter import publisher_for
from .workflow import connect, load_workflow_settings, record_download, render_status

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / '_references/ref-downloader/skills/ref-downloader/scripts/download_refs.py'


def output_path(folder, paper):
    code = paper['paper_id']
    if not re.fullmatch(r'P\d+', code):
        raise ValueError('Invalid paper ID')
    doi = re.sub(r'[^A-Za-z0-9._-]', '_', paper['doi'])
    return Path(folder) / f'{code}_{doi}.pdf'


def validate_queue(connection, papers):
    seen = set()
    for paper in papers:
        code = paper['paper_id']
        if not re.fullmatch(r'P\d+', code) or code in seen:
            raise ValueError(f'Invalid or duplicate ID: {code}')
        seen.add(code)
        row = connection.execute('SELECT * FROM papers WHERE id=?', (int(code[1:]),)).fetchone()
        if (not row or row['human_status'] != 'approved'
                or row['screening_status'] not in ('include', 'uncertain')
                or row['doi'].lower() != paper['doi'].lower()
                or row['title'] != paper['title']):
            raise ValueError(f'{code}: queue no longer matches an approved database record')


def load_engine():
    os.environ['REF_DOWNLOADER_CONFIG'] = str(ROOT / 'config/ref_downloader_edge.toml')
    os.environ['REF_DOWNLOADER_BROWSER'] = 'edge'
    sys.path.insert(0, str(ENGINE.parent))
    spec = importlib.util.spec_from_file_location('autopaper_ref_engine', ENGINE)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    cfg = engine.load_config()
    engine.init_institution_config(cfg.institution)
    engine.init_user_config(cfg.user)
    return engine


async def fetch_main_pdf(engine, ctx, paper, dest):
    """Use only the upstream main-PDF primitives; never enter its PDF+SI loop."""
    page = await ctx.new_page()
    doi, publisher = paper['doi'], publisher_for(paper)
    try:
        if publisher == 'elsevier':
            return await engine.try_elsevier_pdf(page, ctx, doi, dest)
        if publisher == 'wiley':
            return await engine.try_click_pdf(page, ctx, doi, publisher, dest, stage='main_pdf')
        url = engine.direct_pdf_url(doi, publisher)
        if url:
            attempt = await engine.try_direct_pdf(page, ctx, url, dest,
                                                  stage='main_pdf', allow_navigation=True)
            if attempt['state'] != engine.STATUS_FAILED:
                return attempt
        return await engine.try_click_pdf(page, ctx, doi, publisher, dest, stage='main_pdf')
    finally:
        # Each paper owns its pages, including popup viewers.
        for tab in list(ctx.pages):
            try:
                await tab.close()
            except Exception:
                pass
        engine.RUN_CTX['manual_pages'] = []


def checkpoint(connection, settings, paper, dest, attempt, engine, minimum, log):
    valid, reason = validate_pdf(dest, minimum) if dest.is_file() else (False, 'file_missing')
    if valid:
        valid, reason = engine.looks_like_valid_pdf(dest, 'main_pdf')
    if valid:
        status = 'downloaded'
        record_download(connection, paper['paper_id'], status, str(dest.resolve()), dest.stat().st_size)
    else:
        status = 'manual_pending' if attempt.get('state') == engine.STATUS_MANUAL else 'failed'
        record_download(connection, paper['paper_id'], status, None, None)
    result = {'time': datetime.now(timezone.utc).isoformat(), 'paper_id': paper['paper_id'],
              'doi': paper['doi'], 'status': status, 'pdf_path': str(dest.resolve()) if valid else None,
              'validation': reason, 'reason': attempt.get('reason', ''), 'supplementary': False}
    with Path(log).open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(result, ensure_ascii=False) + '\n')
    render_status(connection, settings.status_file)
    print(f"{paper['paper_id']}: {status} ({reason})", flush=True)
    return status


async def download_all(connection, settings, papers, folder, log, engine, timeout):
    from playwright.async_api import async_playwright
    folder.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    engine.RUN_CTX['events_path'] = log.with_name('edge-events.jsonl')
    engine.RUN_CTX['manual_pages'] = []
    counts = {}
    ctx = None
    async with async_playwright() as pw:
        try:
            for paper in papers:
                dest = output_path(folder, paper)
                attempt = {'state': engine.STATUS_FAILED, 'reason': 'not_attempted'}
                engine.RUN_CTX['current_ref'] = dict(paper, id=int(paper['paper_id'][1:]),
                                                    label=paper['paper_id'], publisher=publisher_for(paper))
                try:
                    valid = dest.is_file() and validate_pdf(dest, 10000)[0] and engine.looks_like_valid_pdf(dest, 'main_pdf')[0]
                    if valid:
                        attempt = {'state': engine.STATUS_DOWNLOADED, 'reason': 'existing_valid_pdf'}
                    else:
                        if dest.exists():
                            # Preserve invalid prior output; it must not be treated as a successful resume.
                            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                            dest.rename(dest.with_suffix(f'.{stamp}.invalid'))
                        record_download(connection, paper['paper_id'], 'downloading', None, None)
                        render_status(connection, settings.status_file)
                        if ctx is None:
                            ctx = await pw.chromium.launch_persistent_context(
                                engine.get_edge_user_data_dir(), channel='msedge', headless=False,
                                accept_downloads=True, args=['--profile-directory=Default', '--no-proxy-server'],
                                viewport={'width': 1280, 'height': 900})
                        attempt = await asyncio.wait_for(fetch_main_pdf(engine, ctx, paper, dest), timeout)
                except engine.ManualInterventionRequired as exc:
                    attempt = {'state': engine.STATUS_MANUAL, 'reason': exc.reason}
                except Exception as exc:
                    attempt = {'state': engine.STATUS_FAILED, 'reason': f'{type(exc).__name__}: {exc}'}
                    if ctx is not None:
                        await engine.close_context_quietly(ctx)
                        ctx = None
                finally:
                    status = checkpoint(connection, settings, paper, dest, attempt, engine, 10000, log)
                    counts[status] = counts.get(status, 0) + 1
                await asyncio.sleep(3)
        finally:
            await engine.close_context_quietly(ctx)
            render_status(connection, settings.status_file)
    print(json.dumps(counts, ensure_ascii=False), flush=True)
    return 0 if counts.get('downloaded', 0) == len(papers) else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--queue', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--log', required=True)
    parser.add_argument('--paper-timeout', type=int, default=600)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    settings = load_workflow_settings(args.config)
    if settings.supplementary:
        raise ValueError('This production entry supports supplementary=false only')
    papers = load_queue(args.queue)
    if not papers:
        raise ValueError('Empty queue')
    connection = connect(settings.database)
    try:
        validate_queue(connection, papers)
        engine = load_engine()
        profile = Path(engine.get_edge_user_data_dir()).resolve()
        if profile != (ROOT / 'browser_profiles/edge-autopaper').resolve() or not (profile / 'Default').is_dir():
            raise ValueError('Dedicated AutoPaper Edge profile is missing or misconfigured')
        folder = Path(args.output_dir).resolve()
        print(f'{len(papers)} approved papers; all PDFs: {folder}', flush=True)
        if args.dry_run:
            print('Preflight OK. No browser opened; no downloads started.')
            return 0
        print('请退出 Clash（包括 TUN 模式），关闭 AutoPaper 专用 Edge 窗口。')
        print('断开代理后，本脚本仍会独立运行；每篇完成后会自动更新 STATUS。')
        answer = input('确认已经完成上述操作，并允许专用 Edge 开始下载？输入 YES 后回车：')
        if answer.strip().upper() != 'YES':
            print('未开始下载。')
            return 0
        for key in list(os.environ):
            if key.lower() in ('http_proxy', 'https_proxy', 'all_proxy'):
                os.environ.pop(key, None)
        return asyncio.run(download_all(connection, settings, papers, folder, Path(args.log).resolve(),
                                        engine, args.paper_timeout))
    finally:
        connection.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
