"""Hash chain for the audit log. Nothing in this module calls a model."""

from __future__ import annotations

import hashlib
import json

from agent_ledger.models import Audit

GENESIS = ""
DENIAL_RULES = ("per-job", "daily", "frozen", "approval")


class ChainError(Exception):
    """Stored audit entries do not recompute to the same chain."""


def entry_hash(entry: Audit) -> str:
    body = {
        "prev_hash": entry.prev_hash,
        "action": entry.action,
        "actor": entry.actor,
        "detail": entry.detail,
        "extra": entry.extra,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stamp(entry: Audit, prev_hash: str) -> Audit:
    entry.prev_hash = prev_hash
    entry.entry_hash = entry_hash(entry)
    return entry


def verify_chain(audit: list[Audit]) -> None:
    previous = GENESIS
    for index, entry in enumerate(audit):
        if entry.prev_hash != previous:
            raise ChainError(f"audit entry {index} does not follow the previous hash")
        if entry.entry_hash != entry_hash(entry):
            raise ChainError(f"audit entry {index} hash does not match its contents")
        previous = entry.entry_hash


def format_audit(audit: list[Audit]) -> str:
    if not audit:
        return "(empty audit log)\n"
    lines = []
    for index, entry in enumerate(audit):
        extra = ""
        if entry.extra:
            rendered = " ".join(f"{key}={entry.extra[key]}" for key in sorted(entry.extra))
            extra = " " + rendered
        lines.append(f"{index} {entry.action} {entry.actor} {entry.detail}{extra}")
    return "\n".join(lines) + "\n"
