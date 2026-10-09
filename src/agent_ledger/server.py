from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent_ledger.config import apply_mandate, load_mandate_config
from agent_ledger.ledger import Ledger
from agent_ledger.models import Job, PolicyError, Reputation
from agent_ledger.store import JsonStore


class BadRequest(Exception):
    pass


def job_json(job: Job) -> dict:
    return {
        "job_id": job.job_id,
        "buyer_id": job.buyer_id,
        "payee_id": job.payee_id,
        "amount_cents": job.amount_cents,
        "terms": job.terms,
        "state": job.state.value,
        "approved": job.approved,
        "disputed": job.disputed,
        "payee_cents": job.payee_cents,
        "refund_cents": job.refund_cents,
        "day": job.day,
        "listing_id": job.listing_id,
        "note": job.note,
    }


def reputation_json(rep: Reputation) -> dict:
    return {
        "payee_id": rep.payee_id,
        "settled_jobs": rep.settled_jobs,
        "released": rep.released,
        "refunded": rep.refunded,
        "resolved": rep.resolved,
        "disputed": rep.disputed,
        "volume_cents": rep.volume_cents,
        "release_rate": rep.release_rate,
        "dispute_rate": rep.dispute_rate,
    }


def open_service(ledger_path: Path, config_path: Path) -> tuple[Ledger, JsonStore]:
    store = JsonStore(ledger_path)
    ledger = store.load()
    if config_path.is_file():
        incoming = load_mandate_config(config_path)
        if apply_mandate(ledger, incoming):
            store.save(ledger)
    return ledger, store


class LedgerHandler(BaseHTTPRequestHandler):
    ledger: Ledger
    store: JsonStore
    lock = threading.Lock()

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if path == "/reputation":
            payee_id = _one(query, "payee_id")
            if payee_id is None:
                self._send(400, {"error": "missing payee_id"})
                return
            self._call(lambda: reputation_json(self.ledger.reputation(payee_id)), mutate=False)
            return
        if path == "/job":
            job_id = _one(query, "job_id")
            if job_id is None:
                self._send(400, {"error": "missing job_id"})
                return
            self._call(lambda: job_json(self.ledger._job(job_id)), mutate=False)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            body = self._body()
        except BadRequest as exc:
            self._send(400, {"error": str(exc)})
            return
        routes = {
            "/hire": self._hire,
            "/approve": self._approve,
            "/fund": self._fund,
            "/deliver": self._deliver,
            "/release": self._release,
            "/dispute": self._dispute,
            "/resolve": self._resolve,
        }
        handler = routes.get(path)
        if handler is None:
            self._send(404, {"error": "not found"})
            return
        try:
            self._call(lambda: handler(body), mutate=True)
        except BadRequest as exc:
            self._send(400, {"error": str(exc)})

    def _hire(self, body: dict) -> dict:
        _need(body, "buyer_id", "payee_id", "amount_cents", "terms", "day")
        listing_id = body.get("listing_id")
        job = self.ledger.hire(
            body["buyer_id"],
            body["payee_id"],
            body["amount_cents"],
            body["terms"],
            body["day"],
            listing_id=listing_id if listing_id else None,
        )
        return job_json(job)

    def _approve(self, body: dict) -> dict:
        _need(body, "principal_id", "job_id")
        return job_json(self.ledger.approve(body["principal_id"], body["job_id"]))

    def _fund(self, body: dict) -> dict:
        _need(body, "job_id")
        return job_json(self.ledger.fund(body["job_id"]))

    def _deliver(self, body: dict) -> dict:
        _need(body, "payee_id", "job_id")
        note = body.get("note", "")
        if not isinstance(note, str):
            raise BadRequest("note must be a string")
        return job_json(self.ledger.deliver(body["payee_id"], body["job_id"], note))

    def _release(self, body: dict) -> dict:
        _need(body, "principal_id", "job_id")
        return job_json(self.ledger.release(body["principal_id"], body["job_id"]))

    def _dispute(self, body: dict) -> dict:
        _need(body, "principal_id", "job_id", "note")
        if not isinstance(body["note"], str):
            raise BadRequest("note must be a string")
        return job_json(self.ledger.dispute(body["principal_id"], body["job_id"], body["note"]))

    def _resolve(self, body: dict) -> dict:
        _need(body, "principal_id", "job_id", "payee_cents")
        return job_json(
            self.ledger.resolve(body["principal_id"], body["job_id"], body["payee_cents"])
        )

    def _call(self, fn, *, mutate: bool) -> None:
        # The response is sent only after a successful save, still inside the lock.
        with self.lock:
            try:
                result = fn()
            except PolicyError as exc:
                message = str(exc)
                status = 404 if message.startswith("unknown job") else 403
                self._send(status, {"error": message})
                return
            except BadRequest:
                raise
            except Exception:
                traceback.print_exc()
                self._send(500, {"error": "internal error"})
                return
            if mutate:
                self.store.save(self.ledger)
            self._send(200, result)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise BadRequest("invalid Content-Length") from exc
        raw = self.rfile.read(length) if length else b""
        if not raw:
            raise BadRequest("JSON body required")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BadRequest("invalid JSON") from exc
        if not isinstance(data, dict):
            raise BadRequest("JSON body must be an object")
        return data

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _need(body: dict, *names: str) -> None:
    missing = [name for name in names if name not in body]
    if missing:
        raise BadRequest(f"missing {', '.join(missing)}")


def _one(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values or not values[0]:
        return None
    return values[0]


def serve(ledger: Ledger, store: JsonStore, port: int) -> None:
    LedgerHandler.ledger = ledger
    LedgerHandler.store = store
    server = ThreadingHTTPServer(("127.0.0.1", port), LedgerHandler)
    print(f"agent-ledger listening on http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    raw_port = os.environ.get("PORT", "8787")
    try:
        port = int(raw_port)
    except ValueError:
        print(f"PORT must be an integer, got {raw_port}", file=sys.stderr)
        raise SystemExit(2) from None
    ledger_path = Path(os.environ.get("LEDGER_PATH", "./ledger.json"))
    config_path = Path(os.environ.get("LEDGER_CONFIG", "./config.json"))
    try:
        ledger, store = open_service(ledger_path, config_path)
    except (OSError, PolicyError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    serve(ledger, store, port)


if __name__ == "__main__":
    main()
