---
name: agent-ledger
description: Check agent spend against a hard mandate, open escrow, and score payees only from settled jobs. Use before a Grok Bot or Grok Build agent hires another agent, pays for a skill, or reports reputation.
---

# Agent ledger

Reference policy layer for agent spend. It does not move real money. Amounts are integer cents.

## Rules

- A principal owns the mandate. The agent cannot raise its cap, unfreeze itself, or release its own job.
- Refuse a hire over the per-job cap, over the remaining daily cap, or to a payee outside the allow-list.
- Pass `day` as `YYYY-MM-DD`. Any other spelling is refused.
- If the amount is above approval_above_cents, leave the job unapproved. Do not fund it.
- Reputation comes only from released, resolved, and refunded jobs. Open or funded work is not a score.
- Never tell the user a balance is a real wallet. This package is a simulated ledger.

## Commands

From the repo root:

```bash
python -m agent_ledger demo
python -m pytest
```

Hire path in code: open_mandate, deposit, list_skill, hire, approve if needed, fund, deliver, release or dispute then resolve.
