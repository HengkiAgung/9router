#!/usr/bin/env python3
"""Kantor web server: the API and live event stream behind ui.html. Stdlib only.

Run:   python3 tools/kantor/server.py                 (reads the environment, see Config in engine.py)
       python3 tools/kantor/server.py hash-password   (reads a password on stdin, prints its hash)

Login is one shared password, kept only as a scrypt hash (KANTOR_PASSWORD_HASH; KANTOR_PASSWORD is
hashed at start for local use). Sessions are random tokens whose SHA-256 is all the database holds.
State-changing requests need the X-Kantor header, which a cross-site form can't send.
Listens on 127.0.0.1 only; put nginx with HTTPS in front.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import Config, Office, TeamError, clip  # noqa: E402

UI_FILE = Path(__file__).resolve().parent / "ui.html"
SESSION_DAYS = 7
COOKIE = "kantor_session"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt:{salt.hex()}:{digest.hex()}"  # no "$": systemd environment files may expand it


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split(":")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(got.hex(), digest)
    except ValueError:
        return False


class App:
    def __init__(self):
        self.cfg = Config()
        if not self.cfg.api_key:
            sys.exit("NINEROUTER_API_KEY is not set")
        self.password_hash = os.environ.get("KANTOR_PASSWORD_HASH") or (
            hash_password(os.environ["KANTOR_PASSWORD"]) if os.environ.get("KANTOR_PASSWORD") else "")
        if not self.password_hash:
            sys.exit("Set KANTOR_PASSWORD_HASH (or KANTOR_PASSWORD for local use)")
        self.cookie_secure = os.environ.get("KANTOR_COOKIE_SECURE", "true").lower() in ("1", "true", "yes")
        self.office = Office(self.cfg)
        self.login_attempts: dict[str, deque] = defaultdict(deque)
        self.attempts_lock = threading.Lock()

    def session_ok(self, token: str | None) -> bool:
        if not token:
            return False
        h = hashlib.sha256(token.encode()).hexdigest()
        rows = self.office.db.q("select expires from sessions where hash = ?", (h,))
        return bool(rows) and rows[0][0] > time.time()

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self.office.db.q("delete from sessions where expires < ?", (time.time(),))
        self.office.db.q("insert into sessions(hash, expires) values (?, ?)",
                         (hashlib.sha256(token.encode()).hexdigest(), time.time() + SESSION_DAYS * 86400))
        return token

    def end_session(self, token: str | None) -> None:
        if token:
            self.office.db.q("delete from sessions where hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))

    def allow_login_attempt(self, ip: str) -> bool:
        now = time.time()
        with self.attempts_lock:
            q = self.login_attempts[ip]
            while q and q[0] < now - 300:
                q.popleft()
            if len(q) >= 10:
                return False
            q.append(now)
            return True


APP: App


class Handler(BaseHTTPRequestHandler):
    server_version = "Kantor"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # only errors go to the journal
        pass

    # --- helpers -----------------------------------------------------------------------------------

    def client_ip(self) -> str:
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def token(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def send_json(self, status: int, body, headers: dict | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise TeamError("request too large")
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise TeamError("body is not JSON") from None
        if not isinstance(data, dict):
            raise TeamError("body is a JSON object")
        return data

    def cookie(self, value: str, max_age: int) -> str:
        return (f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"
                + ("; Secure" if APP.cookie_secure else ""))

    # --- routes ------------------------------------------------------------------------------------

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/healthz":
            return self.send_json(200, {"ok": True})
        if url.path in ("/", "/index.html"):
            data = UI_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                             "style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        if not APP.session_ok(self.token()):
            return self.send_json(401, {"error": "login required"})
        office = APP.office
        if url.path == "/api/state":
            return self.send_json(200, office.state())
        if url.path == "/api/events":
            return self.stream(int(self.headers.get("Last-Event-ID") or
                                   parse_qs(url.query).get("after", ["0"])[0] or 0))
        m = re.fullmatch(r"/api/workers/([0-9a-f]{10})/(log|diff)", url.path)
        if m and m.group(1) in office.workers:
            w = office.workers[m.group(1)]
            if m.group(2) == "log":
                before = parse_qs(url.query).get("before", [None])[0]
                return self.send_json(200, office.db.events_for(w.id, int(before) if before else None, 300))
            return self.send_json(200, {"diff": self.worker_diff(w)})
        self.send_json(404, {"error": "not found"})

    def do_HEAD(self):
        self.send_response(200 if urlparse(self.path).path in ("/", "/index.html", "/healthz") else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        url = urlparse(self.path)
        if self.headers.get("X-Kantor") != "1":
            return self.send_json(403, {"error": "missing X-Kantor header"})
        try:
            if url.path == "/api/login":
                return self.login()
            if url.path == "/api/logout":
                APP.end_session(self.token())
                return self.send_json(200, {"ok": True}, {"Set-Cookie": self.cookie("", 0)})
            if not APP.session_ok(self.token()):
                return self.send_json(401, {"error": "login required"})
            return self.send_json(200, self.action(url.path, self.body()))
        except TeamError as e:
            return self.send_json(400, {"error": str(e)})

    def login(self):
        if not APP.allow_login_attempt(self.client_ip()):
            return self.send_json(429, {"error": "too many attempts; wait 5 minutes"})
        if not check_password(str(self.body().get("password", "")), APP.password_hash):
            return self.send_json(401, {"error": "wrong password"})
        return self.send_json(200, {"ok": True}, {"Set-Cookie": self.cookie(APP.new_session(), SESSION_DAYS * 86400)})

    def action(self, path: str, b: dict) -> dict:
        office = APP.office
        if path == "/api/workers":
            w = office.hire(str(b.get("project", "")), str(b.get("task", "")), name=b.get("name") or None,
                            model=b.get("model") or None)
            return {"id": w.id, "name": w.name}
        if path == "/api/projects":
            return {"name": office.add_project(str(b.get("name", "")), (b.get("git_url") or "").strip() or None)}
        m = re.fullmatch(r"/api/approvals/([0-9a-f]{10})", path)
        if m:
            office.decide(m.group(1), bool(b.get("approve")))
            return {"ok": True}
        m = re.fullmatch(r"/api/workers/([0-9a-f]{10})/(message|stop|home)", path)
        if not m or m.group(1) not in office.workers or office.workers[m.group(1)].gone:
            raise TeamError("no such worker")
        w = office.workers[m.group(1)]
        if m.group(2) == "message":
            text = str(b.get("text", "")).strip()
            if not text:
                raise TeamError("write a message")
            w.send("user", text)
        elif m.group(2) == "stop":
            w.stop()
        else:
            return {"result": office.send_home(w, bool(b.get("delete_worktree")))}
        return {"ok": True}

    def worker_diff(self, w) -> str:
        cwd = Path(w.cwd)
        status = APP.office._git(cwd, "status", "--short").stdout
        out = f"$ git status --short\n{status or '(clean)'}\n"
        if w.branch:
            log = APP.office._git(cwd, "log", "--oneline", "-20", "HEAD", "--not", "--exclude=kantor/*",
                                  "--branches").stdout
            out += f"\n$ commits on {w.branch} not on other branches\n{log or '(none)'}\n"
        diff = APP.office._git(cwd, "diff", "HEAD").stdout
        return clip(out + f"\n$ git diff HEAD\n{diff or '(no uncommitted changes)'}", 200_000)

    def stream(self, after: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while True:
                events = APP.office.events.wait(after, 15)
                if not events:
                    self.wfile.write(b": ping\n\n")
                for ev in events:
                    after = ev["id"]
                    self.wfile.write(f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return


def main() -> int:
    if sys.argv[1:] == ["hash-password"]:
        print(hash_password(sys.stdin.readline().rstrip("\n")))
        return 0
    global APP
    APP = App()
    port = int(os.environ.get("KANTOR_PORT", 4610))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True

    def shutdown(*_):
        for w in APP.office.present():
            w.save()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    print(f"Kantor on http://127.0.0.1:{port} (projects: {APP.cfg.projects_dir}, data: {APP.cfg.data_dir})",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
