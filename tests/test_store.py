import json

from agent_ledger.models import EscrowState, Mandate
from agent_ledger.store import JsonStore


def test_missing_or_corrupt_file_starts_empty(tmp_path):
    missing = tmp_path / "ledger.json"
    assert JsonStore(missing).load().jobs == {}

    missing.write_text("{", encoding="utf-8")
    assert JsonStore(missing).load().mandates == {}

    missing.write_text(json.dumps({"jobs": {"job_1": {"state": "nope"}}}), encoding="utf-8")
    assert JsonStore(missing).load().jobs == {}


def test_hire_and_fund_survive_reload(tmp_path):
    path = tmp_path / "ledger.json"
    store = JsonStore(path)
    ledger = store.load()
    ledger.open_mandate(
        Mandate(
            agent_id="buyer",
            principal_id="human",
            max_per_job_cents=1_000,
            max_daily_cents=1_500,
            approval_above_cents=2_000,
            allowed_payees=("seller",),
        )
    )
    ledger.deposit("buyer", 5_000)
    job = ledger.hire("buyer", "seller", 1_000, "a shift", "2026-10-08")
    ledger.fund(job.job_id)
    store.save(ledger)

    again = JsonStore(path).load()
    restored = again.jobs[job.job_id]
    assert restored.state is EscrowState.FUNDED
    assert restored.approved is True
    assert restored.amount_cents == 1_000
    assert again.balances["buyer"] == 4_000
    assert again.mandates["buyer"].max_daily_cents == 1_500
    assert again._seq == ledger._seq
    assert any(entry.action == "fund" for entry in again.audit)
    nxt = again.hire("buyer", "seller", 100, "next", "2026-10-09")
    assert nxt.job_id != job.job_id
    assert list(tmp_path.glob(".ledger.json.*.tmp")) == []
