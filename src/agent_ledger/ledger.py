from __future__ import annotations

from collections import defaultdict
from typing import Optional

from agent_ledger.models import (
    SETTLED,
    Audit,
    EscrowState,
    Job,
    Mandate,
    PolicyError,
    Reputation,
    SkillListing,
)


class Ledger:
    """In-memory settlement ledger. Amounts are integer cents. No real funds."""

    def __init__(self) -> None:
        self.mandates: dict[str, Mandate] = {}
        self.listings: dict[str, SkillListing] = {}
        self.jobs: dict[str, Job] = {}
        self.balances: dict[str, int] = defaultdict(int)
        self.audit: list[Audit] = []
        self._seq = 0

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq}"

    def _log(self, action: str, actor: str, detail: str, **extra: object) -> None:
        self.audit.append(Audit(action, actor, detail, extra))

    def open_mandate(self, mandate: Mandate) -> Mandate:
        if mandate.agent_id in self.mandates:
            raise PolicyError(f"mandate already exists for {mandate.agent_id}")
        self.mandates[mandate.agent_id] = mandate
        self._log("open_mandate", mandate.principal_id, mandate.agent_id)
        return mandate

    def set_caps(
        self,
        principal_id: str,
        agent_id: str,
        max_per_job_cents: int,
        max_daily_cents: int,
        approval_above_cents: Optional[int] = None,
    ) -> Mandate:
        mandate = self._mandate_for_principal(principal_id, agent_id)
        if max_per_job_cents < 0 or max_daily_cents < 0:
            raise PolicyError("caps cannot be negative")
        mandate.max_per_job_cents = max_per_job_cents
        mandate.max_daily_cents = max_daily_cents
        if approval_above_cents is not None:
            if approval_above_cents < 0:
                raise PolicyError("approval threshold cannot be negative")
            mandate.approval_above_cents = approval_above_cents
        self._log(
            "set_caps",
            principal_id,
            agent_id,
            max_per_job_cents=max_per_job_cents,
            max_daily_cents=max_daily_cents,
        )
        return mandate

    def freeze(self, principal_id: str, agent_id: str, frozen: bool = True) -> Mandate:
        mandate = self._mandate_for_principal(principal_id, agent_id)
        mandate.frozen = frozen
        self._log("freeze" if frozen else "unfreeze", principal_id, agent_id)
        return mandate

    def deposit(self, account_id: str, cents: int) -> int:
        if cents <= 0:
            raise PolicyError("deposit must be positive")
        self.balances[account_id] += cents
        self._log("deposit", account_id, str(cents))
        return self.balances[account_id]

    def list_skill(
        self, payee_id: str, name: str, price_cents: int, terms: str
    ) -> SkillListing:
        listing = SkillListing(self._next("lst"), payee_id, name, price_cents, terms)
        self.listings[listing.listing_id] = listing
        self._log("list_skill", payee_id, listing.listing_id, price_cents=price_cents)
        return listing

    def hire(
        self,
        buyer_id: str,
        payee_id: str,
        amount_cents: int,
        terms: str,
        day: str,
        listing_id: Optional[str] = None,
    ) -> Job:
        if amount_cents <= 0:
            raise PolicyError("amount must be positive")
        if buyer_id == payee_id:
            raise PolicyError("an agent cannot hire itself")
        mandate = self.mandates.get(buyer_id)
        if mandate is None:
            raise PolicyError(f"no mandate for {buyer_id}")
        if mandate.frozen:
            raise PolicyError("mandate is frozen")
        if amount_cents > mandate.max_per_job_cents:
            raise PolicyError("over per-job cap")
        if mandate.allowed_payees and payee_id not in mandate.allowed_payees:
            raise PolicyError("payee is not on the allow-list")
        spent = self._locked_today(buyer_id, day)
        if spent + amount_cents > mandate.max_daily_cents:
            raise PolicyError("over daily cap")
        if listing_id is not None:
            listing = self.listings.get(listing_id)
            if listing is None or listing.payee_id != payee_id:
                raise PolicyError("listing does not match payee")
            if amount_cents != listing.price_cents:
                raise PolicyError("amount does not match listing price")
        needs_approval = amount_cents > mandate.approval_above_cents
        job = Job(
            job_id=self._next("job"),
            buyer_id=buyer_id,
            payee_id=payee_id,
            amount_cents=amount_cents,
            terms=terms,
            approved=not needs_approval,
            day=day,
            listing_id=listing_id,
        )
        self.jobs[job.job_id] = job
        self._log("hire", buyer_id, job.job_id, amount_cents=amount_cents, approved=job.approved)
        return job

    def approve(self, principal_id: str, job_id: str) -> Job:
        job = self._job(job_id)
        mandate = self._mandate_for_principal(principal_id, job.buyer_id)
        if mandate.frozen:
            raise PolicyError("mandate is frozen")
        job.approved = True
        self._log("approve", principal_id, job_id)
        return job

    def fund(self, job_id: str) -> Job:
        job = self._job(job_id)
        self._expect(job, EscrowState.OPEN)
        if not job.approved:
            raise PolicyError("job needs principal approval before funding")
        mandate = self.mandates[job.buyer_id]
        if mandate.frozen:
            raise PolicyError("mandate is frozen")
        if self.balances[job.buyer_id] < job.amount_cents:
            raise PolicyError("insufficient balance")
        self.balances[job.buyer_id] -= job.amount_cents
        job.state = EscrowState.FUNDED
        self._log("fund", job.buyer_id, job_id, amount_cents=job.amount_cents)
        return job

    def deliver(self, payee_id: str, job_id: str, note: str = "") -> Job:
        job = self._job(job_id)
        self._expect(job, EscrowState.FUNDED)
        if payee_id != job.payee_id:
            raise PolicyError("only the payee can deliver")
        job.state = EscrowState.DELIVERED
        job.note = note
        self._log("deliver", payee_id, job_id)
        return job

    def release(self, principal_id: str, job_id: str) -> Job:
        job = self._job(job_id)
        self._mandate_for_principal(principal_id, job.buyer_id)
        self._expect(job, EscrowState.DELIVERED)
        job.state = EscrowState.RELEASED
        job.payee_cents = job.amount_cents
        self.balances[job.payee_id] += job.amount_cents
        self._log("release", principal_id, job_id, payee_cents=job.payee_cents)
        return job

    def dispute(self, principal_id: str, job_id: str, note: str) -> Job:
        job = self._job(job_id)
        self._mandate_for_principal(principal_id, job.buyer_id)
        if job.state not in {EscrowState.FUNDED, EscrowState.DELIVERED}:
            raise PolicyError(f"cannot dispute from {job.state.value}")
        job.state = EscrowState.DISPUTED
        job.disputed = True
        job.note = note
        self._log("dispute", principal_id, job_id)
        return job

    def resolve(self, principal_id: str, job_id: str, payee_cents: int) -> Job:
        job = self._job(job_id)
        self._mandate_for_principal(principal_id, job.buyer_id)
        self._expect(job, EscrowState.DISPUTED)
        if payee_cents < 0 or payee_cents > job.amount_cents:
            raise PolicyError("resolution split is outside the locked amount")
        refund = job.amount_cents - payee_cents
        job.payee_cents = payee_cents
        job.refund_cents = refund
        job.state = EscrowState.REFUNDED if payee_cents == 0 else EscrowState.RESOLVED
        if payee_cents == job.amount_cents:
            job.state = EscrowState.RELEASED
        self.balances[job.payee_id] += payee_cents
        self.balances[job.buyer_id] += refund
        self._log(
            "resolve",
            principal_id,
            job_id,
            payee_cents=payee_cents,
            refund_cents=refund,
        )
        return job

    def reputation(self, payee_id: str) -> Reputation:
        rep = Reputation(payee_id=payee_id)
        for job in self.jobs.values():
            if job.payee_id != payee_id or job.state not in SETTLED:
                continue
            rep.settled_jobs += 1
            rep.volume_cents += job.payee_cents
            if job.disputed:
                rep.disputed += 1
            if job.state is EscrowState.RELEASED:
                rep.released += 1
            elif job.state is EscrowState.REFUNDED:
                rep.refunded += 1
            elif job.state is EscrowState.RESOLVED:
                rep.resolved += 1
        return rep

    def pnl(self, agent_id: str) -> dict[str, int]:
        paid_out = sum(
            j.amount_cents - j.refund_cents
            for j in self.jobs.values()
            if j.buyer_id == agent_id and j.state in SETTLED
        )
        earned = sum(
            j.payee_cents for j in self.jobs.values() if j.payee_id == agent_id and j.state in SETTLED
        )
        locked = sum(
            j.amount_cents
            for j in self.jobs.values()
            if j.buyer_id == agent_id and j.state not in SETTLED and j.state is not EscrowState.OPEN
        )
        return {
            "balance_cents": self.balances[agent_id],
            "earned_cents": earned,
            "paid_out_cents": paid_out,
            "locked_cents": locked,
        }

    def _locked_today(self, buyer_id: str, day: str) -> int:
        return sum(
            j.amount_cents
            for j in self.jobs.values()
            if j.buyer_id == buyer_id and j.day == day and j.state is not EscrowState.OPEN
        )

    def _mandate_for_principal(self, principal_id: str, agent_id: str) -> Mandate:
        mandate = self.mandates.get(agent_id)
        if mandate is None or mandate.principal_id != principal_id:
            raise PolicyError("principal does not own this mandate")
        return mandate

    def _job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise PolicyError(f"unknown job {job_id}")
        return job

    @staticmethod
    def _expect(job: Job, state: EscrowState) -> None:
        if job.state is not state:
            raise PolicyError(f"expected {state.value}, got {job.state.value}")
