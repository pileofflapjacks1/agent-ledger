---
name: agent-ledger
description: Check agent spend against a hard mandate, open escrow, and score payees only from settled jobs. Use before a Grok Bot or Grok Build agent hires another agent, pays for a skill, or reports reputation. Use the agent-ledger MCP tools (hire, fund, deliver, reputation, get_job) against the local HTTP ledger.
---

# Agent ledger

Reference policy layer for agent spend. It does not move real money. Amounts are integer cents.

## Rules

- A principal owns the mandate. The agent cannot raise its cap, unfreeze itself, or release its own job.
- Refuse a hire over the per-job cap, over the remaining daily cap, or to a payee outside the allow-list.
- A frozen mandate also refuses fund and release. A disputed job can still be refunded to the buyer in full. Do not expect a payout while frozen.
- Refusals for the per-job cap, the daily cap, freeze, and approval are written to the audit log as deny. They do not move balances.
- Check the log with `python -m agent_ledger verify`. That command does not call a model. A broken chain or a corrupt file is left in place.
- Do not send `day` or `buyer_id`. The server stamps the accounting day and takes the buyer from the agent token.
- If the amount is above approval_above_cents, leave the job unapproved. Do not fund it.
- Reputation comes only from released, resolved, and refunded jobs. Open or funded work is not a score.
- Never tell the user a balance is a real wallet. This package is a simulated ledger.
- Do not hire unless hire returns approved or the principal has approved.
- Stop on needs-approval.
- Never claim a balance is real money.

## MCP

The door is the local HTTP service. The MCP process only calls it. Do not reimplement caps, escrow, or reputation in the client.

From the repo root, with Python 3.11+, copy the example mandate and leave the HTTP service running:

```bash
cp config.example.json config.json
export LEDGER_AGENT_TOKEN="choose-an-agent-token"
export LEDGER_PRINCIPAL_TOKEN="choose-a-different-principal-token"
python -m agent_ledger.server
```

Grok Build then starts `python -m agent_ledger.mcp_server` from this repo's `.grok/config.toml` (`mcp_servers.agent-ledger`). Give that process `LEDGER_AGENT_TOKEN` only. `LEDGER_URL` defaults to `http://127.0.0.1:8787`. The interpreter in that config must be Python 3.11 or newer.

Run the HTTP service as a different user from the agent. The agent gets `LEDGER_URL` and `LEDGER_AGENT_TOKEN` only. It does not get `LEDGER_PRINCIPAL_TOKEN` and it does not get write access to `ledger.json`. The code cannot enforce a file permission the agent process does not have.

Tools: `hire`, `fund`, `deliver`, `reputation`, `get_job`.

`approve`, `release`, `dispute`, `resolve`, and `freeze` stay on the HTTP service and require `LEDGER_PRINCIPAL_TOKEN`. The hiring agent cannot settle or unfreeze its own job.

## Commands

From the repo root:

```bash
python -m agent_ledger demo
python -m agent_ledger verify
python -m pytest
python -m agent_ledger.server
```

The MCP process is separate and stays up with the HTTP service:

```bash
export LEDGER_AGENT_TOKEN="choose-an-agent-token"
python -m agent_ledger.mcp_server
```

Hire path in code: open_mandate, deposit, list_skill, hire, approve if needed, fund, deliver, release or dispute then resolve.
