from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent_ledger.audit import DENIAL_RULES
from agent_ledger.config import apply_mandate, load_mandate_config
from agent_ledger.ledger import Ledger, _require_day
from agent_ledger.models import Job, PolicyError, Reputation
from agent_ledger.store import JsonStore, LedgerFileError

AGENT_ROUTES = {"/hire", "/fund", "/deliver"}
PRINCIPAL_ROUTES = {"/approve", "/release", "/dispute", "/resolve", "/freeze"}
READ_ROUTES = {"/reputation", "/job"}


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
        "principal_approved": job.principal_approved,
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


def open_service(ledger_path: Path, config_path: Path) -> tuple[Ledger, JsonStore, str]:
    store = JsonStore(ledger_path)
    ledger = store.load()
    agent_id = ""
    if config_path.is_file():
        incoming = load_mandate_config(config_path)
        agent_id = incoming.agent_id
        if apply_mandate(ledger, incoming):
            store.save(ledger)
    return ledger, store, agent_id


def current_day() -> str:
    """Accounting day for the door. LEDGER_TODAY pins it; otherwise the clock is UTC."""
    override = os.environ.get("LEDGER_TODAY", "").strip()
    if override:
        return _require_day(override)
    return datetime.now(timezone.utc).date().isoformat()


class LedgerHandler(BaseHTTPRequestHandler):
    ledger: Ledger
    store: JsonStore
    agent_id: str = ""
    agent_token: str = ""
    principal_token: str = ""
    lock = threading.Lock()

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if path in READ_ROUTES and not self._read_ok():
            self._send(403, {"error": "unauthorized"})
            return
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
            "/freeze": self._freeze,
        }
        handler = routes.get(path)
        if handler is None:
            self._send(404, {"error": "not found"})
            return
        if not self._route_ok(path):
            self._send(403, {"error": "unauthorized"})
            return
        try:
            self._call(lambda: handler(body), mutate=True, denial_path=path)
        except BadRequest as exc:
            self._send(400, {"error": str(exc)})

    def _hire(self, body: dict) -> dict:
        _need(body, "payee_id", "amount_cents", "terms")
        if not self.agent_id:
            raise PolicyError("no mandate for this agent")
        listing_id = body.get("listing_id")
        # buyer_id and day in the body are ignored. The token and the server clock decide them.
        job = self.ledger.hire(
            self.agent_id,
            body["payee_id"],
            body["amount_cents"],
            body["terms"],
            current_day(),
            listing_id=listing_id if listing_id else None,
        )
        return job_json(job)

    def _approve(self, body: dict) -> dict:
        _need(body, "job_id")
        return job_json(self.ledger.approve(self._principal_for(body["job_id"]), body["job_id"]))

    def _fund(self, body: dict) -> dict:
        _need(body, "job_id")
        job = self.ledger._job(body["job_id"])
        if job.buyer_id != self.agent_id:
            raise PolicyError("agent cannot fund this job")
        return job_json(self.ledger.fund(body["job_id"]))

    def _deliver(self, body: dict) -> dict:
        _need(body, "payee_id", "job_id")
        note = body.get("note", "")
        if not isinstance(note, str):
            raise BadRequest("note must be a string")
        return job_json(self.ledger.deliver(body["payee_id"], body["job_id"], note))

    def _release(self, body: dict) -> dict:
        _need(body, "job_id")
        return job_json(self.ledger.release(self._principal_for(body["job_id"]), body["job_id"]))

    def _dispute(self, body: dict) -> dict:
        _need(body, "job_id", "note")
        if not isinstance(body["note"], str):
            raise BadRequest("note must be a string")
        return job_json(
            self.ledger.dispute(self._principal_for(body["job_id"]), body["job_id"], body["note"])
        )

    def _resolve(self, body: dict) -> dict:
        _need(body, "job_id", "payee_cents")
        return job_json(
            self.ledger.resolve(
                self._principal_for(body["job_id"]), body["job_id"], body["payee_cents"]
            )
        )

    def _freeze(self, body: dict) -> dict:
        if "frozen" not in body or not isinstance(body["frozen"], bool):
            raise BadRequest("frozen must be a boolean")
        if not self.agent_id:
            raise PolicyError("no mandate for this agent")
        mandate = self.ledger.mandates.get(self.agent_id)
        if mandate is None:
            raise PolicyError("no mandate for this agent")
        updated = self.ledger.freeze(mandate.principal_id, self.agent_id, body["frozen"])
        return {"agent_id": updated.agent_id, "frozen": updated.frozen}

    def _principal_for(self, job_id: str) -> str:
        job = self.ledger._job(job_id)
        mandate = self.ledger.mandates.get(job.buyer_id)
        if mandate is None:
            raise PolicyError("principal does not own this mandate")
        return mandate.principal_id

    def _route_ok(self, path: str) -> bool:
        if path in AGENT_ROUTES:
            return _token_ok(self.headers.get("Authorization", ""), self.agent_token)
        if path in PRINCIPAL_ROUTES:
            return _token_ok(self.headers.get("Authorization", ""), self.principal_token)
        return True

    def _read_ok(self) -> bool:
        header = self.headers.get("Authorization", "")
        return _token_ok(header, self.agent_token) or _token_ok(header, self.principal_token)

    def _denial_actor(self, path: str) -> str:
        if path in AGENT_ROUTES or not self.agent_id:
            return self.agent_id
        mandate = self.ledger.mandates.get(self.agent_id)
        if mandate is None:
            return self.agent_id
        return mandate.principal_id

    def _call(self, fn, *, mutate: bool, denial_path: str = "") -> None:
        # The response is sent only after a successful save, still inside the lock.
        with self.lock:
            try:
                result = fn()
            except PolicyError as exc:
                message = str(exc)
                rule = _denial_rule(message) if mutate else None
                if rule is not None:
                    # The library raised before it changed balances. Keep the refusal on the chain.
                    self.ledger.record_denial(self._denial_actor(denial_path), message, rule)
                    self.store.save(self.ledger)
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


def _denial_rule(message: str) -> str | None:
    for rule in DENIAL_RULES:
        if rule in message:
            return rule
    return None


def _need(body: dict, *names: str) -> None:
    missing = [name for name in names if name not in body]
    if missing:
        raise BadRequest(f"missing {', '.join(missing)}")


def _one(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values or not values[0]:
        return None
    return values[0]


def _token_ok(header: str, expected: str) -> bool:
    presented = header[7:] if header.startswith("Bearer ") else ""
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


def _require_distinct_tokens(agent_token: str, principal_token: str) -> None:
    if not agent_token or not principal_token:
        raise SystemExit("LEDGER_AGENT_TOKEN and LEDGER_PRINCIPAL_TOKEN are required")
    if hmac.compare_digest(agent_token, principal_token):
        raise SystemExit("LEDGER_AGENT_TOKEN and LEDGER_PRINCIPAL_TOKEN must differ")


def serve(
    ledger: Ledger,
    store: JsonStore,
    port: int,
    *,
    agent_id: str,
    agent_token: str,
    principal_token: str,
) -> None:
    LedgerHandler.ledger = ledger
    LedgerHandler.store = store
    LedgerHandler.agent_id = agent_id
    LedgerHandler.agent_token = agent_token
    LedgerHandler.principal_token = principal_token
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
    agent_token = os.environ.get("LEDGER_AGENT_TOKEN", "")
    principal_token = os.environ.get("LEDGER_PRINCIPAL_TOKEN", "")
    try:
        _require_distinct_tokens(agent_token, principal_token)
        current_day()
        ledger, store, agent_id = open_service(ledger_path, config_path)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    except (OSError, PolicyError, json.JSONDecodeError, KeyError, TypeError, LedgerFileError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    serve(
        ledger,
        store,
        port,
        agent_id=agent_id,
        agent_token=agent_token,
        principal_token=principal_token,
    )


if __name__ == "__main__":
    main()
