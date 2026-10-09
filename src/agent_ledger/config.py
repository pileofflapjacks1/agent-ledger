from __future__ import annotations

import json
from pathlib import Path

from agent_ledger.ledger import Ledger
from agent_ledger.models import Mandate, PolicyError


def load_mandate_config(path: str | Path) -> Mandate:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PolicyError("mandate config must be a JSON object")
    agent_id = _require_str(data, "agent_id")
    principal_id = _require_str(data, "principal_id")
    try:
        max_per_job_cents = data["max_per_job_cents"]
        max_daily_cents = data["max_daily_cents"]
    except KeyError as exc:
        raise PolicyError(f"missing {exc.args[0]}") from None
    payees = data.get("allowed_payees", [])
    if not isinstance(payees, list) or not all(isinstance(item, str) for item in payees):
        raise PolicyError("allowed_payees must be a list of strings")
    frozen = data.get("frozen", False)
    if not isinstance(frozen, bool):
        raise PolicyError("frozen must be a boolean")
    return Mandate(
        agent_id=agent_id,
        principal_id=principal_id,
        max_per_job_cents=max_per_job_cents,
        max_daily_cents=max_daily_cents,
        approval_above_cents=data.get("approval_above_cents", 0),
        allowed_payees=tuple(payees),
        frozen=frozen,
    )


def apply_mandate(ledger: Ledger, incoming: Mandate) -> bool:
    """Open a missing mandate. Later cap or freeze edits go through set_caps and freeze."""
    current = ledger.mandates.get(incoming.agent_id)
    if current is None:
        ledger.open_mandate(incoming)
        return True
    changed = False
    if (
        current.max_per_job_cents != incoming.max_per_job_cents
        or current.max_daily_cents != incoming.max_daily_cents
        or current.approval_above_cents != incoming.approval_above_cents
    ):
        ledger.set_caps(
            incoming.principal_id,
            incoming.agent_id,
            incoming.max_per_job_cents,
            incoming.max_daily_cents,
            incoming.approval_above_cents,
        )
        changed = True
    if current.frozen != incoming.frozen:
        ledger.freeze(incoming.principal_id, incoming.agent_id, incoming.frozen)
        changed = True
    return changed


def _require_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise PolicyError(f"{key} must be a string")
    return value
