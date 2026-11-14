import importlib
import logging

import pytest


SETTINGS = [
    ("MAX_AGE_SECONDS", "max_age_seconds", 3600),
    ("SWEEP_MIN_GRACE_SECONDS", "min_grace_seconds", 60),
    ("SWEEP_MAX_ENTRIES", "max_entries", 10000),
    ("SWEEP_MAX_REMOVALS", "max_removals", 500),
    ("SWEEP_MAX_RUN_DIRECTORIES", "max_run_directories", 1000),
    ("SWEEP_BUDGET_SECONDS", "budget_seconds", 20),
]


@pytest.mark.parametrize("suffix,field,default", SETTINGS)
@pytest.mark.parametrize("raw", ["garbage", "-1", "1.5", ""])
def test_invalid_setting_falls_back_independently(suffix, field, default, raw, monkeypatch, caplog) -> None:
    # Given one invalid setting alongside a valid non-default setting.
    startup = importlib.import_module("tasks.render_scratch_startup")
    sweeper = importlib.import_module("creator_service.render_scratch_sweeper")
    monkeypatch.setenv("RENDER_SCRATCH_SWEEP_ENABLED", "true")
    monkeypatch.setenv("RENDER_SCRATCH_SWEEP_MAX_REMOVALS", "777")
    monkeypatch.setenv("RENDER_SCRATCH_SWEEP_MAX_ENTRIES", "888")
    setting = f"RENDER_SCRATCH_{suffix}"
    monkeypatch.setenv(setting, raw)
    policies = []
    monkeypatch.setattr(sweeper, "sweep_render_scratch", lambda root, policy: policies.append(policy))
    # When startup parses its policy.
    startup.sweep_at_start()
    # Then only this field falls back, with one setting-named warning.
    assert len(policies) == 1
    assert getattr(policies[0], field) == default
    other = "max_entries" if field == "max_removals" else "max_removals"
    assert getattr(policies[0], other) == (888 if other == "max_entries" else 777)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING and setting in r.message]) == 1


@pytest.mark.parametrize("suffix,field,default", SETTINGS)
def test_zero_setting_only_disables_maximum_age(suffix, field, default, monkeypatch, caplog) -> None:
    # Given a zero integer for each setting.
    startup = importlib.import_module("tasks.render_scratch_startup")
    sweeper = importlib.import_module("creator_service.render_scratch_sweeper")
    setting = f"RENDER_SCRATCH_{suffix}"
    monkeypatch.setenv(setting, "0")
    policies = []
    monkeypatch.setattr(sweeper, "sweep_render_scratch", lambda root, policy: policies.append(policy))
    # When startup parses zero.
    startup.sweep_at_start()
    # Then age can be disabled but admission budgets retain safe defaults.
    age = field == "max_age_seconds"
    assert getattr(policies[0], field) == (0 if age else default)
    assert len([r for r in caplog.records if setting in r.message]) == (0 if age else 1)


@pytest.mark.parametrize("raw,enabled,warnings", [
    ("1", True, 0), ("yes", True, 0), ("TRUE", True, 0), ("YeS", True, 0),
    ("0", False, 0), ("no", False, 0), ("FALSE", False, 0),
    ("garbage", True, 1), ("", True, 1),
])
def test_enabled_setting_uses_repository_boolean_convention(raw, enabled, warnings, monkeypatch, caplog) -> None:
    # Given an explicit boolean or invalid value.
    startup = importlib.import_module("tasks.render_scratch_startup")
    sweeper = importlib.import_module("creator_service.render_scratch_sweeper")
    monkeypatch.setenv("RENDER_SCRATCH_SWEEP_ENABLED", raw)
    policies = []
    monkeypatch.setattr(sweeper, "sweep_render_scratch", lambda root, policy: policies.append(policy))
    # When the startup hook runs.
    startup.sweep_at_start()
    # Then invalid values preserve enabled-by-default reclamation.
    assert len(policies) == int(enabled)
    assert len([r for r in caplog.records if "RENDER_SCRATCH_SWEEP_ENABLED" in r.message]) == warnings
