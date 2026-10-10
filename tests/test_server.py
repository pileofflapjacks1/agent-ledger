import os
import subprocess
import sys

from agent_ledger.models import EscrowState
from agent_ledger.store import JsonStore
from tests.support import (
    AGENT_TOKEN,
    PRINCIPAL_TOKEN,
    free_port,
    request_json,
    running_server,
    seed_balance,
    write_config,
)


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
            token=PRINCIPAL_TOKEN,
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
            token=PRINCIPAL_TOKEN,
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
            token=PRINCIPAL_TOKEN,
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
            token=PRINCIPAL_TOKEN,
        )
        assert disputed == 200
        resolved, body = request_json(
            port,
            "POST",
            "/resolve",
            {"principal_id": "joe", "job_id": job["job_id"], "payee_cents": 0},
            token=PRINCIPAL_TOKEN,
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


def test_server_stamps_day_and_ignores_caller_identity(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    config_path = tmp_path / "config.json"
    write_config(
        config_path,
        max_per_job_cents=1_000,
        max_daily_cents=1_500,
        approval_above_cents=2_000,
    )
    seed_balance(ledger_path, 5_000)
    with running_server(tmp_path, ledger_path, config_path) as port:
        first_status, first = request_json(
            port,
            "POST",
            "/hire",
            {
                "buyer_id": "someone-else",
                "payee_id": "research-bot",
                "amount_cents": 1_000,
                "terms": "tomorrow",
                "day": "2099-01-01",
            },
        )
        second_status, second = request_json(
            port,
            "POST",
            "/hire",
            {
                "payee_id": "research-bot",
                "amount_cents": 1_000,
                "terms": "yesterday",
                "day": "1999-01-01",
            },
        )
        assert first_status == 200
        assert second_status == 200
        assert first["buyer_id"] == "buyer-bot"
        assert first["day"] == "2026-10-08"
        assert second["day"] == "2026-10-08"
        funded, _ = request_json(port, "POST", "/fund", {"job_id": first["job_id"]})
        assert funded == 200
        blocked, body = request_json(port, "POST", "/fund", {"job_id": second["job_id"]})
        assert blocked == 403
        assert "daily" in body["error"]
        assert JsonStore(ledger_path).load().balances["buyer-bot"] == 4_000
        rules = [
            entry.extra["rule"]
            for entry in JsonStore(ledger_path).load().audit
            if entry.action == "deny"
        ]
        assert rules == ["daily"]
        assert all(entry.actor == "buyer-bot" for entry in JsonStore(ledger_path).load().audit if entry.action == "deny")


def test_refused_spend_is_audited_without_moving_balances(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    config_path = tmp_path / "config.json"
    write_config(
        config_path,
        max_per_job_cents=1_000,
        max_daily_cents=1_500,
        approval_above_cents=2_000,
    )
    seed_balance(ledger_path, 5_000)
    with running_server(tmp_path, ledger_path, config_path) as port:
        denied, denied_body = request_json(
            port,
            "POST",
            "/hire",
            {"payee_id": "research-bot", "amount_cents": 1_001, "terms": "too big"},
        )
        assert denied == 403
        assert "per-job" in denied_body["error"]
        booked = JsonStore(ledger_path).load()
        assert booked.jobs == {}
        assert booked.balances["buyer-bot"] == 5_000
        assert booked.audit[-1].action == "deny"
        assert booked.audit[-1].actor == "buyer-bot"
        assert booked.audit[-1].extra["rule"] == "per-job"

        _, first = request_json(
            port,
            "POST",
            "/hire",
            {"payee_id": "research-bot", "amount_cents": 800, "terms": "one"},
        )
        _, second = request_json(
            port,
            "POST",
            "/hire",
            {"payee_id": "research-bot", "amount_cents": 800, "terms": "two"},
        )
        funded, _ = request_json(port, "POST", "/fund", {"job_id": first["job_id"]})
        assert funded == 200
        blocked, blocked_body = request_json(port, "POST", "/fund", {"job_id": second["job_id"]})
        assert blocked == 403
        assert "daily" in blocked_body["error"]
        booked = JsonStore(ledger_path).load()
        assert booked.balances["buyer-bot"] == 4_200
        assert booked.jobs[second["job_id"]].state is EscrowState.OPEN
        assert [entry.extra["rule"] for entry in booked.audit if entry.action == "deny"] == [
            "per-job",
            "daily",
        ]

        missing, _ = request_json(port, "POST", "/fund", {"job_id": "missing"})
        assert missing == 404
        assert [entry.extra["rule"] for entry in JsonStore(ledger_path).load().audit if entry.action == "deny"] == [
            "per-job",
            "daily",
        ]

        frozen, _ = request_json(
            port, "POST", "/freeze", {"frozen": True}, token=PRINCIPAL_TOKEN
        )
        assert frozen == 200
        refused, refused_body = request_json(
            port,
            "POST",
            "/hire",
            {"payee_id": "research-bot", "amount_cents": 100, "terms": "halted"},
        )
        assert refused == 403
        assert "frozen" in refused_body["error"]
        rules = [entry.extra["rule"] for entry in JsonStore(ledger_path).load().audit if entry.action == "deny"]
        assert rules == ["per-job", "daily", "frozen"]

    approval_path = tmp_path / "approval.json"
    approval_config = tmp_path / "approval-config.json"
    write_config(
        approval_config,
        max_per_job_cents=2_000,
        max_daily_cents=5_000,
        approval_above_cents=100,
    )
    seed_balance(approval_path, 50)
    with running_server(tmp_path, approval_path, approval_config) as port:
        _, pending = request_json(
            port,
            "POST",
            "/hire",
            {"payee_id": "research-bot", "amount_cents": 200, "terms": "needs a person"},
        )
        blocked, blocked_body = request_json(port, "POST", "/fund", {"job_id": pending["job_id"]})
        assert blocked == 403
        assert "approval" in blocked_body["error"]
        _, small = request_json(
            port,
            "POST",
            "/hire",
            {"payee_id": "research-bot", "amount_cents": 80, "terms": "short"},
        )
        short, short_body = request_json(port, "POST", "/fund", {"job_id": small["job_id"]})
        assert short == 403
        assert "insufficient" in short_body["error"]
        booked = JsonStore(approval_path).load()
        assert booked.balances["buyer-bot"] == 50
        assert [entry.extra["rule"] for entry in booked.audit if entry.action == "deny"] == ["approval"]
        assert booked.jobs[pending["job_id"]].state is EscrowState.OPEN
        assert booked.jobs[small["job_id"]].state is EscrowState.OPEN


def test_frozen_http_blocks_payout_and_refunds_the_buyer(tmp_path):
    ledger_path, config_path = _service(tmp_path, balance=10_000)
    with running_server(tmp_path, ledger_path, config_path) as port:
        _, job = request_json(
            port,
            "POST",
            "/hire",
            {"payee_id": "research-bot", "amount_cents": 800, "terms": "hold"},
        )
        assert request_json(port, "POST", "/fund", {"job_id": job["job_id"]})[0] == 200
        assert (
            request_json(
                port,
                "POST",
                "/deliver",
                {"payee_id": "research-bot", "job_id": job["job_id"], "note": "done"},
            )[0]
            == 200
        )
        assert request_json(port, "POST", "/freeze", {"frozen": True}, token=PRINCIPAL_TOKEN)[0] == 200
        released, release_body = request_json(
            port,
            "POST",
            "/release",
            {"job_id": job["job_id"]},
            token=PRINCIPAL_TOKEN,
        )
        assert released == 403
        assert "frozen" in release_body["error"]
        booked = JsonStore(ledger_path).load()
        assert booked.jobs[job["job_id"]].state is EscrowState.DELIVERED
        assert booked.balances["buyer-bot"] == 9_200
        assert booked.balances.get("research-bot", 0) == 0
        denial = [entry for entry in booked.audit if entry.action == "deny"][-1]
        assert denial.actor == "joe"
        assert denial.extra["rule"] == "frozen"

        assert (
            request_json(
                port,
                "POST",
                "/dispute",
                {"job_id": job["job_id"], "note": "stop"},
                token=PRINCIPAL_TOKEN,
            )[0]
            == 200
        )
        partial, partial_body = request_json(
            port,
            "POST",
            "/resolve",
            {"job_id": job["job_id"], "payee_cents": 100},
            token=PRINCIPAL_TOKEN,
        )
        assert partial == 403
        assert "frozen" in partial_body["error"]
        assert JsonStore(ledger_path).load().jobs[job["job_id"]].state is EscrowState.DISPUTED
        assert JsonStore(ledger_path).load().balances["buyer-bot"] == 9_200
        refunded, refund_body = request_json(
            port,
            "POST",
            "/resolve",
            {"job_id": job["job_id"], "payee_cents": 0},
            token=PRINCIPAL_TOKEN,
        )
        assert refunded == 200
        assert refund_body["state"] == "refunded"
        assert JsonStore(ledger_path).load().balances["buyer-bot"] == 10_000


def test_server_leaves_a_corrupt_ledger_in_place(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text("{", encoding="utf-8")
    config_path = tmp_path / "config.json"
    write_config(config_path)
    env = os.environ.copy()
    env["PORT"] = str(free_port())
    env["LEDGER_PATH"] = str(ledger_path)
    env["LEDGER_CONFIG"] = str(config_path)
    env["LEDGER_AGENT_TOKEN"] = AGENT_TOKEN
    env["LEDGER_PRINCIPAL_TOKEN"] = PRINCIPAL_TOKEN
    env["LEDGER_TODAY"] = "2026-10-08"
    proc = subprocess.run(
        [sys.executable, "-m", "agent_ledger.server"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert proc.returncode == 2
    assert "refusing to load" in proc.stderr
    assert ledger_path.read_text(encoding="utf-8") == "{"


def test_agent_token_cannot_settle_or_raise_caps(tmp_path):
    ledger_path, config_path = _service(tmp_path, balance=10_000)
    with running_server(tmp_path, ledger_path, config_path) as port:
        _, job = request_json(
            port,
            "POST",
            "/hire",
            {
                "payee_id": "research-bot",
                "amount_cents": 800,
                "terms": "stay put",
            },
        )
        request_json(port, "POST", "/fund", {"job_id": job["job_id"]})
        request_json(
            port,
            "POST",
            "/deliver",
            {"payee_id": "research-bot", "job_id": job["job_id"]},
        )
        released, release_body = request_json(
            port,
            "POST",
            "/release",
            {"principal_id": "joe", "job_id": job["job_id"]},
        )
        assert released == 403
        assert release_body["error"] == "unauthorized"
        approved, approve_body = request_json(
            port,
            "POST",
            "/approve",
            {"principal_id": "joe", "job_id": job["job_id"]},
        )
        assert approved == 403
        assert approve_body["error"] == "unauthorized"
        missing, _ = request_json(
            port,
            "POST",
            "/set_caps",
            {"max_per_job_cents": 9_000_000, "max_daily_cents": 9_000_000},
        )
        assert missing == 404
        frozen, freeze_body = request_json(port, "POST", "/freeze", {"frozen": True})
        assert frozen == 403
        assert freeze_body["error"] == "unauthorized"

        guessed, _ = request_json(
            port,
            "POST",
            "/release",
            {"principal_id": "not-joe", "job_id": job["job_id"]},
            token=PRINCIPAL_TOKEN,
        )
        assert guessed == 200
        audit = JsonStore(ledger_path).load().audit
        assert any(entry.action == "release" and entry.actor == "joe" for entry in audit)
        assert JsonStore(ledger_path).load().mandates["buyer-bot"].max_per_job_cents == 2_000
        assert JsonStore(ledger_path).load().mandates["buyer-bot"].frozen is False
