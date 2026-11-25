"""Mechanical dispatch table — the routing that replaced the 4.8k-line if-chain."""
from __future__ import annotations

import inspect

import pytest

import mr_roboto


def test_every_route_is_an_async_handler_of_the_same_signature():
    for action, handler in mr_roboto._ACTION_HANDLERS.items():
        assert inspect.iscoroutinefunction(handler), action
        assert list(inspect.signature(handler).parameters) == ["task", "payload", "action"], action
    for prefix, handler in mr_roboto._PREFIX_HANDLERS:
        assert inspect.iscoroutinefunction(handler), prefix


def test_grouped_actions_share_one_handler():
    t = mr_roboto._ACTION_HANDLERS
    assert t["backup_verify"] is t["cron_backup_verify"]
    assert t["cve_scan"] is t["cron_cve_scan"]


def test_no_exact_action_is_shadowed_by_a_prefix():
    for action in mr_roboto._ACTION_HANDLERS:
        for prefix, _ in mr_roboto._PREFIX_HANDLERS:
            assert not action.startswith(prefix), (action, prefix)


def test_dispatchable_actions_covers_table_and_prefixes():
    acts = mr_roboto.dispatchable_actions()
    assert set(mr_roboto._ACTION_HANDLERS) <= acts
    assert {"reviews/poll/", "mention_polls/"} <= acts


@pytest.mark.asyncio
async def test_unknown_and_missing_action_fail_cleanly():
    for payload in ({"action": "no_such_action"}, {}, {"action": None}, {"action": ""}):
        act = await mr_roboto._run_dispatch({"id": 1, "payload": payload})
        assert act.status == "failed"
        assert "unknown mechanical action" in act.error


@pytest.mark.asyncio
async def test_exact_then_prefix_routing(monkeypatch):
    seen = []

    async def _exact(task, payload, action):
        seen.append(("exact", action))
        return mr_roboto.Action(status="completed")

    async def _pref(task, payload, action):
        seen.append(("prefix", action))
        return mr_roboto.Action(status="completed")

    monkeypatch.setitem(mr_roboto._ACTION_HANDLERS, "zz_exact", _exact)
    monkeypatch.setattr(mr_roboto, "_PREFIX_HANDLERS", (("zz/", _pref),))
    await mr_roboto._run_dispatch({"payload": {"action": "zz_exact"}})
    await mr_roboto._run_dispatch({"payload": {"action": "zz/anything"}})
    # context-carried payload is honoured too (same prelude as before)
    await mr_roboto._run_dispatch({"context": {"payload": {"action": "zz_exact"}}})
    assert seen == [("exact", "zz_exact"), ("prefix", "zz/anything"), ("exact", "zz_exact")]
