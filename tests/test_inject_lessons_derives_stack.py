"""P2b — the inject_lessons executor self-derives the stack (i2p base / tech stack)."""
from __future__ import annotations
import json
import pytest


async def _setup_db(tmp_path, monkeypatch, mission_ctx: dict):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    import dabidabi as _db_mod
    monkeypatch.setattr(_db_mod, "DB_PATH", db_path)
    if _db_mod._db_connection is not None:
        await _db_mod._db_connection.close()
        _db_mod._db_connection = None
    from dabidabi import init_db, get_db
    await init_db()
    db = await get_db()
    await db.execute(
        "INSERT INTO missions (id, title, status, context) VALUES (9, 't', 'active', ?)",
        (json.dumps(mission_ctx),),
    )
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_derives_i2p_base_and_finds_lesson(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path, monkeypatch, {"workflow_name": "i2p_v3"})   # greenfield: no tech stack, but i2p
    from kara_kutu import capture, error_domain_key
    await capture(error_domain_key("i2p", "grounding"), task_id=1, mission_id=9,
                  pattern="grounding gate exhausted", fix="ground writes", severity="blocker")

    from mr_roboto.inject_lessons import inject_lessons
    res = await inject_lessons(mission_id=9, stack="", domain=None, limit=5)
    assert res["lessons_count"] >= 1
    async with db.execute("SELECT context FROM missions WHERE id=9") as cur:
        row = await cur.fetchone()
    pats = [l["pattern"] for l in json.loads(row[0])["lessons_top_n"]]
    assert any("grounding gate exhausted" in p for p in pats)


@pytest.mark.asyncio
async def test_derives_tech_stack_when_present(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path, monkeypatch, {"workflow_name": "i2p_v3", "tech_stack_detected": "fastapi"})
    from kara_kutu import capture, error_domain_key
    await capture(error_domain_key("fastapi", "migration"), task_id=1, mission_id=9,
                  pattern="alembic head mismatch", fix="stamp head", severity="warning")

    from mr_roboto.inject_lessons import inject_lessons
    res = await inject_lessons(mission_id=9, stack="", domain=None, limit=5)
    assert res["lessons_count"] >= 1
    async with db.execute("SELECT context FROM missions WHERE id=9") as cur:
        row = await cur.fetchone()
    pats = [l["pattern"] for l in json.loads(row[0])["lessons_top_n"]]
    assert any("alembic head mismatch" in p for p in pats)
