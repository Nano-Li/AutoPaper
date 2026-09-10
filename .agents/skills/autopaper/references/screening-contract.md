# Screening contract

Use one Sub Agent for exactly one paper. A **wave** contains at most three concurrently running Sub Agents; the final wave may contain fewer. A **screening run** covers all papers currently pending from the imported search result, whether that is a few papers or thousands. Do not impose an arbitrary paper-count limit. The main Agent remains responsible for orchestration and database writes.

## Fast screening loop

At the start, define the screening run as the full pending set from the imported search result. Use SQLite screening status as the persistent cursor: query only the next wave of at most three papers, and never keep a complete remaining-ID list or load the full candidate file in the main Agent's context. Process consecutive waves until the database reports no pending papers; do not rerun the search or impose a total-count cap. Between waves, use a mechanical fast path:

1. Wait until every active Sub Agent in the current wave has returned. If only part of the wave has returned, do not analyze, summarize, narrate progress, inspect reports, or query the database again; immediately continue waiting for the remaining results.
2. Validate only the returned `paper_id`, allowed `decision`, one-to-two-sentence Chinese `reason`, and expected model metadata. Do not have the main Agent reconsider the paper's scientific relevance or rewrite a valid reason.
3. Record the completed wave with the fewest practical workflow calls. Once recorded, do not restate or carry its titles, abstracts, prompts, or decisions into later wave handling; rely on SQLite if that information is needed again. Do not explicitly render, open, summarize, or inspect `review.md` or `STATUS.md` between waves; any report files produced as command side effects require no additional Agent work.
4. Immediately dispatch the next wave. Do not emit routine commentary between successful waves.
5. After every paper in the screening run has a recorded result, call `python -m autopaper.workflow finalize-screening` once. Check only compact aggregate counts and successful creation of the timestamped archive, then present that archived review file for the user to open; do not load the full review into the main Agent's context.

Preserve completed decisions as checkpoints so an interrupted, compacted, or long-running task can resume by querying SQLite without repeating screened papers. Investigate or retry only malformed, missing, or failed results rather than slowing the normal path for valid results.

## Deterministic review and statistics

The main Agent must never assemble or append a review from its conversation history. After screening, `finalize-screening` queries SQLite, refreshes the mutable current review, and writes a separate timestamped archive programmatically. Titles and abstracts come from imported records; reasons come from the corresponding stored Sub Agent result; only `include` and `uncertain` papers appear in the detailed review. Generate all screening, human-review, download, and Markdown progress counts with database queries or the status renderer rather than model counting.

Give the Sub Agent only:

- the stable `paper_id`;
- the title copied from the search result;
- the abstract copied from the search result;
- `criteria` and `decision_prompt` from `config/config.local.toml`.

Use the model and reasoning effort from `[screening]` in `config/config.local.toml`; the current baseline is `gpt-5.4-mini` with medium reasoning. The task is read-only and requires no tools.

Require exactly this logical result:

```json
{
  "paper_id": "P0001",
  "decision": "include",
  "reason": "一到两句中文理由，只依据所提供的标题和摘要。"
}
```

Allowed decisions are `include`, `exclude`, and `uncertain`. The reason must be one or two concise Chinese sentences. Do not request or return `matched_criteria`, a rewritten abstract, download URLs, or invented details. Use `uncertain` when the available abstract is missing or insufficient for a responsible decision.

The main Agent validates the returned ID and allowed decision, records the model name, and writes the result with `python -m autopaper.workflow record-screening`. A Sub Agent must never write the SQLite database directly.
