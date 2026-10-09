from agent_ledger.models import EscrowState
from agent_ledger.store import JsonStore
from tests.support import request_json, running_server, seed_balance, write_config


def _service(tmp_path, *, balance: int = 0):
    ledger_path = tmp_path / "ledger.json"
    config_path = tmp_path / "config.json"
    write_config(config_path)
    if balance:
        seed_balance(ledger_path, balance)
    return ledger_path, config_path


def test_http_refuses_over_cap_hire(tmp_path):
    ledger_path, config_path = _service(tmp_path)
    with running_server(tmp_path, ledger_path, config_path) as port:
        status, body = request_json(
            port,
            "POST",
            "/hire",
            {
                "buyer_id": "buyer-bot",
                "payee_id": "research-bot",
                "amount_cents": 2_001,
                "terms": "too big",
                "day": "2026-10-08",
                "max_per_job_cents": 9_000_000,
            },
        )
        assert status == 403
        assert "per-job" in body["error"]
        assert JsonStore(ledger_path).load().jobs == {}
        assert JsonStore(ledger_path).load().mandates["buyer-bot"].max_per_job_cents == 2_000
        missing, missing_body = request_json(port, "POST", "/set_caps", {"buyer_id": "buyer-bot"})
        assert missing == 404
        assert missing_body["error"]


def test_http_blocks_fund_before_approval_then_allows_it(tmp_path):
    ledger_path, config_path = _service(tmp_path, balance=10_000)
    with running_server(tmp_path, ledger_path, config_path) as port:
        status, job = request_json(
            port,
            "POST",
            "/hire",
            {
                "buyer_id": "buyer-bot",
                "payee_id": "research-bot",
                "amount_cents": 1_500,
                "terms": "needs a person",
                "day": "2026-10-08",
            },
        )
        assert status == 200
        assert job["approved"] is False
        assert job["state"] == "open"
        blocked, blocked_body = request_json(port, "POST", "/fund", {"job_id": job["job_id"]})
        assert blocked == 403
        assert "approval" in blocked_body["error"]
        still, open_job = request_json(port, "GET", f"/job?job_id={job['job_id']}")
        assert still == 200
        assert open_job["state"] == "open"
        approved, _ = request_json(
            port,
            "POST",
            "/approve",
            {"principal_id": "joe", "job_id": job["job_id"]},
        )
        assert approved == 200
        funded, paid = request_json(port, "POST", "/fund", {"job_id": job["job_id"]})
        assert funded == 200
        assert paid["state"] == "funded"
        on_disk = JsonStore(ledger_path).load()
        assert on_disk.jobs[job["job_id"]].state is EscrowState.FUNDED
        assert on_disk.balances["buyer-bot"] == 8_500


def test_http_releases_after_deliver(tmp_path):
    ledger_path, config_path = _service(tmp_path, balance=10_000)
    with running_server(tmp_path, ledger_path, config_path) as port:
        status, job = request_json(
            port,
            "POST",
            "/hire",
            {
                "buyer_id": "buyer-bot",
                "payee_id": "research-bot",
                "amount_cents": 800,
                "terms": "Watch one drop window and return the link.",
                "day": "2026-10-08",
            },
        )
        assert status == 200
        assert job["approved"] is True
        assert job["state"] == "open"
        funded, _ = request_json(port, "POST", "/fund", {"job_id": job["job_id"]})
        assert funded == 200
        delivered, _ = request_json(
            port,
            "POST",
            "/deliver",
            {"payee_id": "research-bot", "job_id": job["job_id"], "note": "link ready"},
        )
        assert delivered == 200
        released, done = request_json(
            port,
            "POST",
            "/release",
            {"principal_id": "joe", "job_id": job["job_id"]},
        )
        assert released == 200
        assert done["state"] == "released"
        assert JsonStore(ledger_path).load().balances["research-bot"] == 800


def test_http_reputation_ignores_unfunded_job(tmp_path):
    ledger_path, config_path = _service(tmp_path, balance=10_000)
    with running_server(tmp_path, ledger_path, config_path) as port:
        status, open_job = request_json(
            port,
            "POST",
            "/hire",
            {
                "buyer_id": "buyer-bot",
                "payee_id": "research-bot",
                "amount_cents": 100,
                "terms": "never funded",
                "day": "2026-10-08",
            },
        )
        assert status == 200
        _, settled = request_json(
            port,
            "POST",
            "/hire",
            {
                "buyer_id": "buyer-bot",
                "payee_id": "research-bot",
                "amount_cents": 200,
                "terms": "finished",
                "day": "2026-10-08",
            },
        )
        request_json(port, "POST", "/fund", {"job_id": settled["job_id"]})
        request_json(
            port,
            "POST",
            "/deliver",
            {"payee_id": "research-bot", "job_id": settled["job_id"]},
        )
        request_json(
            port,
            "POST",
            "/release",
            {"principal_id": "joe", "job_id": settled["job_id"]},
        )
        code, rep = request_json(port, "GET", "/reputation?payee_id=research-bot")
        assert code == 200
        assert rep["settled_jobs"] == 1
        assert rep["released"] == 1
        assert rep["volume_cents"] == 200
        still, untouched = request_json(port, "GET", f"/job?job_id={open_job['job_id']}")
        assert still == 200
        assert untouched["state"] == "open"


def test_http_dispute_resolve_refunds_the_buyer(tmp_path):
    ledger_path, config_path = _service(tmp_path, balance=10_000)
    with running_server(tmp_path, ledger_path, config_path) as port:
        _, job = request_json(
            port,
            "POST",
            "/hire",
            {
                "buyer_id": "buyer-bot",
                "payee_id": "research-bot",
                "amount_cents": 300,
                "terms": "wrong file",
                "day": "2026-10-08",
            },
        )
        request_json(port, "POST", "/fund", {"job_id": job["job_id"]})
        request_json(
            port,
            "POST",
            "/deliver",
            {"payee_id": "research-bot", "job_id": job["job_id"]},
        )
        disputed, _ = request_json(
            port,
            "POST",
            "/dispute",
            {"principal_id": "joe", "job_id": job["job_id"], "note": "wrong file"},
        )
        assert disputed == 200
        resolved, body = request_json(
            port,
            "POST",
            "/resolve",
            {"principal_id": "joe", "job_id": job["job_id"], "payee_cents": 0},
        )
        assert resolved == 200
        assert body["state"] == "refunded"
        assert JsonStore(ledger_path).load().balances["buyer-bot"] == 10_000


def test_restarted_server_still_has_funded_job(tmp_path):
    ledger_path, config_path = _service(tmp_path, balance=10_000)
    with running_server(tmp_path, ledger_path, config_path) as port:
        _, job = request_json(
            port,
            "POST",
            "/hire",
            {
                "buyer_id": "buyer-bot",
                "payee_id": "research-bot",
                "amount_cents": 800,
                "terms": "persist me",
                "day": "2026-10-08",
            },
        )
        status, funded = request_json(port, "POST", "/fund", {"job_id": job["job_id"]})
        assert status == 200
        assert funded["state"] == "funded"
        on_disk = JsonStore(ledger_path).load()
        assert on_disk.jobs[job["job_id"]].state is EscrowState.FUNDED
        assert on_disk.balances["buyer-bot"] == 9_200
        unknown, unknown_body = request_json(port, "GET", "/job?job_id=missing")
        assert unknown == 404
        assert unknown_body["error"].startswith("unknown job")

    with running_server(tmp_path, ledger_path, config_path) as port:
        status, restored = request_json(port, "GET", f"/job?job_id={job['job_id']}")
        assert status == 200
        assert restored["state"] == "funded"
        assert restored["amount_cents"] == 800
        again = JsonStore(ledger_path).load()
        assert again.balances["buyer-bot"] == 9_200
        assert sum(entry.action == "open_mandate" for entry in again.audit) == 1
        assert not any(entry.action == "set_caps" for entry in again.audit)
