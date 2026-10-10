import json
import subprocess
import sys

import pytest

from agent_ledger.audit import entry_hash, verify_chain
from agent_ledger.models import Audit, EscrowState, Mandate
from agent_ledger.store import JsonStore, LedgerFileError


def test_missing_file_starts_empty(tmp_path):
    missing = tmp_path / "ledger.json"
    assert JsonStore(missing).load().jobs == {}
    assert not missing.exists()


def _assert_refused(path):
    original = path.read_bytes()
    with pytest.raises(LedgerFileError, match="refusing to load"):
        JsonStore(path).load()
    assert path.read_bytes() == original


def test_corrupt_file_is_refused_and_not_overwritten(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{", encoding="utf-8")
    _assert_refused(path)

    path.write_text(json.dumps({"jobs": {"job_1": {"state": "nope"}}}), encoding="utf-8")
    _assert_refused(path)

    bare = {
        "seq": 0,
        "audit": [{"action": "deposit", "actor": "buyer", "detail": "1", "extra": {}}],
    }
    path.write_text(json.dumps(bare), encoding="utf-8")
    _assert_refused(path)


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
    assert again.audit[0].prev_hash == ""
    assert again.audit[1].prev_hash == again.audit[0].entry_hash
    nxt = again.hire("buyer", "seller", 100, "next", "2026-10-09")
    assert nxt.job_id != job.job_id
    assert list(tmp_path.glob(".ledger.json.*.tmp")) == []
    verify_chain(again.audit)


def test_broken_audit_link_is_refused(tmp_path):
    path = tmp_path / "ledger.json"
    store = JsonStore(path)
    ledger = store.load()
    ledger.deposit("buyer", 100)
    ledger.deposit("buyer", 50)
    store.save(ledger)

    data = json.loads(path.read_text(encoding="utf-8"))
    data["audit"][1]["detail"] = "tampered"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(LedgerFileError, match="does not match"):
        JsonStore(path).load()
    assert json.loads(path.read_text(encoding="utf-8"))["audit"][1]["detail"] == "tampered"

    store.save(ledger)
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data["audit"][1]
    raw["prev_hash"] = ""
    relinked = Audit(raw["action"], raw["actor"], raw["detail"], raw["extra"], "", "")
    relinked.prev_hash = ""
    raw["entry_hash"] = entry_hash(relinked)
    path.write_text(json.dumps(data), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(LedgerFileError, match="does not follow"):
        JsonStore(path).load()
    assert path.read_bytes() == original


def test_verify_cli_prints_the_log_and_leaves_a_broken_file(tmp_path):
    path = tmp_path / "ledger.json"
    missing = tmp_path / "absent.json"
    empty = subprocess.run(
        [sys.executable, "-m", "agent_ledger", "verify", str(missing)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert empty.returncode == 0
    assert empty.stdout == "(empty audit log)\n"
    assert not missing.exists()

    ledger = JsonStore(path).load()
    ledger.deposit("buyer", 100)
    JsonStore(path).save(ledger)
    good = subprocess.run(
        [sys.executable, "-m", "agent_ledger", "verify", str(path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert good.returncode == 0
    assert "deposit buyer 100" in good.stdout
    assert good.stderr == ""

    path.write_text("{", encoding="utf-8")
    broken = subprocess.run(
        [sys.executable, "-m", "agent_ledger", "verify", str(path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert broken.returncode == 1
    assert "refusing to load" in broken.stderr
    assert broken.stdout == ""
    assert path.read_text(encoding="utf-8") == "{"
