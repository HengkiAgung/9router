"""Project memory for Kantor: KANTOR.md notes and graphify code graphs.

Notes: KANTOR.md in the project folder (the whole system) and in each repo of a multi-repo project
(that repo's details, committed with it). Workers get them in their prompt, and workers in the main
checkout keep them up to date.

Graphs: graphify's code-only extraction (AST, no LLM), clustered and labelled with a cheap model
through 9router. Measured on this 2 GB server: a repo of a few hundred code files takes ~70 MB and
a few seconds, but one of 12,000 (kai-erp) runs out of memory in one piece, so a repo over
KANTOR_GRAPH_SPLIT_FILES code files is built per folder ("parts"), each with its own graph and
report, listed in graphify-out/KANTOR_INDEX.md. Assets, third-party code, templates, seed data and
files over 1 MB are ignored (a 28 MB seeder alone exhausted the memory).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

GRAPHIFY = Path.home() / ".local" / "bin" / "graphify"
CODE_EXT = {".py", ".php", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".go", ".rs", ".java", ".kt",
            ".dart", ".swift", ".rb", ".cs", ".c", ".h", ".cpp", ".hpp", ".scala", ".lua", ".sh"}
DEFAULT_IGNORE = ["public/", "**/vendor/", "**/node_modules/", "**/plugins/", "storage/", "bootstrap/cache/",
                  "build/", "dist/", ".next/", "resources/views/", "database/seeders/", "**/migrations/*.sql",
                  "*.min.js", "*.min.css", "*.map"]
MAX_CODE_FILE = 1_000_000
MIN_CODE_FILES = 20  # below this a graph adds nothing (graphify says so too)
NOTES_FILE = "KANTOR.md"
NOTES_TEMPLATE = """# {name}: catatan project untuk Kantor

<!-- Dibaca setiap worker di project ini. Worker di checkout utama memperbaruinya setiap kali
     mempelajari hal yang awet. Ganti bagian yang masih kosong. -->

## Ringkasan
(belum diisi)

## Arsitektur
(belum diisi)

## Cara menjalankan dan test
(belum diisi)

## Konvensi
(belum diisi)

## Jebakan dan pelajaran
(belum diisi)
"""
TEMPLATE_MARK = "(belum diisi)"


def git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)


def repos(project_dir: Path) -> list[Path]:
    """The project's git repos: the folder itself, and git repos directly inside it."""
    found = [project_dir] if (project_dir / ".git").exists() else []
    for d in sorted(project_dir.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and (d / ".git").exists():
            found.append(d)
    return found


def nested(project_dir: Path) -> list[str]:
    """Names of the repos inside a multi-repo project."""
    return [r.name for r in repos(project_dir) if r != project_dir]


# --- notes ----------------------------------------------------------------------------------------

def ensure_notes(project_dir: Path) -> list[Path]:
    """KANTOR.md for the project and each nested repo, from the template when missing."""
    made = []
    for folder in [project_dir] + [project_dir / n for n in nested(project_dir)]:
        f = folder / NOTES_FILE
        if not f.exists():
            f.write_text(NOTES_TEMPLATE.format(name=folder.name), encoding="utf-8")
            made.append(f)
    return made


def notes_unfilled(project_dir: Path) -> bool:
    return any(TEMPLATE_MARK in (f.read_text(encoding="utf-8", errors="replace") if f.exists() else TEMPLATE_MARK)
               for f in [project_dir / NOTES_FILE] + [project_dir / n / NOTES_FILE for n in nested(project_dir)])


def notes_for_prompt(project_dir: Path, limit: int = 5000) -> str:
    parts = []
    for folder in [project_dir] + [project_dir / n for n in nested(project_dir)]:
        f = folder / NOTES_FILE
        if f.exists():
            text = f.read_text(encoding="utf-8", errors="replace").strip()
            rel = NOTES_FILE if folder == project_dir else f"{folder.name}/{NOTES_FILE}"
            parts.append(f"### {rel}\n{text[:limit]}" + ("\n…(truncated)" if len(text) > limit else ""))
    return "\n\n".join(parts)


# --- which files and folders get a graph ----------------------------------------------------------

def _ignore_patterns(repo: Path) -> list[str]:
    f = repo / ".graphifyignore"
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines() if f.exists() else DEFAULT_IGNORE
    return [l.strip() for l in lines if l.strip() and not l.startswith("#")]


def _ignored(path: str, patterns: list[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    for p in patterns:
        if p.endswith("/"):
            d = p.rstrip("/")
            if d.startswith("**/"):
                if f"/{d[3:]}/" in f"/{path}":
                    return True
            elif path.startswith(d + "/"):
                return True
        elif fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p):
            return True
    return False


def code_files(repo: Path) -> list[str]:
    listed = git(repo, "ls-files", "--cached", "--others", "--exclude-standard").stdout.splitlines()
    patterns = _ignore_patterns(repo)
    return [p for p in listed if Path(p).suffix.lower() in CODE_EXT and not _ignored(p, patterns)
            and not p.startswith(("graphify-out/", ".kantor/"))]


def ensure_ignore(repo: Path) -> None:
    """A .graphifyignore (defaults plus code files over 1 MB) and local git excludes for graph output."""
    f = repo / ".graphifyignore"
    if not f.exists():
        big = [p for p in code_files_raw(repo) if (repo / p).is_file() and (repo / p).stat().st_size > MAX_CODE_FILE]
        f.write_text("# Kantor: index application code only (assets, third-party code, templates, seed data and\n"
                     "# huge files cost RAM on this server and add noise). Edit freely.\n"
                     + "\n".join(DEFAULT_IGNORE + big) + "\n", encoding="utf-8")
    exclude = repo / ".git" / "info" / "exclude"
    if exclude.parent.is_dir():
        have = exclude.read_text() if exclude.exists() else ""
        tracked_graph = bool(git(repo, "ls-files", "graphify-out").stdout.strip())
        add = [x for x in [".graphifyignore", ".kantor/"] + ([] if tracked_graph else ["graphify-out/"])
               if x not in have.split()]
        if add:
            with exclude.open("a") as fh:
                fh.write("\n" + "\n".join(add) + "\n")


def code_files_raw(repo: Path) -> list[str]:
    return [p for p in git(repo, "ls-files").stdout.splitlines() if Path(p).suffix.lower() in CODE_EXT]


def plan_parts(files: list[str], split: int) -> list[str]:
    """"" for the whole repo, else folders (one level deeper where a folder is still too big)."""
    if len(files) <= split:
        return [""]
    groups: dict[str, list[str]] = {}
    for f in files:
        if "/" in f:
            groups.setdefault(f.split("/", 1)[0], []).append(f)
    parts = []
    for top, members in sorted(groups.items()):
        if len(members) <= split:
            parts.append(top)
            continue
        sub: dict[str, int] = {}
        for m in members:
            rest = m.split("/", 2)
            if len(rest) > 2:
                sub[f"{rest[0]}/{rest[1]}"] = sub.get(f"{rest[0]}/{rest[1]}", 0) + 1
        parts.extend(sorted(sub))
    return parts


# --- building -------------------------------------------------------------------------------------

def _out_dir(repo: Path, part: str) -> Path:
    return repo if not part else repo / "graphify-out" / "parts" / part.replace("/", "_")


def graph_path(repo: Path, part: str) -> Path:
    return _out_dir(repo, part) / "graphify-out" / "graph.json"


def _fingerprint(repo: Path, part: str) -> str:
    tree = git(repo, "rev-parse", f"HEAD:{part}" if part else "HEAD^{tree}").stdout.strip()
    status = git(repo, "status", "--porcelain", "--", part or ".").stdout
    return hashlib.sha256(f"{tree}\n{status}".encode()).hexdigest()[:16]


def build(repo: Path, run, label_model: str | None, split: int) -> str:
    """Build or refresh the repo's graph(s); only parts whose code changed. `run(args, cwd)` runs a
    graphify command and returns (exit code, output). Returns a one-line summary."""
    if not GRAPHIFY.exists():
        return f"{repo.name}: graphify is not installed"
    ensure_ignore(repo)
    files = code_files(repo)
    if len(files) < MIN_CODE_FILES:
        return f"{repo.name}: {len(files)} code files, too few for a graph"
    parts = plan_parts(files, split)
    stamp_file = repo / "graphify-out" / ".kantor_stamp.json"
    stamps = json.loads(stamp_file.read_text()) if stamp_file.exists() else {}
    if stamps.get("_parts") != parts:  # the split changed: start over
        stamps = {}
        shutil.rmtree(repo / "graphify-out" / "parts", ignore_errors=True)
    label = [f"--backend=openai", f"--model={label_model}"] if label_model else ["--no-label"]
    built, failed = [], []
    for part in parts:
        fp = _fingerprint(repo, part)
        if stamps.get(part) == fp and graph_path(repo, part).exists():
            continue
        out = _out_dir(repo, part)
        target = part or "."
        extract = ["extract", target, "--code-only", "--max-workers", "1"] + (["--out", str(out)] if part else [])
        code, text = run(extract, repo)
        if code == 0:
            code, text = run(["cluster-only", str(out), "--no-viz", *label], repo)
        if code == 0:
            stamps[part] = fp
            built.append(part or "(whole repo)")
        else:
            failed.append(f"{part or 'repo'}: {text.strip().splitlines()[-1][:160] if text.strip() else code}")
    stamps["_parts"] = parts
    stamp_file.parent.mkdir(parents=True, exist_ok=True)
    stamp_file.write_text(json.dumps(stamps))
    if parts != [""]:
        write_index(repo, parts)
    if not built and not failed:
        return f"{repo.name}: graph up to date"
    return (f"{repo.name}: rebuilt {len(built)} of {len(parts)} part(s)"
            + (f"; failed: {'; '.join(failed)}" if failed else ""))


def _report_bits(report: Path, hubs: int = 8) -> tuple[str, list[str]]:
    if not report.exists():
        return "", []
    text = report.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^- (\d[\d,]* nodes · [\d,]+ edges · [\d,]+ communities)", text, re.M)
    hub_block = re.search(r"## Community Hubs[^\n]*\n((?:- .*\n?)+)", text)
    names = [h[2:].strip() for h in hub_block.group(1).splitlines()][:hubs] if hub_block else []
    return (m.group(1) if m else ""), names


def write_index(repo: Path, parts: list[str]) -> None:
    lines = [f"# Code graph index: {repo.name}", "",
             "This repo is too big for one graph on this server, so each folder below has its own.",
             "Ask the one for the folder you work in: graphify query \"<question>\" --graph <graph>", ""]
    for part in parts:
        size, hubs = _report_bits(_out_dir(repo, part) / "graphify-out" / "GRAPH_REPORT.md")
        lines.append(f"- **{part}** ({size or 'not built'}): graph `{graph_path(repo, part)}`"
                     + (f"\n  hubs: {', '.join(hubs)}" if hubs else ""))
    (repo / "graphify-out" / "KANTOR_INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def graph_for_prompt(project_dir: Path, limit: int = 3500) -> str:
    """What workers get about the code graphs: each repo's summary and how to query it."""
    out = []
    for repo in repos(project_dir):
        rel = "." if repo == project_dir else repo.name
        index = repo / "graphify-out" / "KANTOR_INDEX.md"
        report = repo / "graphify-out" / "GRAPH_REPORT.md"
        if index.exists():
            out.append(f"### {rel} (graph per folder)\n" + index.read_text(encoding="utf-8", errors="replace")[:limit])
        elif report.exists():
            text = report.read_text(encoding="utf-8", errors="replace")
            start = text.find("## Summary")
            out.append(f"### {rel}: graphify query \"<question>\" --graph {report.parent / 'graph.json'}\n"
                       + text[max(start, 0):max(start, 0) + limit])
    return "\n\n".join(out)
