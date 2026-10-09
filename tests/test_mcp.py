import ast
import json
import os
import select
import subprocess
import sys

from agent_ledger.mcp_server import TOOLS
from tests.support import ROOT, request_json, running_server, seed_balance, write_config

RULES = (
    "do not hire unless hire returns approved or the principal has approved",
    "stop on needs-approval",
    "never claim a balance is real money",
)


def test_tool_descriptions_state_the_rules():
    names = [tool["name"] for tool in TOOLS]
    assert names == ["hire", "fund", "deliver", "release", "reputation", "get_job"]
    for tool in TOOLS:
        text = tool["description"].lower()
        for rule in RULES:
            assert rule in text


def test_mcp_tools_call_the_http_service(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    config_path = tmp_path / "config.json"
    write_config(config_path)
    seed_balance(ledger_path, 10_000)
    with running_server(tmp_path, ledger_path, config_path) as port:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent_ledger.mcp_server"],
            cwd=tmp_path,
            env={
                **os.environ,
                "LEDGER_URL": f"http://127.0.0.1:{port}",
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            def rpc(message: dict) -> dict:
                assert proc.stdin is not None and proc.stdout is not None
                proc.stdin.write(json.dumps(message).encode() + b"\n")
                proc.stdin.flush()
                ready, _, _ = select.select([proc.stdout], [], [], 5)
                if not ready:
                    raise TimeoutError("mcp server did not respond")
                line = proc.stdout.readline()
                return json.loads(line)

            init = rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"},
                }
            )
            assert init["result"]["serverInfo"]["name"] == "agent-ledger"
            proc.stdin.write(
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
                + b"\n"
            )
            proc.stdin.flush()
            listed = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert [tool["name"] for tool in listed["result"]["tools"]] == [
                "hire",
                "fund",
                "deliver",
                "release",
                "reputation",
                "get_job",
            ]
            hired = rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "hire",
                        "arguments": {
                            "buyer_id": "buyer-bot",
                            "payee_id": "research-bot",
                            "amount_cents": 800,
                            "terms": "under the cap",
                            "day": "2026-10-08",
                        },
                    },
                }
            )
            assert hired["result"]["isError"] is False
            job = json.loads(hired["result"]["content"][0]["text"])
            assert job["approved"] is True
            assert job["state"] == "open"
            refused = rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "hire",
                        "arguments": {
                            "buyer_id": "buyer-bot",
                            "payee_id": "research-bot",
                            "amount_cents": 9_000,
                            "terms": "over the cap",
                            "day": "2026-10-08",
                        },
                    },
                }
            )
            assert refused["result"]["isError"] is True
            assert "per-job" in refused["result"]["content"][0]["text"]
            code, fetched = request_json(port, "GET", f"/job?job_id={job['job_id']}")
            assert code == 200
            assert fetched["amount_cents"] == 800
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=3)


def test_no_payment_sdk_and_mcp_does_not_import_ledger():
    banned = {"stripe", "x402", "coinbase", "paypal", "braintree"}
    for path in (ROOT / "src").rglob("*.py"):
        modules = _imported(path.read_text(encoding="utf-8"))
        assert not (banned & {name.split(".")[0] for name in modules})
    mcp_modules = _imported((ROOT / "src" / "agent_ledger" / "mcp_server.py").read_text())
    assert not any(name == "agent_ledger" or name.startswith("agent_ledger.") for name in mcp_modules)


def _imported(source: str) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules
