import pytest

from agent_ledger.ledger import Ledger
from agent_ledger.models import EscrowState, Mandate, PolicyError


def mandate(**overrides) -> Mandate:
    base = dict(
        agent_id="buyer",
        principal_id="human",
        max_per_job_cents=1_000,
        max_daily_cents=1_500,
        approval_above_cents=400,
        allowed_payees=("seller",),
    )
    base.update(overrides)
    return Mandate(**base)


def setup() -> Ledger:
    ledger = Ledger()
    ledger.open_mandate(mandate())
    ledger.deposit("buyer", 5_000)
    return ledger


def test_hard_cap_refuses_over_job_and_day():
    ledger = setup()
    with pytest.raises(PolicyError, match="per-job"):
        ledger.hire("buyer", "seller", 1_001, "too big", "2026-10-08")
    ledger.hire("buyer", "seller", 1_000, "a", "2026-10-08")
    ledger.approve("human", "job_1")
    ledger.fund("job_1")
    with pytest.raises(PolicyError, match="daily"):
        ledger.hire("buyer", "seller", 600, "b", "2026-10-08")


def test_agent_cannot_release_or_raise_cap():
    ledger = setup()
    with pytest.raises(PolicyError, match="principal"):
        ledger.set_caps("buyer", "buyer", 9_000, 9_000)
    job = ledger.hire("buyer", "seller", 200, "small", "2026-10-08")
    ledger.fund(job.job_id)
    ledger.deliver("seller", job.job_id)
    with pytest.raises(PolicyError, match="principal"):
        ledger.release("buyer", job.job_id)


def test_allow_list_and_self_hire():
    ledger = setup()
    with pytest.raises(PolicyError, match="allow-list"):
        ledger.hire("buyer", "stranger", 100, "nope", "2026-10-08")
    with pytest.raises(PolicyError, match="itself"):
        ledger.hire("buyer", "buyer", 100, "nope", "2026-10-08")


def test_approval_gate_blocks_fund():
    ledger = setup()
    job = ledger.hire("buyer", "seller", 500, "needs a person", "2026-10-08")
    assert job.approved is False
    with pytest.raises(PolicyError, match="approval"):
        ledger.fund(job.job_id)
    ledger.approve("human", job.job_id)
    ledger.fund(job.job_id)
    assert job.state is EscrowState.FUNDED


def test_reputation_ignores_unsettled_and_counts_refund():
    ledger = setup()
    open_job = ledger.hire("buyer", "seller", 100, "never funded", "2026-10-08")
    assert open_job.state is EscrowState.OPEN
    paid = ledger.hire("buyer", "seller", 200, "ok", "2026-10-08")
    ledger.fund(paid.job_id)
    ledger.deliver("seller", paid.job_id)
    ledger.release("human", paid.job_id)
    bad = ledger.hire("buyer", "seller", 300, "bad", "2026-10-09")
    ledger.approve("human", bad.job_id)
    ledger.fund(bad.job_id)
    ledger.deliver("seller", bad.job_id)
    ledger.dispute("human", bad.job_id, "wrong file")
    ledger.resolve("human", bad.job_id, 0)
    rep = ledger.reputation("seller")
    assert rep.settled_jobs == 2
    assert rep.released == 1
    assert rep.refunded == 1
    assert rep.disputed == 1
    assert rep.volume_cents == 200
    assert rep.release_rate == 0.5


def test_freeze_stops_new_locks_and_pnl_tracks_lock():
    ledger = setup()
    ledger.freeze("human", "buyer")
    with pytest.raises(PolicyError, match="frozen"):
        ledger.hire("buyer", "seller", 100, "nope", "2026-10-08")
    ledger.freeze("human", "buyer", frozen=False)
    job = ledger.hire("buyer", "seller", 100, "go", "2026-10-08")
    ledger.fund(job.job_id)
    assert ledger.pnl("buyer")["locked_cents"] == 100
