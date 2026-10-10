from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from agent_ledger.store import JsonStore

ROOT = Path(__file__).resolve().parents[1]

AGENT_TOKEN = "agent-test-token"
PRINCIPAL_TOKEN = "principal-test-token"

EXAMPLE = {
    "agent_id": "buyer-bot",
    "principal_id": "joe",
    "max_per_job_cents": 2000,
    "max_daily_cents": 5000,
    "approval_above_cents": 1000,
    "allowed_payees": ["research-bot"],
    "frozen": False,
}


def write_config(path: Path, **overrides: object) -> None:
    payload = dict(EXAMPLE)
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def seed_balance(path: Path, cents: int = 10_000) -> None:
    store = JsonStore(path)
    ledger = store.load()
    ledger.deposit("buyer-bot", cents)
    store.save(ledger)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request_json(
    port: int,
    method: str,
    path: str,
    body: dict | None = None,
    token: str | None = AGENT_TOKEN,
):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
    )
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _wait_until_up(port: int, proc: subprocess.Popen, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/reputation?payee_id=ready"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode() if proc.stderr else ""
            raise RuntimeError(err or f"server exited {proc.returncode}")
        try:
            urllib.request.urlopen(url, timeout=0.2)
            return
        except urllib.error.HTTPError:
            return
        except Exception:
            time.sleep(0.05)
    if proc.poll() is None:
        proc.kill()
    err = proc.stderr.read().decode() if proc.stderr else ""
    raise RuntimeError(err or "server did not start")


@contextmanager
def running_server(directory: Path, ledger_path: Path, config_path: Path):
    port = free_port()
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["LEDGER_PATH"] = str(ledger_path)
    env["LEDGER_CONFIG"] = str(config_path)
    env["LEDGER_AGENT_TOKEN"] = AGENT_TOKEN
    env["LEDGER_PRINCIPAL_TOKEN"] = PRINCIPAL_TOKEN
    env["LEDGER_TODAY"] = "2026-10-08"
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_ledger.server"],
        cwd=directory,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_until_up(port, proc)
        yield port
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        else:
            proc.wait(timeout=3)
        if proc.stderr is not None:
            proc.stderr.read()
