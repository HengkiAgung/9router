# WAT Project

Workflows, Agents, Tools. Probabilistic AI reasons; deterministic code executes.

## Layout
```
.tmp/           # Disposable intermediates (gitignored)
tools/          # Python execution scripts
workflows/      # Markdown SOPs
knowledge/      # Persistent knowledge: sources/ (raw) + notes/ (learnings)
graphify-out/   # Knowledge graph built by graphify (commit per project)
.env            # Secrets (gitignored) — copy from .env.example
```

## Setup
```
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env         # then fill values

# Knowledge graph (once per machine / per project)
uv tool install graphifyy      # once per machine
graphify hook install          # per clone: auto-rebuild code graph on commit
# then in Claude Code: /graphify .   (first build)
```

## How it works
- **Workflows** (`workflows/*.md`) define objective, inputs, tools, outputs, edge cases.
- **Agent** reads workflow, sequences tools, handles failures.
- **Tools** (`tools/*.py`) do the deterministic work. Import helpers from `tools/_shared.py`.
- **Knowledge** (`knowledge/`) holds sources + learnings, indexed into `graphify-out/`. SOP: `workflows/knowledge_base.md`. CLI: `tools/knowledge.py`.

New workflow: copy `workflows/_template.md`. New tool: import `_shared`, read secrets via `require_env`.

## 9router (Docker)
Runs the upstream `decolua/9router` image. Data persists in `./data` (SQLite).
```
copy .env.example .env         # set JWT_SECRET, INITIAL_PASSWORD, ...
docker compose up -d
docker compose logs -f
```
Dashboard: http://localhost:20128 (login with `INITIAL_PASSWORD`). API: http://localhost:20128/v1.

VPS: clone, create `.env`, `docker compose up -d`. Put a reverse proxy (Caddy/nginx) with HTTPS in front, keep `NINEROUTER_BIND=127.0.0.1`, set `BASE_URL` to the public URL, `AUTH_COOKIE_SECURE=true`, `REQUIRE_API_KEY=true`. Update: `docker compose pull && docker compose up -d`.

## Orkestra (multi-model coding agent)
`tools/orchestrator.py` sends a request to a router model, which splits it into planner / coder / reviewer / writer steps and picks a 9router model for each. The agents read, search and edit files and run commands in the workspace; edits and commands ask for confirmation. Stdlib only.
```
cd /path/to/your/project
python /path/to/tools/orchestrator.py "fix the failing test"   # one request
python /path/to/tools/orchestrator.py                          # interactive loop
```
Flags: `--auto-edit` (apply edits without asking), `--yes` (also run commands without asking), `--root DIR`.
Config: `NINEROUTER_API_KEY` (and optional `ORCH_BASE_URL`, `ORCH_MODELS`) from the environment, `~/.config/orkestra/.env`, or this repo's `.env`. On another machine, copy the single file and create `~/.config/orkestra/.env`.
