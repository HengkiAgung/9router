"""Knowledge store CLI. Deterministic layer over knowledge/ + graphify.

Layout:
    knowledge/sources/   raw inputs (docs, PDFs, fetched URLs)
    knowledge/notes/     agent-written learnings, one topic per file
    graphify-out/        knowledge graph built from the whole repo

Commands:
    python tools/knowledge.py note --title "..." --body "..." [--tags a,b] [--source URL]
    python tools/knowledge.py add-url <url> [--author NAME]
    python tools/knowledge.py query "<question>" [--budget N] [--dfs]
    python tools/knowledge.py status

Doc/note changes need semantic re-extraction: run `/graphify . --update`
in Claude Code after adding knowledge (see workflows/knowledge_base.md).
Code-only changes: `graphify update .` (no LLM).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _shared import ROOT  # noqa: E402

KNOWLEDGE = ROOT / "knowledge"
SOURCES = KNOWLEDGE / "sources"
NOTES = KNOWLEDGE / "notes"
GRAPH_OUT = ROOT / "graphify-out"
GRAPH_JSON = GRAPH_OUT / "graph.json"
MANIFEST = GRAPH_OUT / "manifest.json"
NEEDS_UPDATE = GRAPH_OUT / ".needs_update"


def _graphify() -> str:
    exe = shutil.which("graphify")
    if not exe:
        raise SystemExit("graphify not found on PATH. Install: uv tool install graphifyy")
    return exe


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "note"


def _mark_dirty() -> None:
    GRAPH_OUT.mkdir(exist_ok=True)
    NEEDS_UPDATE.write_text("1", encoding="utf-8")


def cmd_note(args: argparse.Namespace) -> None:
    NOTES.mkdir(parents=True, exist_ok=True)
    path = NOTES / f"{_slug(args.title)}.md"
    body = Path(args.file).read_text(encoding="utf-8") if args.file else args.body
    if not body:
        raise SystemExit("Provide --body or --file")
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    today = date.today().isoformat()

    if path.exists():
        # Append dated section instead of overwriting existing knowledge.
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n\n## Update {today}\n\n{body.strip()}\n")
        print(f"Appended: {path.relative_to(ROOT)}")
    else:
        fm = ["---", f"title: {args.title}", f"created: {today}"]
        if tags:
            fm.append(f"tags: [{', '.join(tags)}]")
        if args.source:
            fm.append(f"source: {args.source}")
        fm.append("---")
        path.write_text("\n".join(fm) + f"\n\n# {args.title}\n\n{body.strip()}\n", encoding="utf-8")
        print(f"Created: {path.relative_to(ROOT)}")
    _mark_dirty()


def cmd_add_url(args: argparse.Namespace) -> None:
    SOURCES.mkdir(parents=True, exist_ok=True)
    cmd = [_graphify(), "add", args.url, "--dir", str(SOURCES)]
    if args.author:
        cmd += ["--author", args.author]
    sys.exit(subprocess.call(cmd, cwd=ROOT))


def cmd_query(args: argparse.Namespace) -> None:
    if not GRAPH_JSON.exists():
        raise SystemExit("No graph yet. Build it: /graphify . in Claude Code")
    cmd = [_graphify(), "query", args.question, "--budget", str(args.budget)]
    if args.dfs:
        cmd.append("--dfs")
    sys.exit(subprocess.call(cmd, cwd=ROOT))


def _knowledge_files() -> list[Path]:
    return [p for d in (SOURCES, NOTES) if d.exists() for p in d.rglob("*")
            if p.is_file() and p.name != ".gitkeep"]


def cmd_status(_: argparse.Namespace) -> None:
    files = _knowledge_files()
    print(f"Sources: {sum(1 for f in files if SOURCES in f.parents)}  "
          f"Notes: {sum(1 for f in files if NOTES in f.parents)}")
    if not GRAPH_JSON.exists():
        print("Graph: not built. Run /graphify . in Claude Code")
        return
    g = json.loads(GRAPH_JSON.read_text(encoding="utf-8"))
    print(f"Graph: {len(g.get('nodes', []))} nodes, "
          f"{len(g.get('links', g.get('edges', [])))} edges")

    # Files newer than the graph = not yet extracted.
    built = GRAPH_JSON.stat().st_mtime
    stale = [f for f in files if f.stat().st_mtime > built]
    if stale or NEEDS_UPDATE.exists():
        print("Pending re-extraction (run /graphify . --update):")
        for f in stale:
            print(f"  - {f.relative_to(ROOT)}")
    else:
        print("Graph up to date.")


def main() -> None:
    p = argparse.ArgumentParser(description="WAT knowledge store")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("note", help="write/append a learning note")
    n.add_argument("--title", required=True)
    n.add_argument("--body")
    n.add_argument("--file", help="read body from file")
    n.add_argument("--tags", help="comma-separated")
    n.add_argument("--source", help="URL or origin of the knowledge")
    n.set_defaults(func=cmd_note)

    a = sub.add_parser("add-url", help="fetch URL into knowledge/sources")
    a.add_argument("url")
    a.add_argument("--author")
    a.set_defaults(func=cmd_add_url)

    q = sub.add_parser("query", help="query the knowledge graph")
    q.add_argument("question")
    q.add_argument("--budget", type=int, default=2000)
    q.add_argument("--dfs", action="store_true")
    q.set_defaults(func=cmd_query)

    s = sub.add_parser("status", help="show store + graph freshness")
    s.set_defaults(func=cmd_status)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
