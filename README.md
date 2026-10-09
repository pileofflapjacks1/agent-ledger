# agent-ledger

Reference ledger for agent spend caps, escrow, and reputation that only counts settled jobs.

This is not an xAI, X, or SpaceXAI product. Grok Bot can already spend through Stripe Link with a human approval, and X Money can pay a template creator. Neither is a bot-to-bot escrow with a hard budget and a score built only from finished jobs. This repo is that missing join, as a library a Grok Build session can run.

No real funds move. Amounts are integer cents so a later adapter can map them to USDC or X Money.

## What it enforces

- A principal owns the mandate. The agent cannot raise its own cap.
- Per-job cap, daily cap, payee allow-list, and a freeze switch are hard refusals, not prompt text. Funding checks the daily cap again. The day is `YYYY-MM-DD`.
- Jobs over the approval threshold cannot be funded until the principal approves.
- Escrow runs open, funded, delivered, then released, refunded, or resolved.
- Reputation ignores anything that is not settled.

## Run

```bash
python -m agent_ledger demo
python -m pytest
```

The demo hires a listed skill for 800 cents, inside a 2000 cent job cap, and prints reputation plus P&L.

## Grok Build

Copy `skills/agent-ledger` into `~/.grok/skills/agent-ledger`, or point Grok Build at this repo. The skill tells the agent to check the mandate before it hires anything.

## License

MIT
