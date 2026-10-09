# Agent Ledger spec v0.1

Unaffiliated reference. This is not an xAI, X, or SpaceXAI product.

The ledger is the join that current agent payment rails skip: a human principal,
a hard budget, an escrow state machine, and a reputation score that ignores
unsettled work.

## Units

All money is an integer number of cents. No floats. A cent is a stand-in for
the smallest unit of whatever rail an adapter later settles (USDC 6-decimals,
X Money cents, a card auth). This package does not move real funds.

## Actors

- Principal. The human who owns the mandate and can change it.
- Agent. A named bot that can spend only inside an active mandate.
- Counterparty. Another agent, or an external payee id.

An agent cannot raise its own cap, freeze itself back open, or mark its own
delivery as released.

## Mandate

A mandate is the only spending authority.

- max_per_job_cents: hard ceiling for one escrow
- max_daily_cents: hard ceiling for funds locked today
- approval_above_cents: jobs above this stay pending until the principal approves
- allowed_payees: empty means any payee; otherwise an allow-list
- frozen: a frozen mandate rejects every new lock

Cap changes are principal actions and are appended to the audit log.

## Escrow states

open -> funded -> delivered -> released
                    |            \
                    v             v
                 disputed ---> resolved
                    |
                    v
                 refunded

released, resolved, and refunded are settled. open, funded, delivered, and
disputed are not. Reputation reads only settled rows.

## Reputation

For a payee:

- settled_jobs: released + resolved + refunded
- release_rate: released / settled_jobs
- dispute_rate: jobs that entered disputed / settled_jobs
- volume_cents: cents released plus the payee share of resolved jobs

A listing with zero settled jobs has no score. Volume alone cannot outrank
a payee with a worse release rate if the caller sorts by release_rate.

## What an adapter must do

A rail adapter implements two calls and nothing else:

- lock(job_id, cents) -> receipt
- settle(job_id, payee_cents, refund_cents) -> receipt

The ledger decides the amounts. The adapter does not decide policy.
