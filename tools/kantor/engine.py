"""Kantor engine: a team of agent workers that hire, message, wait for and dismiss each other.

Each worker is a thread running an agent loop against 9router, with the orchestrator's file tools
plus team tools (list_workers, hire_worker, tell_worker, wait_for_workers, send_home). There is no
fixed manager: any worker given a big task can split it across workers it hires. A worker hired
by another worker gets its own git worktree and branch, so parallel workers never share files.

Workers are threads, not agent CLI processes, because the models run in 9router: a worker costs
about 2 MB of RAM. What costs RAM is the commands they run, so model calls and commands are gated
by the KANTOR_* limits in .env (measured on a 2 GB server; raise them when you scale up).
"""
from __future__ import annotations

import difflib
import json
import os
import pwd
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from orchestrator import LLM, MAX_TOOL_RESULT, TOOL_SPECS, Workspace, tool_spec  # noqa: E402
import knowledge as kb  # noqa: E402

# What each model is good for, shown to agents and in the UI (KANTOR_MODEL_NOTES replaces it).
# Checked against this 9router on 2026-11-03: all answer with tool calls.
MODEL_NOTES = {
    "ag/gemini-3.8-flash": "fast; simple edits, searches, summaries",
    "ag/claude-sonnet-4-6": "strong coder; most features and bugs",
    "ag/claude-opus-4-6-thinking": "strongest free model; architecture, hard bugs, reviews",
    "ollama/gpt-oss:120b": "solid open model on another provider (Ollama free plan)",
    "ollama/gemma4:31b": "small open model (Ollama free plan)",
    "kenari/glm-4-7-flash:free": "free, quick, weak",
    "kenari/qwen3-8-27b:free": "free, sometimes very slow",
}
DIFFICULTIES = ("easy", "normal", "hard")


def parse_pairs(text: str) -> dict[str, str]:
    """"model=note;model=note" -> {model: note}."""
    out = {}
    for item in (text or "").split(";"):
        model, _, note = item.partition("=")
        if model.strip():
            out[model.strip()] = note.strip()
    return out
NAMES = ["Ani", "Budi", "Citra", "Dodi", "Eka", "Fajar", "Gita", "Hadi", "Intan", "Joko", "Kiki",
         "Lina", "Made", "Nina", "Oki", "Putu", "Rina", "Sari", "Tono", "Udin", "Wati", "Yanto"]

# Statuses in which a worker is still on its task (wait_for_workers keeps waiting).
BUSY = {"working", "queued", "waiting_memory", "waiting_approval", "waiting_command", "waiting_team"}

# Commands that only read, run without approval when they have no shell syntax and no paths
# outside the worker's folder.
SAFE_COMMANDS = {"ls", "pwd", "cat", "head", "tail", "wc", "grep", "rg", "tree", "stat", "file", "du", "diff", "find"}
SAFE_GIT = {"status", "diff", "log", "show", "rev-parse", "ls-files", "blame"}
SAFE_GRAPHIFY = {"query", "explain", "path", "affected", "god-nodes"}  # read graph.json only
UNSAFE_ARGS = {"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprintf", "-fls", "--ext-diff"}
# Commands that can take hundreds of MB; they run one at a time (KANTOR_MAX_HEAVY_COMMANDS).
HEAVY_RX = re.compile(r"\b(npm|npx|pnpm|yarn|bun|node|next|vite|tsc|webpack|pip3?|poetry|uv|docker|"
                      r"cargo|go|mvn|gradle|make|composer|php|pytest)\b")

# Commands that still wait for the user in auto mode, with the reason shown on the approval.
RISKY_COMMANDS = [
    (r"/var/lib/kantor|kantor\.db|/proc/[^\s]*/environ", "touches Kantor's own state or secrets"),
    (r"\bgit\s+push\b", "pushes to another machine"),
    (r"(?<![\w./-])(ssh|scp|sftp)\b|\brsync\b[^\n]*:", "reaches another machine"),  # not ~/.ssh/ paths
    (r"\b(curl|wget)\b[^\n]*\|\s*(sudo\s+)?(sh|bash|zsh|python3?)\b", "runs a downloaded script"),
    (r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(-[a-zA-Z]+\s+)*(/|~|\$HOME|\*|\.\.?/?)(\s|$)", "deletes a whole tree"),
    (r"\b(sudo|su|systemctl|crontab|shutdown|reboot|mkfs|pkill|killall)\b|\bdd\s+if=|\bkill\s+-9\s+-1\b",
     "system-level command"),
    (r"\bchmod\s+(-R\s+)?[0-7]*7[0-7]*\s+/|\bchown\b", "changes permissions or owners"),
]


def risky_reason(command: str) -> str | None:
    for pattern, reason in RISKY_COMMANDS:
        if re.search(pattern, command):
            return reason
    return None


MAX_HISTORY_CHARS = 400_000
EVENT_TEXT_LIMIT = 20_000


def clip(text: str | None, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def mem_available_mb() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


def is_safe_command(command: str) -> bool:
    if re.search(r"[;&|<>`$(){}\\\n]", command):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    args = parts[1:]
    if parts[0] == "graphify":  # graph paths are absolute, which is fine for a read-only query
        return bool(args) and args[0] in SAFE_GRAPHIFY and "--output" not in command
    if parts[0] not in SAFE_COMMANDS | {"git"}:
        return False
    if any(a.startswith(("/", "~")) or ".." in a or a in UNSAFE_ARGS or a.startswith("--output") for a in args):
        return False
    return parts[0] != "git" or (bool(args) and args[0] in SAFE_GIT)


class TeamError(Exception):
    """A team request that can't be done; its message goes back to the agent or the user."""


class Config:
    def __init__(self, env=os.environ):
        def num(key: str, default: int) -> int:
            try:
                return int(env.get(key, default))
            except ValueError:
                return default

        def flag(key: str, default: bool) -> bool:
            return env.get(key, str(default)).strip().lower() in ("1", "true", "yes", "on")

        self.api_key = env.get("NINEROUTER_API_KEY", "")
        self.base_url = env.get("KANTOR_BASE_URL") or "http://127.0.0.1:20128/v1"
        self.max_workers = num("KANTOR_MAX_WORKERS", 10)
        self.max_active = num("KANTOR_MAX_ACTIVE", 4)
        self.max_commands = num("KANTOR_MAX_COMMANDS", 2)
        self.max_heavy = num("KANTOR_MAX_HEAVY_COMMANDS", 1)
        self.max_depth = num("KANTOR_MAX_HIRE_DEPTH", 2)
        self.min_free_mb = num("KANTOR_MIN_FREE_MB", 250)
        self.max_steps = num("KANTOR_MAX_STEPS", 40)
        self.auto_edit = flag("KANTOR_AUTO_EDIT", True)
        # Auto mode's starting value; the switch in the UI overrides it from then on.
        self.auto_approve = flag("KANTOR_AUTO_APPROVE_COMMANDS", False)
        self.auto_mode_paid = flag("KANTOR_AUTO_MODE_PAID", False)
        self.data_dir = Path(env.get("KANTOR_DATA_DIR") or Path.home() / ".kantor")
        self.projects_dir = Path(env.get("KANTOR_PROJECTS_DIR") or Path.home() / "projects")
        def models(key: str, default: str) -> list[str]:
            return [m.strip() for m in (env.get(key) or default).split(",") if m.strip()]

        # A worker's model comes from its task's difficulty, unless someone names one.
        self.tiers = {
            "easy": env.get("KANTOR_MODEL_EASY") or "ag/gemini-3.8-flash",
            "normal": env.get("KANTOR_MODEL_NORMAL") or env.get("KANTOR_DEFAULT_MODEL") or "ag/claude-sonnet-4-6",
            "hard": env.get("KANTOR_MODEL_HARD") or "ag/claude-opus-4-6-thinking",
        }
        self.default_model = self.tiers["normal"]
        # Project memory: KANTOR.md notes and graphify code graphs (see knowledge.py).
        self.graph = flag("KANTOR_GRAPH", True)
        self.graph_label_model = env.get("KANTOR_GRAPH_LABEL_MODEL", "ag/gemini-3.8-flash") or None
        self.graph_split_files = num("KANTOR_GRAPH_SPLIT_FILES", 1500)
        self.auto_notes = flag("KANTOR_AUTO_NOTES", True)
        self.classifier_model = env.get("KANTOR_CLASSIFIER_MODEL") or self.tiers["easy"]
        # Tried in order when a worker's own model fails.
        self.fallback_models = models("KANTOR_FALLBACK_MODELS", "ag/claude-sonnet-4-6,ollama/gpt-oss:120b")
        # Last resort when every model above fails (usually limits): weaker, so the UI warns.
        self.cheap_models = models("KANTOR_CHEAP_MODELS",
                                   "ollama/gemma4:31b,kenari/glm-4-7-flash:free,kenari/qwen3-8-27b:free")
        # Never used automatically; an agent must justify one and the user must approve it.
        self.paid_models = parse_pairs(env.get("KANTOR_PAID_MODELS", ""))
        notes = parse_pairs(env["KANTOR_MODEL_NOTES"]) if env.get("KANTOR_MODEL_NOTES") else MODEL_NOTES
        self.models = {m: notes.get(m, "") for m in
                       [*self.tiers.values(), *self.fallback_models, *self.cheap_models] if m not in self.paid_models}


class Events:
    """Everything that happens, for the UI: kept in SQLite, and the latest in memory for live streams."""

    def __init__(self, db: "Store"):
        self.db = db
        self.cond = threading.Condition()
        self.recent: deque[dict] = deque(maxlen=3000)
        self.last_id = db.last_event_id()

    def emit(self, worker: str | None, kind: str, text: str = "", data: dict | None = None) -> None:
        text = clip(text, EVENT_TEXT_LIMIT)
        with self.cond:
            ev = {"id": self.db.add_event(worker, kind, text, data), "ts": time.time(),
                  "worker": worker, "kind": kind, "text": text, "data": data}
            self.last_id = ev["id"]
            self.recent.append(ev)
            self.cond.notify_all()

    def wait(self, after: int, timeout: float) -> list[dict]:
        with self.cond:
            if self.last_id <= after:
                self.cond.wait(timeout)
            return [e for e in self.recent if e["id"] > after]


class Store:
    """SQLite state: workers (with their conversation), events and login sessions."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("pragma journal_mode=wal")
        self.conn.executescript("""
            create table if not exists workers(id text primary key, name text, project text, cwd text,
              branch text, parent text, depth int, model text, status text, task text, report text,
              history text, created real);
            create table if not exists events(id integer primary key autoincrement, ts real, worker text,
              kind text, text text, data text);
            create index if not exists events_worker on events(worker, id);
            create table if not exists sessions(hash text primary key, expires real);
            create table if not exists settings(key text primary key, value text);
        """)
        for column in ("usage text", "last_model text"):  # added after the first release
            try:
                self.conn.execute(f"alter table workers add column {column}")
            except sqlite3.OperationalError:
                pass

    def q(self, sql: str, args: tuple = ()) -> list:
        with self.lock:
            cur = self.conn.execute(sql, args)
            rows = cur.fetchall()
            self.conn.commit()
            return rows

    def last_event_id(self) -> int:
        return self.q("select coalesce(max(id), 0) from events")[0][0]

    def add_event(self, worker, kind, text, data) -> int:
        with self.lock:
            cur = self.conn.execute("insert into events(ts, worker, kind, text, data) values (?,?,?,?,?)",
                                    (time.time(), worker, kind, text, json.dumps(data) if data else None))
            self.conn.commit()
            return cur.lastrowid

    def events_for(self, worker: str, before: int | None, limit: int) -> list[dict]:
        rows = self.q("select id, ts, worker, kind, text, data from events where worker = ? and id < ? "
                      "order by id desc limit ?", (worker, before or 2**62, limit))
        return [{"id": r[0], "ts": r[1], "worker": r[2], "kind": r[3], "text": r[4],
                 "data": json.loads(r[5]) if r[5] else None} for r in reversed(rows)]

    def prune_events(self, keep: int = 50_000) -> None:
        self.q("delete from events where id <= (select max(id) from events) - ?", (keep,))


class KantorWorkspace(Workspace):
    """The orchestrator's file tools, with approvals going to the UI and commands gated by the limits."""

    def __init__(self, worker: "Worker"):
        super().__init__(Path(worker.cwd), auto_edit=worker.office.cfg.auto_edit, yes=False)
        self.worker = worker

    def _writable(self, path: str) -> Path:
        full = self._path(path)
        parts = full.relative_to(self.root).parts
        # .git/config and hooks can make later commands run anything; .kantor holds other workers' trees.
        if parts and parts[0] in (".git", ".kantor"):
            raise ValueError(f"{parts[0]}/ can't be written with file tools")
        return full

    def write_file(self, path: str, content: str) -> str:
        self._writable(path)
        return super().write_file(path, content)

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        self._writable(path)
        return super().edit_file(path, old_text, new_text)

    def _confirm_edit(self, path: str, old: str, new: str) -> bool:
        diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}"))
        office = self.worker.office
        office.events.emit(self.worker.id, "diff", diff, {"path": path})
        return self.auto_edit or office.auto_mode or office.request_approval(self.worker, "edit", f"Edit {path}", diff)

    def run_command(self, command: str, timeout: int = 120) -> str:
        office, worker = self.worker.office, self.worker
        try:
            timeout = max(5, min(int(timeout), 900))
        except (TypeError, ValueError):
            timeout = 120
        office.events.emit(worker.id, "command", command)
        risky = risky_reason(command)
        if not (is_safe_command(command) or (office.auto_mode and not risky)):
            detail = f"in {self.root}" + (f"\nHeld even in auto mode: {risky}" if risky and office.auto_mode else "")
            if not office.request_approval(worker, "command", command, detail, risky=risky):
                return "DENIED: the user rejected this command"
        heavy = bool(HEAVY_RX.search(command))
        gate = office.heavy_gate if heavy else office.light_gate
        with worker.waiting(gate, "waiting_command"):
            office.wait_for_memory(worker)
            if worker.cancel.is_set():
                return "CANCELLED: the worker was stopped"
            out, code = self._spawn(command, timeout)
        if len(out) > MAX_TOOL_RESULT:
            half = MAX_TOOL_RESULT // 2
            out = out[:half] + "\n... (output truncated) ...\n" + out[-half:]
        office.events.emit(worker.id, "output", out, {"code": code})
        return f"exit code {code}\n{out}"

    def _spawn(self, command: str, timeout: int) -> tuple[str, int | str]:
        user = pwd.getpwuid(os.getuid())
        # Commands never see Kantor's own environment (API key, password hash).
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if k in os.environ}
        env.update(HOME=user.pw_dir, USER=user.pw_name, TERM="dumb", CI="1", GIT_TERMINAL_PROMPT="0",
                   KANTOR_WORKER=self.worker.name)
        env["PATH"] = f"{user.pw_dir}/.local/bin:" + env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        # If the RAM cap is hit, the kernel kills the agent's command, not Kantor itself.
        command = f"echo 500 > /proc/self/oom_score_adj 2>/dev/null; {command}"
        proc = subprocess.Popen(command, shell=True, cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace", start_new_session=True)
        self.worker.proc = proc
        try:
            out, _ = proc.communicate(timeout=timeout)
            return out, proc.returncode
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            return out + f"\n(killed: timed out after {timeout}s)", "timeout"
        finally:
            self.worker.proc = None


TEAM_SPECS = {
    "list_workers": tool_spec("list_workers", "List every worker in the office: status, task, model, "
                              "branch and last report.", {}, []),
    "hire_worker": tool_spec(
        "hire_worker", "Hire a new worker for a part of your task. It starts right away in its own git "
        "worktree and branch (kantor/<name>) cut from your current commit. Give it everything it needs: "
        "it cannot see your conversation.",
        {"task": {"type": "string", "description": "The complete, self-contained task."},
         "difficulty": {"type": "string", "enum": list(DIFFICULTIES),
                        "description": "Picks the model tier. Left out, the office judges it from the task."},
         "model": {"type": "string", "description": "A specific model instead of the difficulty's."},
         "justification": {"type": "string", "description": "Required for a paid model: why the free "
                           "tiers are not enough for this task."},
         "repos": {"type": "array", "items": {"type": "string"},
                   "description": "Multi-repo project only: the repos it needs (each is a full copy of "
                   "the code). Default: all."},
         "name": {"type": "string", "description": "Optional name."}},
        ["task"]),
    "switch_model": tool_spec(
        "switch_model", "Change the model you run on, from your next step. A paid model needs a justification "
        "and the user's approval.",
        {"model": {"type": "string"}, "justification": {"type": "string"}}, ["model"]),
    "tell_worker": tool_spec("tell_worker", "Send a message to another worker; it handles it as its next "
                             "prompt (a finished worker starts again with it).",
                             {"worker": {"type": "string"}, "message": {"type": "string"}}, ["worker", "message"]),
    "wait_for_workers": tool_spec(
        "wait_for_workers", "Wait until these workers finish what they are doing, then get their reports. "
        "Use this after hiring or telling workers, instead of checking again and again.",
        {"workers": {"type": "array", "items": {"type": "string"}},
         "timeout_minutes": {"type": "integer", "description": "Default 30."}}, ["workers"]),
    "send_home": tool_spec(
        "send_home", "Dismiss workers you hired, once their work is merged or no longer needed.",
        {"workers": {"type": "array", "items": {"type": "string"}},
         "delete_worktree": {"type": "boolean", "description": "Also remove their worktree (only when it "
                             "has no uncommitted changes; the branch is kept)."}}, ["workers"]),
}
WORK_TOOLS = ["list_files", "read_file", "search", "write_file", "edit_file", "run_command"]


class Worker:
    def __init__(self, office: "Office", row: dict):
        self.office = office
        self.id: str = row["id"]
        self.name: str = row["name"]
        self.project: str = row["project"]
        self.cwd: str = row["cwd"]
        self.branch: str | None = row.get("branch")
        self.parent: str | None = row.get("parent")
        self.depth: int = row.get("depth") or 0
        self.model: str = row["model"]
        self.status: str = row.get("status") or "idle"
        self.task: str = row.get("task") or ""
        self.report: str = row.get("report") or ""
        self.history: list[dict] = row.get("history") or []
        self.created: float = row.get("created") or time.time()
        self.usage: dict[str, int] = row.get("usage") or {}  # tokens per model
        self.last_model: str | None = row.get("last_model")
        self.model_note = ""  # how its model was picked, for the hirer
        self.ephemeral = False  # goes home by itself after its task (the note taker)
        self.inbox: deque[tuple[str, str]] = deque()
        self.cond = threading.Condition()
        self.cancel = threading.Event()
        self.gone = self.status == "home"
        self.proc: subprocess.Popen | None = None
        self.ws = KantorWorkspace(self)
        self.thread = threading.Thread(target=self._loop, name=f"worker-{self.name}", daemon=True)

    # --- state ---------------------------------------------------------------------------------

    def row(self) -> dict:
        parent = self.office.workers.get(self.parent) if self.parent else None
        return {"id": self.id, "name": self.name, "project": self.project, "branch": self.branch,
                "cwd": self.cwd, "parent": parent.name if parent else None, "depth": self.depth,
                "model": self.model, "paid": self.model in self.office.cfg.paid_models,
                "last_model": self.last_model, "usage": self.usage, "status": self.status,
                "task": clip(self.task, 400), "report": clip(self.report, 3000), "created": self.created,
                "inbox": len(self.inbox)}

    def save(self) -> None:
        self.office.db.q(
            "insert or replace into workers(id, name, project, cwd, branch, parent, depth, model, status, "
            "task, report, history, created, usage, last_model) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.id, self.name, self.project, self.cwd, self.branch, self.parent, self.depth, self.model,
             self.status, self.task, self.report, json.dumps(self.history), self.created,
             json.dumps(self.usage), self.last_model))

    def set_status(self, status: str) -> None:
        if status == self.status:
            return
        self.status = status
        self.office.events.emit(self.id, "status", status, self.row())
        with self.office.changed:
            self.office.changed.notify_all()

    def busy(self) -> bool:
        return self.status in BUSY or bool(self.inbox)

    def waiting(self, gate: threading.BoundedSemaphore, status: str):
        """Context manager: hold a gate, showing `status` while queued for it."""
        worker = self

        class _Hold:
            def __enter__(self):
                if not gate.acquire(blocking=False):
                    before = worker.status
                    worker.set_status(status)
                    gate.acquire()
                    worker.set_status(before if before not in ("queued", status) else "working")

            def __exit__(self, *exc):
                gate.release()

        return _Hold()

    # --- messages and the agent loop -------------------------------------------------------------

    def start(self) -> None:
        if not self.gone:
            self.thread.start()

    def send(self, sender: str, text: str) -> None:
        with self.cond:
            self.inbox.append((sender, text))
            self.cond.notify()
        self.office.events.emit(self.id, "user", text, {"from": sender})
        with self.office.changed:
            self.office.changed.notify_all()

    def stop(self) -> None:
        """Interrupt the current task (the worker stays, and takes the next message)."""
        self.cancel.set()
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _loop(self) -> None:
        while True:
            with self.cond:
                while not self.inbox and not self.gone:
                    self.cond.wait()
                if self.gone:
                    return
                messages = list(self.inbox)
                self.inbox.clear()
            self.cancel.clear()
            for sender, text in messages:
                who = "the user (your boss)" if sender == "user" else f"worker {sender}"
                self.history.append({"role": "user", "content": f"Message from {who}:\n{text}"})
            self.set_status("working")
            try:
                self.report = self._turn()
                status = "idle"
            except Exception as e:
                self.report = f"ERROR: {e}"
                self.office.events.emit(self.id, "error", str(e))
                status = "error"
            if self.gone:
                return
            self.office.events.emit(self.id, "report", self.report)
            self.save()
            self.set_status(status)
            if self.parent is None:  # it works in the main checkout, so the code graph may be stale now
                if self.ephemeral:
                    self.office.send_home(self)
                    return
                self.office.schedule_graph(self.project)

    def _system_prompt(self) -> str:
        cfg = self.office.cfg
        parent = self.office.workers.get(self.parent) if self.parent else None
        tiers = "\n".join(f"  - {d}: {m} ({cfg.models.get(m, '')})" for d, m in cfg.tiers.items())
        paid = "\n".join(f"  - {m}: {n}" for m, n in cfg.paid_models.items()) or "  (none configured)"
        models = (f"Give each worker you hire a difficulty, which picks its model:\n{tiers}\n"
                  f"Other models you may name instead: {', '.join(cfg.models)}.\n"
                  f"Paid models, which cost the user money:\n{paid}\n"
                  "Use a paid model only when the task truly needs it: the hard tier already failed at it, or "
                  "it is critical and beyond the hard tier. Name it with a justification of why the free "
                  "tiers are not enough; the user must approve it. If they don't, carry on with the hard "
                  "tier. switch_model changes your own model the same way.")
        can_hire = self.depth < cfg.max_depth
        lines = [
            f"You are {self.name}, a worker in Kantor, a small office of AI agents that build software together.",
            f"Project: {self.project}. Your working directory: {self.cwd}. File tool paths are relative to it.",
            f"You were hired by {parent.name if parent else 'the user, who is the boss'}.",
            f"You work on git branch {self.branch} in your own worktree. Commit your work to it before you "
            "report (git add -A && git commit -m ...), so whoever hired you can merge it." if self.branch else
            "You work directly in the project's main checkout.",
            "",
            "Team tools: list_workers, hire_worker, tell_worker, wait_for_workers, send_home, switch_model.",
            "Do a small task yourself. Split a big task into independent parts and hire one worker per part "
            f"(at most {cfg.max_active} think at once; the rest queue), then wait_for_workers, review their "
            "branches (git log/diff kantor/<name>), merge them into your checkout with run_command, run the "
            "tests, send them home, and report." if can_hire else
            "You cannot hire workers (the hiring depth limit is reached); do the task yourself.",
            models,
            "",
            "Shell commands need the user's approval unless they only read (ls, cat, grep, git status/diff/log).",
            "Heavy commands (npm, node, builds, pip, docker) queue one at a time: the server has little RAM.",
            "Finish every task with a short report: what you did, what is left, and where the changes are.",
            "Reply in the language the user writes in.",
        ]
        return "\n".join(lines + self._project_context())

    def _project_context(self) -> list[str]:
        """Multi-repo layout, the project's KANTOR.md notes and its code graph summary."""
        cfg = self.office.cfg
        project_dir = cfg.projects_dir / self.project
        out = []
        inner = kb.nested(Path(self.cwd))
        if inner:
            out += ["", f"This is a multi-repo project: {', '.join(inner)} are separate git repos inside your "
                    "folder. Run git per repo (git -C <repo> status)."
                    + (f" Each has your branch {self.branch}; commit in every repo you changed." if self.branch else
                       " To merge a worker's work, merge its branch in each repo it changed "
                       "(git -C <repo> merge kantor/<name>). When hiring, pass repos to give a worker only the "
                       "repos it needs: each one is a full copy of that code.")]
        notes = kb.notes_for_prompt(project_dir, limit=3000)
        if notes:
            out += ["", "Project notes (KANTOR.md). Follow them. "
                    + ("You work in the main checkout, so keep them current: when you learn something durable "
                       "(architecture, how to run or test, a convention, a pitfall), edit the KANTOR.md it "
                       "belongs to, and put in what your workers report learning. Replace '(belum diisi)'."
                       if self.parent is None else
                       "You can't edit them from your worktree: put anything durable you learn about the "
                       "project in your report, so your hirer can add it."),
                    notes]
        if cfg.graph:
            graph = kb.graph_for_prompt(project_dir, limit=1200)
            if graph:
                out += ["", "Code graph of the main checkout (it can lag behind uncommitted work). Use it "
                        "before searching file by file: graphify query \"<question>\" --graph <graph.json>, "
                        "graphify explain \"<name>\" --graph <graph.json>, graphify affected \"<name>\" --graph "
                        "<graph.json> (what depends on it). These only read and run without approval.",
                        graph]
        return out

    def _tools(self) -> list[dict]:
        return [TOOL_SPECS[t] for t in WORK_TOOLS] + list(TEAM_SPECS.values())

    def _turn(self) -> str:
        office = self.office
        tools = self._tools()
        nudged = False
        for _ in range(office.cfg.max_steps):
            if self.cancel.is_set():
                return "(stopped by the user)"
            msg = office.chat(self, [{"role": "system", "content": self._system_prompt()}] + self.history, tools)
            text = (msg.get("content") or "").strip()
            calls = msg.get("tool_calls") or []
            if text and calls:
                office.events.emit(self.id, "assistant", text)
            if not calls:
                if text:
                    office.events.emit(self.id, "assistant", text)
                    self.history.append({"role": "assistant", "content": text})
                    return text
                if nudged:
                    return "(no text output)"
                nudged = True  # some models end a turn with neither text nor a tool call
                self.history.append({"role": "user", "content": "Write your report now, as text."})
                continue
            for call in calls:
                call.setdefault("id", f"call_{uuid.uuid4().hex[:12]}")
                call.setdefault("type", "function")
            self.history.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                self.history.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": clip(self._run_call(call), MAX_TOOL_RESULT)})
            self._compact()
            self.save()
        office.events.emit(self.id, "system", f"reached {office.cfg.max_steps} steps; asking for a report")
        self.history.append({"role": "user", "content": "You have used all your steps for this task. Report "
                             "now, as text, from what you have: what is done and what is not."})
        msg = office.chat(self, [{"role": "system", "content": self._system_prompt()}] + self.history, tools,
                          final=True)
        text = (msg.get("content") or "").strip() or "(stopped: step limit reached)"
        self.history.append({"role": "assistant", "content": text})
        office.events.emit(self.id, "assistant", text)
        return text

    def _run_call(self, call: dict) -> str:
        name = call.get("function", {}).get("name", "")
        if self.cancel.is_set():
            return "CANCELLED: the worker was stopped"
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except (ValueError, KeyError):
            return "ERROR: tool arguments were not valid JSON"
        shown = ", ".join(f"{k}={clip(str(v), 80)!r}" for k, v in args.items()
                          if k not in ("content", "old_text", "new_text", "task", "message"))
        self.office.events.emit(self.id, "tool", f"{name}({shown})")
        try:
            if name in TEAM_SPECS:
                result = getattr(self.office, f"tool_{name}")(self, **args)
            elif name in WORK_TOOLS:
                result = self.ws.call(name, args, WORK_TOOLS)
            else:
                result = f"ERROR: there is no tool called {name}"
        except TeamError as e:
            result = f"ERROR: {e}"
        except TypeError as e:
            result = f"ERROR: bad arguments for {name}: {e}"
        if name not in ("run_command",):  # command output already has its own event
            self.office.events.emit(self.id, "result", clip(result, 4000), {"tool": name})
        return result

    def _compact(self) -> None:
        """Keep the conversation under MAX_HISTORY_CHARS by blanking the oldest tool outputs."""
        size = sum(len(m.get("content") or "") for m in self.history)
        for m in self.history[:-10]:
            if size <= MAX_HISTORY_CHARS * 0.6:
                break
            if m.get("role") == "tool" and len(m.get("content") or "") > 200:
                size -= len(m["content"])
                m["content"] = "(old tool output removed to save memory)"

    def repair_history(self) -> None:
        """After a restart mid-task: answer tool calls that never got a result."""
        answered = {m.get("tool_call_id") for m in self.history if m.get("role") == "tool"}
        fixed = []
        for m in self.history:
            fixed.append(m)
            for call in m.get("tool_calls") or []:
                if call.get("id") not in answered:
                    fixed.append({"role": "tool", "tool_call_id": call.get("id"),
                                  "content": "INTERRUPTED: Kantor restarted before this finished"})
        self.history = fixed


class Office:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Store(cfg.data_dir / "kantor.db")
        self.db.prune_events()
        self.events = Events(self.db)
        self.llm = LLM(cfg.base_url, cfg.api_key)
        self.lock = threading.RLock()
        self.changed = threading.Condition()
        self.active_gate = threading.BoundedSemaphore(cfg.max_active)
        self.light_gate = threading.BoundedSemaphore(cfg.max_commands)
        self.heavy_gate = threading.BoundedSemaphore(cfg.max_heavy)
        self.approvals: dict[str, dict] = {}
        saved = self.db.q("select value from settings where key = 'auto_mode'")
        self._auto_mode = saved[0][0] == "1" if saved else cfg.auto_approve
        self.workers: dict[str, Worker] = {}
        cfg.projects_dir.mkdir(parents=True, exist_ok=True)
        # Code graphs are built one project at a time in the background (see knowledge.py).
        self.graph_cond = threading.Condition()
        self.graph_pending: list[str] = []
        self.graph_status: dict[str, str] = {}
        self._load()
        threading.Thread(target=self._graph_loop, name="graphs", daemon=True).start()
        for project in self.projects():  # set up what already exists: notes, then graphs
            self.setup_project(project)

    # --- project memory: KANTOR.md and code graphs ----------------------------------------------

    def setup_project(self, project: str) -> None:
        try:
            made = kb.ensure_notes(self.cfg.projects_dir / project)
        except OSError as e:
            self.events.emit(None, "error", f"{project}: could not write KANTOR.md: {e}")
            return
        if made:
            self.events.emit(None, "graph", f"{project}: created {', '.join(str(m.relative_to(self.cfg.projects_dir)) for m in made)}",
                             {"project": project, "state": "notes"})
        self.schedule_graph(project)

    def schedule_graph(self, project: str) -> None:
        if not self.cfg.graph:
            return
        with self.graph_cond:
            if project not in self.graph_pending:
                self.graph_pending.append(project)
                self.graph_cond.notify()

    def _graph_loop(self) -> None:
        while True:
            with self.graph_cond:
                while not self.graph_pending:
                    self.graph_cond.wait()
                project = self.graph_pending.pop(0)
            try:
                self._build_graphs(project)
            except Exception as e:
                self.graph_status[project] = f"failed: {clip(str(e), 200)}"
                self.events.emit(None, "graph", f"{project}: graph update failed: {clip(str(e), 300)}",
                                 {"project": project, "state": "failed"})

    def _build_graphs(self, project: str) -> None:
        project_dir = self.cfg.projects_dir / project
        if not project_dir.is_dir():
            return
        self.graph_status[project] = "updating"
        self.events.emit(None, "graph", f"{project}: updating the code graph", {"project": project, "state": "running"})
        results = [kb.build(repo, self._graph_run, self.cfg.graph_label_model, self.cfg.graph_split_files)
                   for repo in kb.repos(project_dir)]
        summary = "; ".join(results) or "no git repos"
        self.graph_status[project] = summary
        self.events.emit(None, "graph", f"{project}: {summary}", {"project": project, "state": "done"})
        has_code = any(len(kb.code_files(r)) >= kb.MIN_CODE_FILES for r in kb.repos(project_dir))
        if self.cfg.auto_notes and has_code and kb.notes_unfilled(project_dir):
            self._hire_note_taker(project)

    def _graph_run(self, args: list[str], cwd: Path) -> tuple[int, str]:
        """Run graphify as a heavy command: one at a time, after the free-RAM gate, first to be killed."""
        with self.heavy_gate:
            while mem_available_mb() < self.cfg.min_free_mb:
                time.sleep(5)
            env = {k: os.environ[k] for k in ("LANG", "LC_ALL", "TZ") if k in os.environ}
            home = pwd.getpwuid(os.getuid()).pw_dir
            env.update(HOME=home, PATH=f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin", GIT_TERMINAL_PROMPT="0")
            if self.cfg.graph_label_model:  # community names, through 9router
                env.update(OPENAI_BASE_URL=self.cfg.base_url, OPENAI_API_KEY=self.cfg.api_key,
                           OPENAI_MODEL=self.cfg.graph_label_model)
            cmd = ["sh", "-c", 'echo 900 > /proc/self/oom_score_adj 2>/dev/null; exec "$@"', "sh", str(kb.GRAPHIFY), *args]
            try:
                p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, errors="replace",
                                   timeout=1800, stdin=subprocess.DEVNULL)
                return p.returncode, p.stdout + p.stderr
            except subprocess.TimeoutExpired:
                return 124, "timed out after 30 minutes"

    def _hire_note_taker(self, project: str) -> None:
        """Once per project: a worker that fills in the KANTOR.md templates from the code and the graph."""
        key = f"notes_bootstrapped:{project}"
        if self.db.q("select 1 from settings where key = ?", (key,)):
            return
        self.db.q("insert or replace into settings(key, value) values (?, '1')", (key,))
        inner = kb.nested(self.cfg.projects_dir / project)
        where = "KANTOR.md di folder project" + (f" dan di tiap repo ({', '.join(f'{r}/KANTOR.md' for r in inner)})"
                                                  if inner else "")
        task = (f"Tugasmu hanya menulis catatan project: isi {where}. Pakai ringkasan code graph dan catatan di "
                "prompt-mu, lalu baca README, file konfigurasi (composer.json, package.json, pubspec.yaml, "
                "requirements.txt, dll.), struktur folder, dan beberapa file inti. Untuk setiap file, isi: "
                "ringkasan, arsitektur (modul utama" + (" dan hubungan antar repo" if inner else "") + "), cara "
                "menjalankan dan test, konvensi, serta jebakan. Ganti semua '(belum diisi)'; tulis 'belum "
                "diketahui' kalau tidak bisa dipastikan dari kode. Maksimal sekitar 80 baris per file. Jangan "
                "ubah file lain, jangan jalankan perintah yang mengubah apa pun, jangan commit, dan jangan "
                "merekrut worker.")
        name = "Pencatat" if "pencatat" not in {w.name.lower() for w in self.present()} else None
        try:
            w = self.hire(project, task, name=name, difficulty="normal")
        except TeamError as e:
            self.events.emit(None, "graph", f"{project}: could not start the note taker: {e}",
                             {"project": project, "state": "failed"})
            return
        w.ephemeral = True

    def _load(self) -> None:
        cols = ["id", "name", "project", "cwd", "branch", "parent", "depth", "model", "status", "task",
                "report", "history", "created", "usage", "last_model"]
        for values in self.db.q(f"select {', '.join(cols)} from workers order by created"):
            row = dict(zip(cols, values))
            row["history"] = json.loads(row["history"] or "[]")
            row["usage"] = json.loads(row["usage"] or "{}")
            if row["status"] != "home":
                row["status"] = "idle" if row["status"] in BUSY else row["status"]
            w = Worker(self, row)
            w.repair_history()
            self.workers[w.id] = w
        for w in self.workers.values():
            w.start()

    # --- lookups -----------------------------------------------------------------------------------

    def present(self) -> list[Worker]:
        return [w for w in self.workers.values() if not w.gone]

    def find(self, key: str) -> Worker:
        key = (key or "").strip()
        for w in self.present():
            if w.id == key or w.name.lower() == key.lower():
                return w
        names = ", ".join(w.name for w in self.present()) or "nobody"
        raise TeamError(f"no worker called {key!r} (there is: {names})")

    def projects(self) -> list[str]:
        return sorted(p.name for p in self.cfg.projects_dir.iterdir() if p.is_dir() and not p.name.startswith("."))

    def state(self) -> dict:
        return {
            "workers": [w.row() for w in self.workers.values() if not w.gone],
            "approvals": [a["public"] for a in self.approvals.values()],
            "projects": self.projects(),
            "models": self.cfg.models,
            "default_model": self.cfg.default_model,
            "tiers": self.cfg.tiers,
            "fallback_models": self.cfg.fallback_models,
            "cheap_models": self.cfg.cheap_models,
            "paid_models": self.cfg.paid_models,
            "paid_tokens": sum(n for w in self.workers.values() for m, n in w.usage.items()
                               if m in self.cfg.paid_models),
            "limits": {"max_workers": self.cfg.max_workers, "max_active": self.cfg.max_active,
                       "max_commands": self.cfg.max_commands, "max_heavy": self.cfg.max_heavy,
                       "max_depth": self.cfg.max_depth, "min_free_mb": self.cfg.min_free_mb},
            "graph_status": self.graph_status,
            "auto_mode": self._auto_mode,
            "auto_mode_paid": self.cfg.auto_mode_paid,
            "mem_available_mb": mem_available_mb(),
            "last_event_id": self.events.last_id,
        }

    # --- model calls and gates --------------------------------------------------------------------

    def wait_for_memory(self, worker: Worker) -> None:
        while mem_available_mb() < self.cfg.min_free_mb and not worker.cancel.is_set():
            before = worker.status
            worker.set_status("waiting_memory")
            time.sleep(3)
            if mem_available_mb() >= self.cfg.min_free_mb:
                worker.set_status(before if before != "waiting_memory" else "working")

    def _call(self, worker: Worker, model: str, messages: list, tools: list | None, final: bool,
              errors: list) -> dict | None:
        body = {"model": model, "messages": messages, "stream": False}
        if tools:
            body["tools"] = tools
            if final:
                body["tool_choice"] = "none"
        with worker.waiting(self.active_gate, "queued"):
            self.wait_for_memory(worker)
            if worker.cancel.is_set():
                raise RuntimeError("stopped by the user")
            try:
                data = self.llm._post(body)
                msg = data["choices"][0]["message"]
            except Exception as e:
                errors.append(f"{model}: {clip(str(e), 200)}")
                self.events.emit(worker.id, "error", f"{model} failed: {clip(str(e), 300)}")
                return None
        usage = data.get("usage") or {}
        tokens = usage.get("total_tokens") or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
        worker.usage[model] = worker.usage.get(model, 0) + int(tokens or 0)
        return msg

    def chat(self, worker: Worker, messages: list, tools: list, final: bool = False) -> dict:
        """The worker's model, then the fallbacks (twice, since limits often clear in seconds), then
        the cheap models with a warning. Paid models are never a fallback."""
        primary = [worker.model] + [m for m in self.cfg.fallback_models if m != worker.model]
        cheap = [m for m in self.cfg.cheap_models if m not in primary]
        errors: list[str] = []
        for attempt in range(2):
            for model in primary:
                msg = self._call(worker, model, messages, tools, final, errors)
                if msg is not None:
                    if worker.last_model in cheap:
                        self.events.emit(worker.id, "notice", f"{worker.name} is back on {model}")
                    worker.last_model = model
                    return msg
            if attempt == 0:
                worker.cancel.wait(15)  # Antigravity locks a model for a few seconds after bursts
        for model in cheap:
            msg = self._call(worker, model, messages, tools, final, errors)
            if msg is not None:
                if worker.last_model != model:
                    self.events.emit(worker.id, "warning",
                                     f"{worker.name}: {worker.model} and the fallbacks all failed (likely limits). "
                                     f"Using the cheap backup {model} for now; expect weaker work.",
                                     {"model": model})
                worker.last_model = model
                return msg
        raise RuntimeError("every model failed: " + " | ".join(errors[-4:]))

    def classify(self, task: str) -> str:
        """Judge a task's difficulty with the classifier model; "normal" when unsure."""
        prompt = ("Rate how hard this software task is for an AI coding agent. easy: small and mechanical, "
                  "one place, clear fix. normal: a typical feature or bug across a few files. hard: "
                  "architecture, tricky debugging, concurrency, security, a large refactor, or unclear "
                  "requirements. Answer with one word: easy, normal or hard.\n\nTask:\n" + task[:4000])
        try:
            with self.active_gate:
                data = self.llm._post({"model": self.cfg.classifier_model, "stream": False,
                                       "messages": [{"role": "user", "content": prompt}]})
            m = re.search(r"\b(easy|normal|hard)\b", (data["choices"][0]["message"].get("content") or "").lower())
            return m.group(1) if m else "normal"
        except Exception:
            return "normal"

    def pick_model(self, task: str, hirer: Worker | None, model: str | None, difficulty: str | None,
                   justification: str | None) -> tuple[str, str]:
        """(model, note). A paid model named by an agent needs a justification and the user's approval;
        the user naming one in the UI is the approval."""
        cfg = self.cfg
        if model:
            if model in cfg.paid_models:
                if hirer is None:
                    return model, f"paid model {model}, chosen by the user"
                if len((justification or "").strip()) < 30:
                    raise TeamError("a paid model needs a justification (at least 30 characters): why the "
                                    "free tiers are not enough for this task")
                title = f"{hirer.name} wants paid model {model}"
                detail = f"Why:\n{justification.strip()}\n\nFor this task:\n{task}"
                if self.request_approval(hirer, "paid_model", title, detail):
                    return model, f"paid model {model}, approved by the user"
                return cfg.tiers["hard"], f"the user declined {model}; using the hard tier, {cfg.tiers['hard']}"
            if model not in cfg.models:
                raise TeamError(f"model is one of: {', '.join(cfg.models)}"
                                + (f"; paid, with approval: {', '.join(cfg.paid_models)}" if cfg.paid_models else ""))
            return model, f"model {model}, chosen by name"
        if difficulty not in DIFFICULTIES:
            difficulty = self.classify(task)
            return cfg.tiers[difficulty], f"judged {difficulty} -> {cfg.tiers[difficulty]}"
        return cfg.tiers[difficulty], f"{difficulty} -> {cfg.tiers[difficulty]}"

    # --- auto mode -------------------------------------------------------------------------------

    @property
    def auto_mode(self) -> bool:
        return self._auto_mode

    def _auto_approves(self, public: dict) -> bool:
        if not self._auto_mode:
            return False
        if public["kind"] == "paid_model":
            return self.cfg.auto_mode_paid
        return not public.get("risky")

    def set_auto_mode(self, on: bool) -> int:
        """Switch auto mode for every worker; turning it on also approves what is already waiting
        (except risky commands and, unless KANTOR_AUTO_MODE_PAID, paid models). Returns how many."""
        self._auto_mode = bool(on)
        self.db.q("insert or replace into settings(key, value) values ('auto_mode', ?)", ("1" if on else "0",))
        approved = 0
        for aid, entry in list(self.approvals.items()):
            if self._auto_approves(entry["public"]):
                entry["approved"] = True
                entry["event"].set()
                approved += 1
        self.events.emit(None, "settings", f"auto mode {'on' if on else 'off'}",
                         {"auto_mode": self._auto_mode, "approved": approved})
        return approved

    def request_approval(self, worker: Worker, kind: str, title: str, detail: str, risky: str | None = None) -> bool:
        aid = uuid.uuid4().hex[:10]
        decided = threading.Event()
        public = {"id": aid, "worker": worker.id, "worker_name": worker.name, "kind": kind,
                  "title": clip(title, 2000), "detail": clip(detail, EVENT_TEXT_LIMIT), "ts": time.time(),
                  "risky": risky}
        entry = {"public": public, "event": decided, "approved": False}
        self.approvals[aid] = entry  # listed before the check, so a switch flipped meanwhile still sees it
        if self._auto_approves(public):
            self.approvals.pop(aid, None)
            self.events.emit(worker.id, "approval", title, {"approval": public, "state": "auto"})
            return True
        before = worker.status
        worker.set_status("waiting_approval")
        self.events.emit(worker.id, "approval", title, {"approval": public, "state": "pending"})
        while not decided.wait(1):
            if worker.cancel.is_set() or worker.gone:
                break
        self.approvals.pop(aid, None)
        approved = entry["approved"] and decided.is_set()
        self.events.emit(worker.id, "approval", title, {"approval": public,
                                                       "state": "approved" if approved else "denied"})
        worker.set_status(before if before != "waiting_approval" else "working")
        return approved

    def decide(self, aid: str, approve: bool) -> None:
        entry = self.approvals.get(aid)
        if not entry:
            raise TeamError("that approval is no longer pending")
        entry["approved"] = approve
        entry["event"].set()

    # --- hiring and dismissing --------------------------------------------------------------------

    def _project_dir(self, project: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", project or "") or project.startswith("."):
            raise TeamError("project is a folder name in the projects directory")
        path = (self.cfg.projects_dir / project).resolve()
        if not path.is_dir():
            raise TeamError(f"no project called {project!r} (there is: {', '.join(self.projects()) or 'none'})")
        return path

    def _git(self, cwd: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})

    @staticmethod
    def _exclude(repo: Path, *entries: str) -> None:
        exclude = repo / ".git" / "info" / "exclude"
        if exclude.parent.is_dir():
            have = (exclude.read_text() if exclude.exists() else "").split()
            missing = [e for e in entries if e not in have]
            if missing:
                with exclude.open("a") as f:
                    f.write("\n" + "\n".join(missing) + "\n")

    def _make_worktree(self, project_dir: Path, base_cwd: Path, name: str,
                       only: list[str] | None = None) -> tuple[str, str] | None:
        """A workspace for a hired worker: a worktree of each of the project's repos on one new branch,
        cut from the hirer's commits, laid out like the project (nested repos inside). None outside git."""
        inner = kb.nested(project_dir)
        if only:
            unknown = [r for r in only if r not in inner]
            if unknown:
                raise TeamError(f"repos are some of: {', '.join(inner)} (unknown: {', '.join(unknown)})")
            inner = [r for r in inner if r in only]
        # (repo, the hirer's copy of it, path inside the workspace)
        pairs = ([(project_dir, base_cwd, "")] if (project_dir / ".git").exists() else []) + \
                [(project_dir / r, base_cwd / r, r) for r in inner]
        pairs = [p for p in pairs if self._git(p[1], "rev-parse", "--verify", "HEAD").returncode == 0]
        if not pairs:
            return None
        branch, n = f"kantor/{name.lower()}", 1
        while any(self._git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").returncode == 0
                  for repo, _, _ in pairs):
            n += 1
            branch = f"kantor/{name.lower()}-{n}"
        workspace = project_dir / ".kantor" / "worktrees" / branch.split("/", 1)[1]
        if (project_dir / ".git").exists():
            self._exclude(project_dir, ".kantor/", *(f"{r}/" for r in kb.nested(project_dir)))
        else:
            workspace.mkdir(parents=True, exist_ok=True)
        for repo, base_repo, rel in pairs:
            base = self._git(base_repo, "rev-parse", "HEAD").stdout.strip()
            res = self._git(repo, "worktree", "add", "-b", branch, str(workspace / rel if rel else workspace), base)
            if res.returncode != 0:
                raise TeamError(f"could not create a worktree of {rel or project_dir.name}: {clip(res.stderr, 300)}")
        return str(workspace), branch

    def _remove_workspace(self, w: Worker) -> str:
        """Remove a worker's worktrees (nested repos first); keep everything if any has uncommitted work."""
        project_dir = self.cfg.projects_dir / w.project
        ws = Path(w.cwd)
        trees = [(project_dir / r, ws / r) for r in kb.nested(project_dir) if (ws / r / ".git").exists()]
        if (project_dir / ".git").exists() and (ws / ".git").is_file():
            trees.append((project_dir, ws))
        dirty = [str(t.relative_to(ws)) if t != ws else "." for _, t in trees
                 if self._git(t, "status", "--porcelain").stdout.strip()]
        if dirty:
            return f" (worktree kept: uncommitted changes in {', '.join(dirty)})"
        for repo, tree in trees:
            res = self._git(repo, "worktree", "remove", str(tree))
            if res.returncode != 0:
                return f" (worktree partly kept: {clip(res.stderr, 200)})"
        if ws.is_dir() and not any(ws.iterdir()):
            ws.rmdir()
        return " (worktree removed, branch kept)"

    def hire(self, project: str, task: str, parent: Worker | None = None, name: str | None = None,
             model: str | None = None, difficulty: str | None = None,
             justification: str | None = None, repos: list[str] | None = None) -> Worker:
        task = (task or "").strip()
        if not task:
            raise TeamError("give the worker its task")
        if len(self.present()) >= self.cfg.max_workers:  # checked again below; this avoids a pointless approval
            raise TeamError(f"the office is full ({self.cfg.max_workers} workers, KANTOR_MAX_WORKERS); "
                            "send someone home first")
        # Before taking the lock: this may wait for the user to approve a paid model.
        model, model_note = self.pick_model(task, parent, model, difficulty, justification)
        with self.lock:
            if len(self.present()) >= self.cfg.max_workers:
                raise TeamError(f"the office is full ({self.cfg.max_workers} workers, KANTOR_MAX_WORKERS); "
                                "send someone home first")
            depth = parent.depth + 1 if parent else 0
            if depth > self.cfg.max_depth:
                raise TeamError(f"hiring depth limit reached (KANTOR_MAX_HIRE_DEPTH={self.cfg.max_depth})")
            taken = {w.name.lower() for w in self.present()}
            if name:
                name = re.sub(r"[^A-Za-z0-9_-]", "", name)[:24]
                if not name or name.lower() in taken:
                    raise TeamError(f"name {name!r} is empty or taken")
            else:
                name = next((n for n in NAMES if n.lower() not in taken), None)
                k = 2
                while name is None:
                    name = next((f"{n}{k}" for n in NAMES if f"{n}{k}".lower() not in taken), None)
                    k += 1
            project_dir = self._project_dir(parent.project if parent else project)
            cwd, branch = str(project_dir), None
            if parent:  # agent-hired workers get their own worktree
                tree = self._make_worktree(project_dir, Path(parent.cwd), name, repos)
                if tree:
                    cwd, branch = tree
            w = Worker(self, {"id": uuid.uuid4().hex[:10], "name": name, "project": project_dir.name,
                              "cwd": cwd, "branch": branch, "parent": parent.id if parent else None,
                              "depth": depth, "model": model, "status": "idle",
                              "task": task, "created": time.time()})
            self.workers[w.id] = w
            w.save()
            w.start()
        w.model_note = model_note
        self.events.emit(w.id, "status", "hired", w.row())
        self.events.emit(w.id, "notice", f"Model: {model_note}")
        w.send(parent.name if parent else "user", task)
        return w

    def send_home(self, w: Worker, delete_worktree: bool = False) -> str:
        w.gone = True
        w.stop()
        with w.cond:
            w.cond.notify()
        note = self._remove_workspace(w) if delete_worktree and w.branch else ""
        w.status = "home"
        w.save()
        self.events.emit(w.id, "status", "home", w.row())
        with self.changed:
            self.changed.notify_all()
        return f"{w.name} went home{note}"

    def add_project(self, name: str, git_url: str | None) -> str:
        """An empty git project, one cloned repo, or (several URLs, one per line) a multi-repo project
        with each repo cloned into a folder named after it."""
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name or "") or name.startswith("."):
            raise TeamError("name: letters, digits, dot, dash or underscore")
        path = self.cfg.projects_dir / name
        if path.exists():
            raise TeamError(f"{name} already exists")
        urls = [u.strip() for u in (git_url or "").splitlines() if u.strip()]
        for u in urls:
            if not re.fullmatch(r"(https://|git@)[\w.@:/~+-]+", u):
                raise TeamError(f"not an https:// or git@ address: {u}")
        if len(urls) == 1:
            res = self._git(self.cfg.projects_dir, "clone", "--", urls[0], name, timeout=600)
            if res.returncode != 0:
                raise TeamError(f"clone failed: {clip(res.stderr, 500)}")
        elif urls:
            folders = [re.sub(r"\.git$", "", u.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]) for u in urls]
            if len(set(folders)) != len(folders):
                raise TeamError("two URLs end in the same repo name")
            path.mkdir()
            for u, folder in zip(urls, folders):
                res = self._git(path, "clone", "--", u, folder, timeout=900)
                if res.returncode != 0:
                    raise TeamError(f"clone of {u} failed: {clip(res.stderr, 500)} "
                                    f"(the repos cloned so far are in {name})")
        else:
            path.mkdir()
            self._git(path, "init", "-q")
            (path / "README.md").write_text(f"# {name}\n")
            self._git(path, "add", "README.md")
            self._git(path, "commit", "-qm", "Start project")
        self.events.emit(None, "project", name)
        self.setup_project(name)  # KANTOR.md templates, then the code graph and the note taker
        return name

    # --- team tools (called by workers) ---------------------------------------------------------

    def tool_list_workers(self, me: Worker) -> str:
        rows = []
        for w in self.present():
            r = w.row()
            rows.append(f"- {r['name']}{' (you)' if w is me else ''}: {r['status']}, model {r['model']}, "
                        f"hired by {r['parent'] or 'the user'}, branch {r['branch'] or '(main checkout)'}\n"
                        f"  task: {clip(r['task'], 200)}\n  last report: {clip(r['report'], 300) or '-'}")
        return "\n".join(rows)

    def tool_hire_worker(self, me: Worker, task: str, difficulty: str | None = None, model: str | None = None,
                         justification: str | None = None, name: str | None = None,
                         repos: list | None = None) -> str:
        if repos is not None and not (isinstance(repos, list) and all(isinstance(r, str) for r in repos)):
            raise TeamError("repos is a list of repo folder names")
        w = self.hire(me.project, task, parent=me, name=name, model=model, difficulty=difficulty,
                      justification=justification, repos=repos or None)
        return (f"Hired {w.name} on {w.model} ({w.model_note}), branch {w.branch or 'shared checkout'}. "
                "It is working now; call wait_for_workers to get its report.")

    def tool_switch_model(self, me: Worker, model: str, justification: str | None = None) -> str:
        if model == me.model:
            return f"You already run on {model}."
        new, note = self.pick_model(me.task, me, model, None, justification)
        if new != model:
            return f"Not switched: {note}. You stay on {me.model}."
        me.model = new
        me.save()
        self.events.emit(me.id, "model", f"{me.name} now runs on {new} ({note})", me.row())
        return f"Switched to {new}; it takes effect from your next step."

    def tool_tell_worker(self, me: Worker, worker: str, message: str) -> str:
        target = self.find(worker)
        if target is me:
            raise TeamError("you cannot message yourself")
        target.send(me.name, message)
        return f"Told {target.name}."

    def tool_wait_for_workers(self, me: Worker, workers: list, timeout_minutes: int = 30) -> str:
        if not isinstance(workers, list) or not workers:
            raise TeamError("workers is a list of names")
        targets = [self.find(str(k)) for k in workers]
        if me in targets:
            raise TeamError("you cannot wait for yourself")
        deadline = time.time() + max(1, min(int(timeout_minutes or 30), 240)) * 60
        me.set_status("waiting_team")
        with self.changed:
            while not me.cancel.is_set() and time.time() < deadline:
                if not any(t.busy() for t in targets):
                    break
                self.changed.wait(2)
        me.set_status("working")
        parts = []
        for t in targets:
            state = "STILL WORKING (timed out waiting)" if t.busy() else t.status
            parts.append(f"## {t.name} ({state}, branch {t.branch or 'shared checkout'})\n{t.report or '(no report)'}")
        return "\n\n".join(parts)

    def tool_send_home(self, me: Worker, workers: list, delete_worktree: bool = False) -> str:
        if not isinstance(workers, list) or not workers:
            raise TeamError("workers is a list of names")
        out = []
        for key in workers:
            w = self.find(str(key))
            if w.parent != me.id:
                out.append(f"{w.name}: not sent home (you can only send home workers you hired)")
            else:
                out.append(self.send_home(w, bool(delete_worktree)))
        return "\n".join(out)
