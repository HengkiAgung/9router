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

## Kantor (multi-agent office)
A team of AI workers that hire, message, wait for and dismiss each other, on top of 9router. Live at https://myoffice.hengkiagung.my.id (password: `KANTOR_PASSWORD` in `.env`).
- Hire a worker from the web UI with a task. A big task can be split: the worker hires others (`hire_worker`), each in its own git worktree and branch `kantor/<name>`, waits for their reports (`wait_for_workers`), merges their branches and reports. Also `tell_worker`, `list_workers`, `send_home`.
- Models by difficulty (judged automatically, or set by the hiring worker): `KANTOR_MODEL_EASY/NORMAL/HARD`. When a model fails: `KANTOR_FALLBACK_MODELS`, then `KANTOR_CHEAP_MODELS` with a warning in the UI. `KANTOR_PAID_MODELS` are never automatic: an agent must justify one and you approve it; paid tokens are counted in the header.
- Workers are threads (about 2 MB each), not agent CLIs; model calls and commands are gated by the `KANTOR_*` limits in `.env` (sized for this 2 GB server).
- File edits apply directly; shell commands wait for approval in the UI, except read-only ones (`ls`, `cat`, `grep`, `git status/diff/log`). The **Auto** switch in the navbar runs every worker's commands without asking, except risky ones (`git push`, `ssh`, `sudo`, `rm -rf` of a whole tree, anything touching Kantor's own state) and paid models (`KANTOR_AUTO_MODE_PAID`). Its state survives restarts.
- Project memory: every worker gets the project's `KANTOR.md` notes (project folder, and each repo of a multi-repo project) and a summary of its graphify code graph, and may run `graphify query/explain/affected` without approval. A new project gets KANTOR.md templates, a code graph and a one-off `Pencatat` worker that fills the notes in. After a main-checkout worker finishes a task, the graph is rebuilt for the code that changed (AST only, no LLM; areas named by `KANTOR_GRAPH_LABEL_MODEL`). Repos over `KANTOR_GRAPH_SPLIT_FILES` code files get one graph per folder, listed in `graphify-out/KANTOR_INDEX.md`.
- Multi-repo projects (a folder of git repos, created from several URLs in **+ Project**): a hired worker gets a worktree of each repo it needs on branch `kantor/<name>`.
- Code: `tools/kantor/` (`engine.py`, `knowledge.py`, `server.py`, `ui.html`). Install or update, after changing code or `.env`: `bash tools/kantor/deploy.sh`.
- Runs as the `kantor` user (no sudo, other homes hidden) under systemd with a RAM cap; projects live in `/home/kantor/projects` (add them in the UI by git URL; for private repos add the SSH key `deploy.sh` prints to GitHub). Logs: `sudo journalctl -u kantor -f`.
