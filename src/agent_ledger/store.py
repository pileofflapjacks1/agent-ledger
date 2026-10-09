from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from agent_ledger.ledger import Ledger
from agent_ledger.models import Audit, EscrowState, Job, Mandate, PolicyError, SkillListing


def _int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _snapshot(ledger: Ledger) -> dict:
    return {
        "seq": ledger._seq,
        "mandates": {
            agent_id: {
                "agent_id": mandate.agent_id,
                "principal_id": mandate.principal_id,
                "max_per_job_cents": mandate.max_per_job_cents,
                "max_daily_cents": mandate.max_daily_cents,
                "approval_above_cents": mandate.approval_above_cents,
                "allowed_payees": list(mandate.allowed_payees),
                "frozen": mandate.frozen,
            }
            for agent_id, mandate in ledger.mandates.items()
        },
        "listings": {
            listing_id: {
                "listing_id": listing.listing_id,
                "payee_id": listing.payee_id,
                "name": listing.name,
                "price_cents": listing.price_cents,
                "terms": listing.terms,
            }
            for listing_id, listing in ledger.listings.items()
        },
        "jobs": {
            job_id: {
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
            for job_id, job in ledger.jobs.items()
        },
        "balances": dict(ledger.balances),
        "audit": [
            {
                "action": entry.action,
                "actor": entry.actor,
                "detail": entry.detail,
                "extra": dict(entry.extra),
            }
            for entry in ledger.audit
        ],
    }


def _load_mandate(raw: object) -> Mandate:
    if not isinstance(raw, dict):
        raise ValueError("mandate must be an object")
    payees = raw.get("allowed_payees", [])
    if not isinstance(payees, list) or not all(isinstance(item, str) for item in payees):
        raise ValueError("allowed_payees must be a list of strings")
    frozen = raw.get("frozen", False)
    if not isinstance(frozen, bool):
        raise ValueError("frozen must be a boolean")
    return Mandate(
        agent_id=raw["agent_id"],
        principal_id=raw["principal_id"],
        max_per_job_cents=_int(raw["max_per_job_cents"], "max_per_job_cents"),
        max_daily_cents=_int(raw["max_daily_cents"], "max_daily_cents"),
        approval_above_cents=_int(raw.get("approval_above_cents", 0), "approval_above_cents"),
        allowed_payees=tuple(payees),
        frozen=frozen,
    )


def _load_listing(raw: object) -> SkillListing:
    if not isinstance(raw, dict):
        raise ValueError("listing must be an object")
    return SkillListing(
        listing_id=raw["listing_id"],
        payee_id=raw["payee_id"],
        name=raw["name"],
        price_cents=_int(raw["price_cents"], "price_cents"),
        terms=raw["terms"],
    )


def _load_job(raw: object) -> Job:
    if not isinstance(raw, dict):
        raise ValueError("job must be an object")
    return Job(
        job_id=raw["job_id"],
        buyer_id=raw["buyer_id"],
        payee_id=raw["payee_id"],
        amount_cents=_int(raw["amount_cents"], "amount_cents"),
        terms=raw["terms"],
        state=EscrowState(raw["state"]),
        approved=raw["approved"],
        disputed=raw["disputed"],
        payee_cents=_int(raw["payee_cents"], "payee_cents"),
        refund_cents=_int(raw["refund_cents"], "refund_cents"),
        day=raw["day"],
        listing_id=raw.get("listing_id"),
        note=raw.get("note", ""),
    )


def _fill(ledger: Ledger, data: object) -> None:
    if not isinstance(data, dict):
        raise ValueError("snapshot must be an object")
    ledger._seq = _int(data.get("seq", 0), "seq")
    mandates = data.get("mandates", {})
    listings = data.get("listings", {})
    jobs = data.get("jobs", {})
    balances = data.get("balances", {})
    audit = data.get("audit", [])
    if not all(isinstance(section, dict) for section in (mandates, listings, jobs, balances)):
        raise ValueError("snapshot collections must be objects")
    if not isinstance(audit, list):
        raise ValueError("audit must be a list")
    for agent_id, raw in mandates.items():
        mandate = _load_mandate(raw)
        if mandate.agent_id != agent_id:
            raise ValueError("mandate key does not match agent_id")
        ledger.mandates[agent_id] = mandate
    for listing_id, raw in listings.items():
        listing = _load_listing(raw)
        if listing.listing_id != listing_id:
            raise ValueError("listing key does not match listing_id")
        ledger.listings[listing_id] = listing
    for job_id, raw in jobs.items():
        job = _load_job(raw)
        if job.job_id != job_id:
            raise ValueError("job key does not match job_id")
        if not isinstance(job.approved, bool) or not isinstance(job.disputed, bool):
            raise ValueError("job flags must be booleans")
        ledger.jobs[job_id] = job
    for account_id, cents in balances.items():
        ledger.balances[account_id] = _int(cents, "balance")
    for raw in audit:
        if not isinstance(raw, dict) or not isinstance(raw.get("extra", {}), dict):
            raise ValueError("audit entry must be an object")
        ledger.audit.append(
            Audit(raw["action"], raw["actor"], raw["detail"], dict(raw.get("extra", {})))
        )


class JsonStore:
    """Persist one ledger as JSON. A missing or unreadable file is an empty ledger."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> Ledger:
        if not self.path.is_file():
            return Ledger()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            ledger = Ledger()
            _fill(ledger, data)
        except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError, PolicyError):
            return Ledger()
        return ledger

    def save(self, ledger: Ledger) -> None:
        payload = json.dumps(_snapshot(ledger), indent=2, sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # Rename onto the destination so a crash cannot leave a half-written snapshot in place.
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()
