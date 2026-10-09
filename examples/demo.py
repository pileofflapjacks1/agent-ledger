"""Run a capped hire against a listed skill. No real funds move."""

from agent_ledger.__main__ import demo

if __name__ == "__main__":
    import json
    print(json.dumps(demo(), indent=2))
