"""MCP stdio door. Tools call the local ledger HTTP service."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

RULES = (
    "Do not hire unless hire returns approved or the principal has approved. "
    "Stop on needs-approval. Never claim a balance is real money."
)

TOOLS = [
    {
        "name": "hire",
        "description": "Open an escrow job on the local ledger. " + RULES,
        "inputSchema": {
            "type": "object",
            "properties": {
                "buyer_id": {"type": "string"},
                "payee_id": {"type": "string"},
                "amount_cents": {"type": "integer"},
                "terms": {"type": "string"},
                "day": {"type": "string", "description": "YYYY-MM-DD"},
                "listing_id": {"type": "string"},
            },
            "required": ["buyer_id", "payee_id", "amount_cents", "terms", "day"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fund",
        "description": "Lock the job amount on the local ledger. " + RULES,
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "deliver",
        "description": "Mark a funded job delivered. " + RULES,
        "inputSchema": {
            "type": "object",
            "properties": {
                "payee_id": {"type": "string"},
                "job_id": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["payee_id", "job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "release",
        "description": "Principal releases a delivered job to the payee. " + RULES,
        "inputSchema": {
            "type": "object",
            "properties": {
                "principal_id": {"type": "string"},
                "job_id": {"type": "string"},
            },
            "required": ["principal_id", "job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "reputation",
        "description": "Score a payee from settled jobs only. " + RULES,
        "inputSchema": {
            "type": "object",
            "properties": {"payee_id": {"type": "string"}},
            "required": ["payee_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_job",
        "description": "Read one job from the local ledger. " + RULES,
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
]


def call_tool(name: str, arguments: dict) -> tuple[object, bool]:
    if name == "hire":
        body = {
            "buyer_id": arguments.get("buyer_id"),
            "payee_id": arguments.get("payee_id"),
            "amount_cents": arguments.get("amount_cents"),
            "terms": arguments.get("terms"),
            "day": arguments.get("day"),
        }
        if arguments.get("listing_id"):
            body["listing_id"] = arguments["listing_id"]
        return _request("POST", "/hire", body)
    if name == "fund":
        return _request("POST", "/fund", {"job_id": arguments.get("job_id")})
    if name == "deliver":
        body = {"payee_id": arguments.get("payee_id"), "job_id": arguments.get("job_id")}
        if "note" in arguments:
            body["note"] = arguments["note"]
        return _request("POST", "/deliver", body)
    if name == "release":
        return _request(
            "POST",
            "/release",
            {"principal_id": arguments.get("principal_id"), "job_id": arguments.get("job_id")},
        )
    if name == "reputation":
        return _request("GET", "/reputation", query={"payee_id": arguments.get("payee_id", "")})
    if name == "get_job":
        return _request("GET", "/job", query={"job_id": arguments.get("job_id", "")})
    return f"unknown tool {name}", True


def _request(
    method: str, path: str, body: dict | None = None, query: dict | None = None
) -> tuple[object, bool]:
    base = os.environ.get("LEDGER_URL", "http://127.0.0.1:8787").rstrip("/")
    url = base + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8")), False
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw or exc.reason, True
        if isinstance(parsed, dict) and "error" in parsed:
            return parsed["error"], True
        return parsed, True
    except urllib.error.URLError as exc:
        return f"ledger http service unavailable: {exc.reason}", True


def handle(message: dict) -> dict | None:
    method = message.get("method")
    msg_id = message.get("id")
    if method in {"notifications/initialized", "initialized"} or (
        isinstance(method, str) and method.startswith("notifications/")
    ):
        return None
    if msg_id is None:
        return None
    if method == "initialize":
        params = message.get("params") or {}
        version = params.get("protocolVersion") or "2024-11-05"
        return _result(
            msg_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agent-ledger", "version": "0.1.0"},
            },
        )
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})
    if method == "resources/list":
        return _result(msg_id, {"resources": []})
    if method == "prompts/list":
        return _result(msg_id, {"prompts": []})
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        payload, is_error = call_tool(str(name), arguments)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return _result(
            msg_id,
            {"content": [{"type": "text", "text": text}], "isError": is_error},
        )
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def _result(msg_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main() -> None:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        line = stdin.readline()
        if not line:
            return
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        response = handle(message)
        if response is None:
            continue
        stdout.write(json.dumps(response).encode("utf-8") + b"\n")
        stdout.flush()


if __name__ == "__main__":
    main()
