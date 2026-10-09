from pathlib import Path

import pytest

from agent_ledger.config import apply_mandate, load_mandate_config
from agent_ledger.ledger import Ledger
from agent_ledger.models import PolicyError
from agent_ledger.store import JsonStore
from tests.support import ROOT, write_config


def test_example_config_opens_one_mandate():
    mandate = load_mandate_config(ROOT / "config.example.json")
    ledger = Ledger()
    assert apply_mandate(ledger, mandate) is True
    assert apply_mandate(ledger, mandate) is False
    assert list(ledger.mandates) == ["buyer-bot"]
    assert ledger.mandates["buyer-bot"].allowed_payees == ("research-bot",)
    assert sum(entry.action == "open_mandate" for entry in ledger.audit) == 1


def test_restart_updates_caps_and_freeze_as_principal(tmp_path):
    config_path = tmp_path / "config.json"
    ledger_path = tmp_path / "ledger.json"
    write_config(config_path)
    ledger = Ledger()
    assert apply_mandate(ledger, load_mandate_config(config_path)) is True
    JsonStore(ledger_path).save(ledger)

    write_config(
        config_path,
        max_per_job_cents=1500,
        max_daily_cents=4000,
        approval_above_cents=100,
        frozen=True,
    )
    reloaded = JsonStore(ledger_path).load()
    assert apply_mandate(reloaded, load_mandate_config(config_path)) is True
    mandate = reloaded.mandates["buyer-bot"]
    assert mandate.max_per_job_cents == 1500
    assert mandate.max_daily_cents == 4000
    assert mandate.approval_above_cents == 100
    assert mandate.frozen is True
    assert mandate.allowed_payees == ("research-bot",)
    cap_changes = [entry for entry in reloaded.audit if entry.action == "set_caps"]
    assert len(cap_changes) == 1
    assert cap_changes[0].actor == "joe"
    freeze = [entry for entry in reloaded.audit if entry.action == "freeze"]
    assert len(freeze) == 1
    assert freeze[0].actor == "joe"
    assert sum(entry.action == "open_mandate" for entry in reloaded.audit) == 1

    with pytest.raises(PolicyError, match="principal"):
        reloaded.set_caps("buyer-bot", "buyer-bot", 9_000, 9_000)
    assert reloaded.mandates["buyer-bot"].max_per_job_cents == 1500


def test_unchanged_allow_list_is_not_rewritten_on_reload(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    ledger = Ledger()
    apply_mandate(ledger, load_mandate_config(config_path))
    write_config(config_path, allowed_payees=["other-bot"])
    assert apply_mandate(ledger, load_mandate_config(config_path)) is False
    assert ledger.mandates["buyer-bot"].allowed_payees == ("research-bot",)


def test_file_principal_must_own_an_existing_mandate(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    ledger = Ledger()
    apply_mandate(ledger, load_mandate_config(config_path))
    write_config(config_path, principal_id="buyer-bot", max_per_job_cents=3_000)
    with pytest.raises(PolicyError, match="principal"):
        apply_mandate(ledger, load_mandate_config(config_path))
    assert ledger.mandates["buyer-bot"].max_per_job_cents == 2_000
    assert ledger.mandates["buyer-bot"].principal_id == "joe"


def test_same_file_on_disk_does_not_duplicate(tmp_path: Path):
    config_path = tmp_path / "config.json"
    ledger_path = tmp_path / "ledger.json"
    write_config(config_path)
    ledger = Ledger()
    apply_mandate(ledger, load_mandate_config(config_path))
    JsonStore(ledger_path).save(ledger)
    again = JsonStore(ledger_path).load()
    assert apply_mandate(again, load_mandate_config(config_path)) is False
    assert list(again.mandates) == ["buyer-bot"]
    assert [entry.action for entry in again.audit] == ["open_mandate"]
