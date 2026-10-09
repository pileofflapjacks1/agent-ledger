from __future__ import annotations

import argparse
import json

from agent_ledger.ledger import Ledger
from agent_ledger.models import Mandate


def demo() -> dict:
    ledger = Ledger()
    ledger.open_mandate(
        Mandate(
            agent_id="buyer-bot",
            principal_id="joe",
            max_per_job_cents=2_000,
            max_daily_cents=5_000,
            approval_above_cents=1_000,
            allowed_payees=("research-bot",),
        )
    )
    ledger.deposit("buyer-bot", 10_000)
    listing = ledger.list_skill(
        "research-bot",
        "queue-watch",
        800,
        "Watch one drop window and return the link.",
    )
    job = ledger.hire(
        "buyer-bot",
        "research-bot",
        listing.price_cents,
        listing.terms,
        day="2026-10-08",
        listing_id=listing.listing_id,
    )
    ledger.fund(job.job_id)
    ledger.deliver("research-bot", job.job_id, note="link ready")
    ledger.release("joe", job.job_id)
    return {
        "job": job.job_id,
        "state": job.state.value,
        "reputation": ledger.reputation("research-bot").__dict__,
        "pnl": ledger.pnl("buyer-bot"),
        "audit": len(ledger.audit),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent ledger reference implementation")
    parser.add_argument("command", choices=["demo"])
    args = parser.parse_args()
    if args.command == "demo":
        print(json.dumps(demo(), indent=2))


if __name__ == "__main__":
    main()
