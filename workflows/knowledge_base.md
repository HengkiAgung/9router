# Workflow: knowledge_base

## Objective
Keep persistent, queryable knowledge (sources + learnings) in `knowledge/`, indexed as a graphify knowledge graph so any session can recall it.

## Inputs
- `url` — web page / article / paper to ingest (optional)
- `file` — local doc/PDF/image to ingest (optional) → copy into `knowledge/sources/`
- `learning` — fact, decision, API quirk, or fix discovered during work (optional)
- `question` — something to look up (optional)

## Tools
- `tools/knowledge.py note --title "..." --body "..." --tags a,b [--source URL]` — write learning to `knowledge/notes/<slug>.md`. Same title → appends dated section, never overwrites.
- `tools/knowledge.py add-url <url> [--author NAME]` — fetch URL into `knowledge/sources/` (wraps `graphify add`).
- `tools/knowledge.py query "<question>" [--budget N] [--dfs]` — BFS/DFS over graph.
- `tools/knowledge.py status` — counts + files not yet in graph.
- `/graphify . --update` (Claude Code skill) — re-extract changed docs/notes into graph (semantic, uses subagents).
- `graphify update .` — code-only rebuild, no LLM. Post-commit hook runs this automatically.

## Steps

### Recall (before starting any task)
1. If `graphify-out/graph.json` exists, run `knowledge.py query "<task topic>"`.
2. Use hits (cite `source_location`). Read only referenced files, not whole folders.
3. Broad orientation: read `graphify-out/GRAPH_REPORT.md` (god nodes, communities).

### Capture (during/after task)
1. New durable learning (rate limit, quirk, decision, how-to) → `knowledge.py note`. One topic per note; reuse title to extend.
2. External reference worth keeping → `knowledge.py add-url` or copy file into `knowledge/sources/`.
3. Workflow-specific edge cases still go in that workflow's "Edge Cases & Learnings". Note links back to workflow by name.

### Rebuild
1. `knowledge.py status` → lists pending files.
2. Pending docs/notes → run `/graphify . --update`.
3. Only code changed → `graphify update .` (or let post-commit hook do it).
4. Commit `knowledge/` + `graphify-out/` (graph.json, GRAPH_REPORT.md, manifest) so knowledge persists across machines.

## Outputs
- `knowledge/notes/*.md`, `knowledge/sources/*` — source of truth (committed)
- `graphify-out/graph.json`, `GRAPH_REPORT.md`, `graph.html` — derived index (committed, regenerable)
- `graphify-out/cache/` — extraction cache (gitignored)

## Edge Cases & Learnings
- graphify installed as uv tool (`uv tool install graphifyy`), not in project `.venv`. Python path saved in `graphify-out/.graphify_python` (machine-specific, gitignored).
- No API key needed. Semantic extraction uses Claude Code subagents unless `GEMINI_API_KEY` set.
- Post-commit hook only re-extracts code. Notes/sources always need `/graphify . --update`.
- graph.json shrink-guard: if files deleted, rebuild refuses to shrink → `graphify update . --force`.
- Secrets never go in notes. `.env` content is never knowledge.
