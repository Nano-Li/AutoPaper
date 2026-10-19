# Screening contract

## Batch size and scope

Use `[screening].papers_per_agent` from `config/config.local.toml`: currently 3 papers per Sub Agent. The final batch may contain one or two papers. A wave contains at most `[screening].max_parallel_agents` concurrently running Sub Agents, currently 3, so a full wave contains 9 papers. Each paper receives its own independent decision, reason, and database record.

Use the configured model and reasoning effort, currently `gpt-5.6-luna` / `low`. The task is read-only and requires no tools. The main Agent handles orchestration and database writes. Do not repeat previously completed papers or mix decisions across screening profiles.

## Fast screening loop

1. Use `python -m autopaper.workflow --config <run-config> pending-batches` to retrieve only the next wave. Each JSON line is one agent payload; `--limit 1` returns one agent's batch. Import and pending-task generation programmatically convert papers without a usable abstract to `uncertain`, so no returned agent payload may contain a missing or blank abstract. Do not load a complete pending-ID list or candidate file into the main Agent.
2. Send each batch to one fresh Sub Agent. Supply the batch's shared criteria and decision prompt once, plus each paper's stable ID, original title, and original abstract. The reusable role contains no research-specific criteria; always pass those supplied by the workflow. The agent must judge every paper independently without using another paper's evidence.
3. Wait for the active wave to return. Validate the exact set of supplied IDs, one result per ID, allowed decisions, expected model, and one-to-two-sentence Chinese reasons. Do not reconsider relevance or rewrite valid reasons in the main Agent.
4. Record valid results through `autopaper.workflow.record_screening` or the `record-screening` CLI. Store the model actually used. If a batch has a missing or malformed result, preserve its valid results and retry only unresolved papers when allowed. If the user asks to skip problems, log failed IDs and leave them pending; do not invent screening decisions to mark them complete. A shared account limit is not a paper-level failure.
5. Immediately continue with the next bounded wave unless the user asks to pause. Use SQLite as the persistent cursor, not conversation history. Do not inspect or rewrite reports between waves.
6. When every paper has a recorded result, run `finalize-screening` once. Inspect only compact counts and archive creation, then present the new archive. Do not start downloads automatically.

## Result format

Return only a JSON array, in input order, with exactly one object per supplied paper:

```json
[
  {"paper_id": "P0001", "decision": "include", "reason": "一到两句中文理由，只依据该论文的标题和摘要。"},
  {"paper_id": "P0002", "decision": "exclude", "reason": "一到两句中文理由。"},
  {"paper_id": "P0003", "decision": "uncertain", "reason": "摘要存在，但关键信息仍不足，无法可靠判断。"}
]
```

Allowed decisions are `include`, `exclude`, and `uncertain`. Apply the configured criteria and decision prompt independently to each paper. A Sub Agent uses `uncertain` only for genuine ambiguity in an available abstract; missing abstracts are handled before delegation. Do not return rewritten abstracts, matched criteria, download URLs, or invented facts. Do not change the scientific criteria or stored profile merely to change batching.

## Reports and user-requested pauses

All review content and statistics must be generated from SQLite by the workflow program. Titles and abstracts come from imported records; reasons come from saved Sub Agent results. Every review must expand all completed include, uncertain, and exclude results, including original titles, original abstracts (or a missing-abstract marker), and stored reasons. Keep [workflow].review_include_excluded = true in project and run-specific configurations. Pass settings.review_include_excluded to render_review; do not hard-code False. Pending papers remain pending and appear in summary counts.

If the user asks to stop after active tasks and receive an interim review, finish only those active batches and record their successful results. Use `render_review` with `include_excluded=settings.review_include_excluded` and a new timestamped `partial-review.md` path, scoped to the active run, and refresh status programmatically. Preserve existing review documents. Leave unscreened or failed papers pending, clearly report the remaining count, and do not invoke `finalize-screening` or start another batch.
