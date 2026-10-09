from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EscrowState(str, Enum):
    OPEN = "open"
    FUNDED = "funded"
    DELIVERED = "delivered"
    RELEASED = "released"
    DISPUTED = "disputed"
    REFUNDED = "refunded"
    RESOLVED = "resolved"


SETTLED = {EscrowState.RELEASED, EscrowState.REFUNDED, EscrowState.RESOLVED}


class PolicyError(Exception):
    """A spend or transition the mandate does not allow."""


def require_cents(value: object) -> int:
    # bool is a subclass of int; True would otherwise fund as 1 cent.
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError("amount must be an integer number of cents")
    return value


@dataclass
class Mandate:
    agent_id: str
    principal_id: str
    max_per_job_cents: int
    max_daily_cents: int
    approval_above_cents: int = 0
    allowed_payees: tuple[str, ...] = ()
    frozen: bool = False

    def __post_init__(self) -> None:
        require_cents(self.max_per_job_cents)
        require_cents(self.max_daily_cents)
        require_cents(self.approval_above_cents)
        if self.max_per_job_cents < 0 or self.max_daily_cents < 0:
            raise PolicyError("caps cannot be negative")
        if self.approval_above_cents < 0:
            raise PolicyError("approval threshold cannot be negative")


@dataclass
class SkillListing:
    listing_id: str
    payee_id: str
    name: str
    price_cents: int
    terms: str

    def __post_init__(self) -> None:
        require_cents(self.price_cents)
        if self.price_cents < 0:
            raise PolicyError("price cannot be negative")


@dataclass
class Job:
    job_id: str
    buyer_id: str
    payee_id: str
    amount_cents: int
    terms: str
    state: EscrowState = EscrowState.OPEN
    approved: bool = False
    disputed: bool = False
    payee_cents: int = 0
    refund_cents: int = 0
    day: str = ""
    listing_id: Optional[str] = None
    note: str = ""


@dataclass
class Reputation:
    payee_id: str
    settled_jobs: int = 0
    released: int = 0
    refunded: int = 0
    resolved: int = 0
    disputed: int = 0
    volume_cents: int = 0

    @property
    def release_rate(self) -> float:
        if self.settled_jobs == 0:
            return 0.0
        return self.released / self.settled_jobs

    @property
    def dispute_rate(self) -> float:
        if self.settled_jobs == 0:
            return 0.0
        return self.disputed / self.settled_jobs


@dataclass
class Audit:
    action: str
    actor: str
    detail: str
    extra: dict = field(default_factory=dict)
