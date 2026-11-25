"""investor_bullets cron sweep: one product failing must not abort the rest.

Regression: the per-product except-branch called ``logger.warning`` but
mr_roboto/__init__.py defines no module-level ``logger`` — the NameError was
caught by the outer handler and failed the WHOLE sweep, the exact opposite
of the "one product must not abort" contract written next to it.
"""
from __future__ import annotations

import pytest


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows

    async def close(self):
        return None


class _DB:
    async def execute(self, *_a, **_k):
        return _Cur([("p-bad",), ("p-good",)])


@pytest.mark.asyncio
async def test_sweep_survives_one_failing_product(monkeypatch):
    import dabidabi
    import src.app.jobs.investor_bullets as ib
    import mr_roboto

    async def _get_db():
        return _DB()

    async def _run(product_id):
        if product_id == "p-bad":
            raise RuntimeError("boom")
        return {"ok": True}

    monkeypatch.setattr(dabidabi, "get_db", _get_db)
    monkeypatch.setattr(ib, "run_investor_bullets", _run)

    act = await mr_roboto._run_dispatch({"id": 1, "payload": {"action": "investor_bullets"}})
    assert act.status == "completed", act.error
    assert act.result == {"ok": True, "products_swept": 2, "products_ran": 1}
