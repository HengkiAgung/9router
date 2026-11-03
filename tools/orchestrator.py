#!/usr/bin/env python3
"""Orkestra: multi-model coding agent on top of 9router.

A cheap router model splits the request into steps (plan / code / review /
write) and picks a model for each; every step then runs as a tool-using agent
inside the workspace (the current directory by default). A reviewer can send
work back to the coder. Stdlib only, so this single file can be copied to any
machine - your laptop or a server you reach over SSH - and run next to the code.

Usage:
    python orchestrator.py "add input validation to app.py"
    python orchestrator.py                      # interactive prompt loop
    python orchestrator.py --auto-edit "..."    # apply file edits without asking
    python orchestrator.py --root ~/myapp "..." # work on another directory

Config, read from environment variables, then from ~/.config/orkestra/.env,
then (when this file lives in a WAT repo's tools/) from that repo's .env:
    NINEROUTER_API_KEY   required, an API key from the 9router dashboard
    ORCH_BASE_URL        default https://airouter.hengkiagung.my.id/v1
    ORCH_MODELS          optional path to a JSON file {"coder": ["model", ...], ...}
                         overriding the model lists in ROLES
"""
from __future__ import annotations

import argparse
import difflib
import fnmatch
import json
import os
import platform
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://airouter.hengkiagung.my.id/v1"
CONFIG_KEYS = ("NINEROUTER_API_KEY", "ORCH_BASE_URL", "ORCH_MODELS")

# Models per role are tried in order; the first one that answers is used.
ROLES = {
    "router": {
        "models": ["ag/gemini-3.8-flash", "kenari/glm-4-7-flash:free"],
        "desc": "splits the request into steps",
    },
    "planner": {
        "models": ["ag/gemini-3.1-pro-high", "ag/claude-sonnet-4-6"],
        "desc": "reads the code and writes a concrete implementation plan; makes no edits",
    },
    "coder": {
        "models": ["ag/claude-sonnet-4-6", "ag/gemini-3.1-pro-high"],
        "desc": "edits files and runs commands to implement changes",
    },
    "reviewer": {
        "models": ["ag/gemini-3.8-flash", "ag/claude-sonnet-4-6"],
        "desc": "checks the coder's changes for bugs, may run tests; makes no edits",
    },
    "writer": {
        "models": ["kenari/qwen3-8-27b:free", "ag/gemini-3.8-flash"],
        "desc": "answers questions, explains code, writes docs or other text",
    },
}

READ = ["list_files", "read_file", "search"]
WRITE = ["write_file", "edit_file"]
RUN = ["run_command"]
ROLE_TOOLS = {
    "planner": READ,
    "coder": READ + WRITE + RUN,
    "reviewer": READ + RUN,
    "writer": READ + WRITE,
}

ROLE_PROMPTS = {
    "planner": "Produce a concrete, numbered implementation plan: which files to change, "
    "what to change in each, and how to verify it. Do not write the full code.",
    "coder": "Implement the task. Read a file before editing it. Use edit_file for changes "
    "to existing files and write_file for new files. If the project has tests or a quick "
    "way to check the change, run it. Finish with a short summary of what you changed.",
    "reviewer": "Review the changes described in the previous steps: read the changed files "
    "and run the tests if there are any. Report only real bugs or unmet requirements, each "
    "with the file and the fix. End with exactly one line: VERDICT: OK or VERDICT: FIX",
    "writer": "Answer the question or write the requested text. Be clear and concise.",
}

ROUTER_PROMPT = """You route a developer's request to a team of agents.
Roles and the models each can use (first is the default):
{roles}

Return ONLY a JSON object, no prose:
{{"steps": [{{"role": "planner|coder|reviewer|writer", "task": "what this step must do", "model": "optional, one of that role's models"}}]}}

Rules:
- Questions, explanations, docs, other text: one writer step.
- A small, clear code change: coder, then reviewer.
- A larger feature or an unclear change: planner, coder, reviewer.
- At most 5 steps. Choose a non-default model only when the step needs it."""

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".tmp"}
MAX_READ_LINES = 400
MAX_TOOL_RESULT = 12000
MAX_CONTEXT_PER_STEP = 8000
MAX_FIX_ROUNDS = 2


def load_config() -> dict:
    """Only the keys Orkestra needs; other secrets in .env never reach the agent's shell."""
    cfg = {k: os.environ[k] for k in CONFIG_KEYS if os.environ.get(k)}
    files = [Path.home() / ".config" / "orkestra" / ".env"]
    here = Path(__file__).resolve().parent
    if here.name == "tools":
        files.append(here.parent / ".env")
    for path in files:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, val = line.strip().partition("=")
            if sep and key.strip() in CONFIG_KEYS:
                cfg.setdefault(key.strip(), val.strip().strip("\"'"))
    return cfg


class LLM:
    def __init__(self, base_url: str, api_key: str):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key

    def chat(self, models: list[str], messages: list, tools: list | None = None) -> tuple[str, dict]:
        """Try each model in turn; return (model used, assistant message)."""
        errors = []
        for model in models:
            body = {"model": model, "messages": messages, "stream": False}
            if tools:
                body["tools"] = tools
            try:
                return model, self._post(body)["choices"][0]["message"]
            except Exception as e:  # network error, HTTP error, or malformed reply
                errors.append(f"{model}: {e}")
                print(f"    ! {model} failed ({str(e)[:150]}), trying next model")
        raise RuntimeError("all models failed:\n  " + "\n  ".join(errors))

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            # Cloudflare in front of the router blocks the default "Python-urllib" agent (error 1010).
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                     "User-Agent": "orkestra/0.1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from None


class Workspace:
    """File and shell tools, confined to one directory, with user approval for changes."""

    def __init__(self, root: Path, auto_edit: bool, yes: bool):
        self.root = root.resolve()
        self.auto_edit = auto_edit or yes
        self.yes = yes

    def call(self, name: str, args: dict, allowed: list[str]) -> str:
        if name not in allowed:
            return f"ERROR: tool {name} is not available to this role"
        try:
            return getattr(self, name)(**args)
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    def _path(self, path: str) -> Path:
        full = (self.root / path).resolve()
        if full != self.root and self.root not in full.parents:
            raise ValueError(f"path is outside the workspace: {path}")
        return full

    def _ask(self, question: str) -> bool:
        if self.yes:
            return True
        if not sys.stdin.isatty():
            print("    (no terminal to confirm; denied)")
            return False
        return input(f"    {question} [y/N] ").strip().lower() in ("y", "yes")

    def _walk(self, base: Path):
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                yield Path(dirpath) / name

    def list_files(self, path: str = ".", max_depth: int = 3) -> str:
        base = self._path(path)
        out = []
        for f in self._walk(base):
            if len(f.relative_to(base).parts) > max_depth:
                continue
            out.append(str(f.relative_to(self.root)))
            if len(out) >= 500:
                out.append("... (truncated, list a subdirectory)")
                break
        return "\n".join(out) or "(no files)"

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        lines = self._path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        end = min(end_line or len(lines), len(lines), start + MAX_READ_LINES - 1)
        body = "\n".join(f"{i}\t{lines[i - 1]}" for i in range(start, end + 1))
        if end < len(lines):
            body += f"\n... ({len(lines)} lines total; continue with start_line={end + 1})"
        return body or "(empty file)"

    def search(self, pattern: str, path: str = ".", glob: str | None = None) -> str:
        rx = re.compile(pattern)
        hits = []
        for f in self._walk(self._path(path)):
            if glob and not fnmatch.fnmatch(f.name, glob):
                continue
            try:
                if f.stat().st_size > 1_000_000:
                    continue
                with f.open("rb") as fh:
                    if b"\0" in fh.read(1024):
                        continue  # binary
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f.relative_to(self.root)}:{n}: {line.strip()[:200]}")
                    if len(hits) >= 200:
                        return "\n".join(hits) + "\n... (truncated)"
        return "\n".join(hits) or "(no matches)"

    def write_file(self, path: str, content: str) -> str:
        full = self._path(path)
        old = full.read_text(encoding="utf-8", errors="replace") if full.exists() else ""
        if not self._confirm_edit(path, old, content):
            return "DENIED: the user rejected this change"
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return f"wrote {path}"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        full = self._path(path)
        text = full.read_text(encoding="utf-8", errors="replace")
        count = text.count(old_text)
        if count != 1:
            return (f"ERROR: old_text occurs {count} times in {path}; it must match exactly once. "
                    "Read the file and include more surrounding lines.")
        new = text.replace(old_text, new_text, 1)
        if not self._confirm_edit(path, text, new):
            return "DENIED: the user rejected this change"
        full.write_text(new, encoding="utf-8")
        return f"edited {path}"

    def _confirm_edit(self, path: str, old: str, new: str) -> bool:
        diff = "".join(difflib.unified_diff(
            old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}"))
        print(diff if len(diff) < 6000 else diff[:6000] + "\n... (diff truncated)")
        return self.auto_edit or self._ask(f"Apply this change to {path}?")

    def run_command(self, command: str, timeout: int = 120) -> str:
        print(f"    $ {command}")
        if not self._ask("Run this command?"):
            return "DENIED: the user rejected this command"
        try:
            proc = subprocess.run(command, shell=True, cwd=self.root, capture_output=True,
                                  text=True, errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return f"ERROR: timed out after {timeout}s"
        out = proc.stdout + proc.stderr
        if len(out) > MAX_TOOL_RESULT:
            half = MAX_TOOL_RESULT // 2
            out = out[:half] + "\n... (output truncated) ...\n" + out[-half:]
        return f"exit code {proc.returncode}\n{out}"


def tool_spec(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


STR, INT = {"type": "string"}, {"type": "integer"}
TOOL_SPECS = {
    "list_files": tool_spec("list_files", "List files under a workspace directory.",
                            {"path": STR, "max_depth": INT}, []),
    "read_file": tool_spec("read_file", "Read a text file, with line numbers.",
                           {"path": STR, "start_line": INT, "end_line": INT}, ["path"]),
    "search": tool_spec("search", "Regex search across workspace files; returns file:line: text.",
                        {"pattern": STR, "path": STR,
                         "glob": {"type": "string", "description": "file name filter, e.g. *.py"}},
                        ["pattern"]),
    "write_file": tool_spec("write_file", "Create a file, or overwrite it with the full new content.",
                            {"path": STR, "content": STR}, ["path", "content"]),
    "edit_file": tool_spec("edit_file", "Replace one exact, unique occurrence of old_text with new_text.",
                           {"path": STR, "old_text": STR, "new_text": STR},
                           ["path", "old_text", "new_text"]),
    "run_command": tool_spec("run_command",
                             "Run a shell command in the workspace root (tests, builds, git status).",
                             {"command": STR, "timeout": INT}, ["command"]),
}


class Orchestra:
    def __init__(self, llm: LLM, ws: Workspace, roles: dict, max_steps: int):
        self.llm, self.ws, self.roles, self.max_steps = llm, ws, roles, max_steps

    def route(self, request: str) -> list[dict]:
        roles = "\n".join(f"- {name}: {r['desc']}. Models: {', '.join(r['models'])}"
                          for name, r in self.roles.items() if name != "router")
        messages = [{"role": "system", "content": ROUTER_PROMPT.format(roles=roles)},
                    {"role": "user", "content": request}]
        try:
            _, msg = self.llm.chat(self.roles["router"]["models"], messages)
            match = re.search(r"\{.*\}", msg.get("content") or "", re.DOTALL)
            steps = [s for s in json.loads(match.group(0))["steps"]
                     if s.get("role") in ROLE_TOOLS and s.get("task")][:5]
            if steps:
                return steps
        except Exception as e:
            print(f"! router failed ({str(e)[:150]}); falling back to a single coder step")
        return [{"role": "coder", "task": request}]

    def agent(self, role: str, task: str, request: str, context: list[tuple[str, str]],
              preferred: str | None = None) -> str:
        models = list(self.roles[role]["models"])
        if preferred in models:
            models.remove(preferred)
            models.insert(0, preferred)
        prior = "\n\n".join(f"## Output of {r}\n{o[:MAX_CONTEXT_PER_STEP]}" for r, o in context)
        user = f"Original request:\n{request}\n\nYour task as {role}:\n{task}"
        if prior:
            user += f"\n\nPrevious steps:\n{prior}"
        system = (f"You are the {role} in a multi-agent coding team. Workspace root: {self.ws.root} "
                  f"({platform.system()}). Paths are relative to the workspace root. Look at the real "
                  "code with your tools instead of guessing. Reply in the language of the request.\n\n"
                  + ROLE_PROMPTS[role])
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        tools = [TOOL_SPECS[t] for t in ROLE_TOOLS[role]]
        nudged = False
        for _ in range(self.max_steps):
            used, msg = self.llm.chat(models, messages, tools)
            if used != models[0]:  # stick with the model that answered
                models.remove(used)
                models.insert(0, used)
            calls = msg.get("tool_calls") or []
            if not calls:
                text = (msg.get("content") or "").strip()
                if text:
                    return text
                # Some models (seen with gemini flash) stop with neither text nor a tool call.
                # Ask once for the final answer, then hand the step to the next model.
                if not nudged:
                    nudged = True
                    messages.append({"role": "user", "content": "Write your final answer now, as text."})
                elif len(models) > 1:
                    print(f"    ! {used} returned no text, switching to {models[1]}")
                    models.append(models.pop(0))
                    messages.pop()
                    nudged = False
                else:
                    return "(no text output)"
                continue
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args, result = {}, "ERROR: tool arguments were not valid JSON"
                else:
                    shown = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in args.items()
                                      if k not in ("content", "old_text", "new_text"))
                    print(f"    · {name}({shown})")
                    result = self.ws.call(name, args, ROLE_TOOLS[role])
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": result[:MAX_TOOL_RESULT]})
        return "(stopped: reached the per-step tool limit)"

    def run(self, request: str) -> str:
        steps = self.route(request)
        print("Plan: " + " -> ".join(f"{s['role']}" + (f" [{s['model']}]" if s.get("model") else "")
                                     for s in steps))
        context: list[tuple[str, str]] = []
        fixes = 0
        i = 0
        while i < len(steps):
            step = steps[i]
            role = step["role"]
            print(f"\n=== {i + 1}/{len(steps)} {role}: {step['task'][:120]}")
            out = self.agent(role, step["task"], request, context, step.get("model"))
            context.append((role, out))
            print(f"--- {role} output ---\n{out}")
            if role == "reviewer" and "VERDICT: FIX" in out.upper() and fixes < MAX_FIX_ROUNDS:
                fixes += 1
                steps[i + 1:i + 1] = [
                    {"role": "coder", "task": "Fix the issues the reviewer reported."},
                    {"role": "reviewer", "task": step["task"]},
                ]
            i += 1
        return context[-1][1] if context else ""


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Multi-model coding agent on top of 9router.")
    ap.add_argument("request", nargs="*", help="what to do; omit for an interactive loop")
    ap.add_argument("--root", default=".", help="workspace directory (default: current directory)")
    ap.add_argument("--auto-edit", action="store_true", help="apply file edits without asking")
    ap.add_argument("--yes", action="store_true", help="also run shell commands without asking")
    ap.add_argument("--max-steps", type=int, default=25, help="tool calls per agent step")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.get("NINEROUTER_API_KEY"):
        print("Missing NINEROUTER_API_KEY. Put it in ~/.config/orkestra/.env or the environment.")
        return 2
    roles = {name: dict(r) for name, r in ROLES.items()}
    if cfg.get("ORCH_MODELS"):
        for name, models in json.loads(Path(cfg["ORCH_MODELS"]).expanduser().read_text()).items():
            if name in roles:
                roles[name]["models"] = models

    orchestra = Orchestra(LLM(cfg.get("ORCH_BASE_URL") or DEFAULT_BASE_URL, cfg["NINEROUTER_API_KEY"]),
                          Workspace(Path(args.root), args.auto_edit, args.yes), roles, args.max_steps)
    print(f"Workspace: {orchestra.ws.root}")
    if args.request:
        try:
            orchestra.run(" ".join(args.request))
        except RuntimeError as e:
            print(f"! {e}")
            return 1
        return 0
    while True:
        try:
            request = input("\norkestra> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if request in ("", "exit", "quit"):
            return 0
        try:
            orchestra.run(request)
        except Exception as e:
            print(f"! {e}")


if __name__ == "__main__":
    sys.exit(main())
