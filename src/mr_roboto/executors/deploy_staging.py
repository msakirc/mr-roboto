"""Mechanical deploy_staging orchestrator — stands up a $0 staging env, writes 7.13 artifacts.

Reaches adapters via DIRECT adapter.execute() (NOT mr_roboto vendor_call — that wrapper
strips the ``mocked`` flag the anti-fake guard depends on). A mock response can never
certify health_check_passed:true.
"""
from __future__ import annotations
import asyncio, os, shutil, sys
from typing import Any

from yazbunu import get_logger

logger = get_logger("mr_roboto.deploy_staging")

# Render deploy statuses that are TERMINAL failures. Live retrieve-deploy → deployStatus enum
# (2026-05): created, queued, build_in_progress, update_in_progress, live, deactivated,
# build_failed, update_failed, canceled, pre_deploy_in_progress, pre_deploy_failed.
_RENDER_FAIL_STATES = frozenset({
    "build_failed", "update_failed", "canceled", "deactivated", "pre_deploy_failed",
})

# $0 HARD GUARANTEE. This whole capability exists to stand up staging at ZERO cost on
# no-card accounts. Only these Render plan values are billing-free; anything else is a
# paid instance and is REFUSED before any API call — no silent paid provision, no fallback
# from an unavailable free tier to a paid one. A founder can deliberately opt into paid via
# KUTAI_ALLOW_PAID_DEPLOY=1 (breaks the $0 guarantee); absent that, paid fails CLOSED.
_FREE_RENDER_PLANS = frozenset({"free"})

def _paid_deploys_allowed() -> bool:
    return os.getenv("KUTAI_ALLOW_PAID_DEPLOY") == "1"

def _is_mocked(envelope: dict) -> bool:
    """True when a registry response carries the mock tag."""
    return bool(isinstance(envelope, dict) and envelope.get("mocked") is True)

async def _call(service: str, action: str, params: dict) -> dict:
    """Direct adapter.execute(); preserves the mocked flag."""
    from src.integrations.registry import get_integration_registry
    adapter = get_integration_registry().get(service)
    if adapter is None:
        return {"status": "error", "error": f"adapter {service} not registered"}
    return await adapter.execute(action, params)

def _fail(reason: str, **extra) -> dict:
    return {"ok": False, "reason": reason, **extra}

async def _repo_from_mission(mission_id) -> str | None:
    """Read the pushed repo URL persisted by Plan-1 git_prepare_repo (missions.github_repo_url).
    The payload's {git_repo_url} placeholder is NOT substituted at expansion (only {mission_id}
    is) — Opus review Bug 1. Resolve from the mission row instead."""
    try:
        from dabidabi import get_db
        db = await get_db()
        cur = await db.execute("SELECT github_repo_url FROM missions WHERE id = ?", (mission_id,))
        row = await cur.fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None

async def _resolve_workspace(mission_id, payload_ws) -> str | None:
    """Absolute mission workspace. A payload-relative 'mission_{id}/' would resolve against the
    process CWD (Opus review Bug 2) — re-root under WORKSPACE_DIR via get_mission_workspace."""
    if payload_ws and os.path.isabs(payload_ws):
        return payload_ws
    if mission_id is not None:
        from src.tools.workspace import get_mission_workspace
        return get_mission_workspace(int(mission_id))
    return payload_ws

async def run(task: dict) -> dict[str, Any]:
    payload = (task.get("payload") or (task.get("context") or {}).get("payload") or {})
    backend_arch = payload.get("backend_arch", "nestjs_render")
    if backend_arch != "nestjs_render":
        return _fail("serverless_not_yet_supported")
    ctx = task.get("context") or {}
    mission_id = ctx.get("mission_id") or payload.get("mission_id")

    repo = payload.get("repo")
    if (not repo or "{" in str(repo)) and mission_id is not None:
        repo = await _repo_from_mission(mission_id)
    if not repo or "{" in str(repo):
        return _fail("missing repo (no github_repo_url on mission)")

    workspace = await _resolve_workspace(mission_id, payload.get("workspace"))
    if not workspace:
        return _fail("missing workspace")

    state = {"mocked_any": False, "services": {}, "provisioned": []}

    prov = await _provision(mission_id)
    if not prov["ok"]:
        return prov
    state["mocked_any"] |= prov.get("mocked", False)
    state["services"].update(prov["services"])
    logger.info("deploy_staging: provision leg complete (mocked=%s)", prov.get("mocked", False))

    be = await _deploy_backend(
        repo=repo, env=prov["env"], owner_id=payload.get("owner_id", ""),
        plan=payload.get("render_plan", "free"),
        build_command=payload.get("build_command", "npm install && npm run build"),
        start_command=payload.get("start_command", "npm run start:prod"),
    )
    if not be["ok"]:
        return {**be, "state": state}
    state["mocked_any"] |= be.get("mocked", False)
    state["services"].update(be["services"])
    logger.info("deploy_staging: backend leg complete (mocked=%s)", be.get("mocked", False))

    mig = await _migrate(workspace=workspace, database_url=prov["env"]["DATABASE_URL"])
    if not mig["ok"]:
        return {**mig, "state": state}

    fe = await _deploy_frontend(repo=repo, backend_url=be["url"])
    if not fe["ok"]:
        return {**fe, "state": state}
    state["mocked_any"] |= fe.get("mocked", False)
    state["services"].update(fe["services"])
    logger.info("deploy_staging: frontend leg complete (mocked=%s)", fe.get("mocked", False))

    # Skip the real HTTP health check on a mock run — the result is discarded anyway.
    hc = {} if state["mocked_any"] else await _health_check(be["url"])

    # Anti-fake guard: a mocked run can NEVER certify a live deploy.
    if state["mocked_any"]:
        logger.warning("deploy_staging: anti-fake guard fired — mocked_any, refusing to certify")
        health_passed, reason = False, "mock_mode_active"
    else:
        health_passed, reason = bool(hc.get("passed")), (None if hc.get("passed") else "health_check_failed")

    artifacts = {
        "staging_environment": {"url": fe["url"], "services": state["services"]},
        "staging_deployment_verified": {"deployed": True, "health_check_passed": health_passed,
                                        "reason": reason},
    }
    ok = health_passed
    return {"ok": ok, "artifacts": artifacts, "state": state,
            "reason": None if ok else reason}


async def _provision(mission_id) -> dict:
    """Provision Neon Postgres + Upstash Redis; return env vars + service descriptors.

    Idempotent: list-before-create keyed on a mission tag would go here (list_* returns []
    in mock; real impl reuses a resource named kutay_mission_{id}). Returns
    {ok, env:{DATABASE_URL,REDIS_URL}, services:{db,cache}, mocked}.
    """
    name = f"kutay_mission_{mission_id}"
    db = await _call("neon", "create_project", {"project": {"name": name}})
    if db.get("status") != "ok":
        return _fail("neon_provision_failed", detail=db.get("error"))
    conn = (db.get("data", {}).get("connection_uris") or [{}])[0].get("connection_uri")
    if not conn:
        return _fail("neon_no_connection_uri")

    # Upstash Developer API POST /v2/redis/database requires database_name + platform + primary_region
    # (live docs: create_database_global — `region` is NOT a request field; the `region:"global"`
    #  in the response is a region-TYPE). upstash.json required_params=[database_name, primary_region];
    #  we pass primary_region (config guard) AND platform (real API) for a complete, valid body.
    cache = await _call("upstash", "create_redis",
                        {"database_name": name, "primary_region": "us-east-1", "platform": "aws"})
    if cache.get("status") != "ok":
        return _fail("upstash_provision_failed", detail=cache.get("error"))
    cd = cache.get("data", {})
    redis_url = f"rediss://default:{cd.get('password')}@{cd.get('endpoint')}:{cd.get('port')}"

    return {
        "ok": True,
        "env": {"DATABASE_URL": conn, "REDIS_URL": redis_url},
        "services": {"db": {"provider": "neon"}, "cache": {"provider": "upstash"}},
        "mocked": _is_mocked(db) or _is_mocked(cache),
    }


async def _deploy_backend(repo: str, env: dict, *, owner_id: str = "", plan: str = "free",
                          build_command: str = "npm install && npm run build",
                          start_command: str = "npm run start:prod",
                          max_wait_s: float = 900) -> dict:
    """Create the Render web service (env vars at create → auto-deploys once), then poll the
    initial deploy until 'live'. Render create auto-initiates the first deploy — no separate
    trigger_deploy for first boot.

    Body reconciled against the live Render OpenAPI (create-service, 2026-05):
      * ``envVars`` is a TOP-LEVEL field of servicePOST — NOT nested under serviceDetails.
      * ``serviceDetails.runtime`` is REQUIRED; the old ``env`` field is deprecated
        ("runtime should be used in its place").
      * ``serviceDetails.plan`` defaults to the PAID ``starter`` when omitted, so we send it
        explicitly. Only free plans are allowed (``_FREE_RENDER_PLANS``) — a non-free plan is
        refused BEFORE the API call (fail-closed $0 guarantee). ``plan="free"`` also fails LOUD
        (HTTP 400) if Render's free web tier is unavailable — that is the correct outcome; we
        never fall back to a paid instance.
      * a node runtime needs ``envSpecificDetails.{buildCommand,startCommand}`` to build + boot
        (defaults suit the declared nestjs_render backend; override via payload).
    Create returns the ``serviceAndDeploy`` envelope ``{service:{...}, deployId}`` — poll that
    real ``deployId`` (there is no "latest" alias).
    """
    from mr_roboto.deploy_util import poll_until
    # $0 hard stop: never request a billable Render instance (see _FREE_RENDER_PLANS).
    if plan not in _FREE_RENDER_PLANS and not _paid_deploys_allowed():
        return _fail("refuse_paid_plan", plan=plan,
                     hint="deploy is $0-only; set KUTAI_ALLOW_PAID_DEPLOY=1 to override (breaks the $0 guarantee)")
    env_vars = [{"key": k, "value": v} for k, v in env.items()]
    create = await _call("render", "create_service", {
        "ownerId": owner_id, "type": "web_service", "name": "kutay-backend", "repo": repo,
        "envVars": env_vars,                        # top-level per servicePOST
        "serviceDetails": {
            "runtime": "node",                      # required; `env` is deprecated
            "plan": plan,                           # explicit; "free" fails loud (never silent-paid)
            "envSpecificDetails": {"buildCommand": build_command, "startCommand": start_command},
        },
    })
    if create.get("status") != "ok":
        return _fail("render_create_failed", detail=create.get("error"))
    data = create.get("data", {}) or {}
    svc = data.get("service", {}) or {}
    sid = svc.get("id")
    url = (svc.get("serviceDetails") or {}).get("url")
    deploy_id = data.get("deployId")                # initial deploy id (serviceAndDeploy schema)
    if not deploy_id:
        return _fail("render_no_deploy_id", service_id=sid)

    poll = await poll_until(
        lambda: _deploy_state(sid, deploy_id),
        ready=lambda r: r.get("status") == "live",
        fail=lambda r: r.get("status") in _RENDER_FAIL_STATES,
        max_wait_s=max_wait_s,
    )
    if not poll["ok"]:
        return _fail(f"backend_deploy_{poll['reason']}", service_id=sid)
    mocked = _is_mocked(create) or bool((poll.get("result") or {}).get("mocked"))
    return {"ok": True, "url": url, "service_id": sid,
            "mocked": mocked, "services": {"backend": {"provider": "render", "url": url}}}

async def _deploy_state(service_id: str, deploy_id: str) -> dict:
    """Fetch a specific deploy's state for polling (GET /services/{id}/deploys/{deployId})."""
    d = await _call("render", "get_deploy", {"id": service_id, "deployId": deploy_id})
    st = d.get("data", {}) or {}
    if _is_mocked(d):                               # preserve mocked so the caller's guard sees it
        st = {**st, "mocked": True}
    return st


def _resolve_exe(name: str) -> str:
    """Windows-safe exe resolution: create_subprocess_exec does NOT use the shell, so bare 'npx'
    won't find 'npx.cmd' → FileNotFoundError on Windows (Opus review Bug 3). Resolve via
    shutil.which, fall back to '<name>.cmd' on win32."""
    found = shutil.which(name) or (shutil.which(f"{name}.cmd") if sys.platform == "win32" else None)
    return found or (f"{name}.cmd" if sys.platform == "win32" else name)

async def _shell(cmd: list[str], cwd: str, env: dict) -> dict:
    exe = _resolve_exe(cmd[0])
    proc = await asyncio.create_subprocess_exec(
        exe, *cmd[1:], cwd=cwd, env={**os.environ, **env},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return {"returncode": proc.returncode,
            "stdout": out.decode(errors="replace"), "stderr": err.decode(errors="replace")}

async def _migrate(workspace: str, database_url: str) -> dict:
    """Run `npx prisma migrate deploy` in the backend dir against the provisioned DATABASE_URL.

    NOTE (Opus review): prefer reusing `mr_roboto.run_cmd` (see `expo_cli.py:121`/`eas_build.py:162`
    for the call shape) — it centralizes workspace-rooting + missing-exe soft-skip; read its
    signature before adopting. The self-contained `_shell` above is the fallback. **Prereq:** the
    backend's `node_modules` (prisma CLI + generated client) must be installed on the host — add an
    `npm ci` preflight or ensure the runbook installs deps, else the live run fails with
    'prisma: not found'. (Alternative: run migrations as a Render release command.)
    """
    backend = os.path.join(workspace, "backend")
    if not os.path.isdir(backend):
        return _fail("backend_dir_missing", path=backend)
    res = await _shell(["npx", "prisma", "migrate", "deploy"], cwd=backend,
                       env={"DATABASE_URL": database_url})
    if res["returncode"] != 0:
        return _fail("migration_failed", detail=res["stderr"][:300])
    return {"ok": True, "output": res["stdout"][:300]}


async def _deploy_frontend(repo: str, backend_url: str, *, max_wait_s: float = 600) -> dict:
    from mr_roboto.deploy_util import poll_until
    dep = await _call("vercel", "deploy", {
        "name": "kutay-frontend",
        "gitSource": {"type": "github", "repo": repo},
        "env": {"NEXT_PUBLIC_API_URL": backend_url},
    })
    if dep.get("status") != "ok":
        return _fail("vercel_deploy_failed", detail=dep.get("error"))
    dep_id = dep.get("data", {}).get("id")

    async def fetch():
        r = await _call("vercel", "get_deployment", {"id": dep_id})
        d = r.get("data", {})
        if _is_mocked(r):
            d = {**d, "mocked": True}
        return d
    poll = await poll_until(fetch, ready=lambda r: r.get("readyState") == "READY",
                            fail=lambda r: r.get("readyState") in ("ERROR", "CANCELED"),
                            max_wait_s=max_wait_s)
    if not poll["ok"]:
        return _fail(f"frontend_deploy_{poll['reason']}", deployment_id=dep_id)
    url = poll["result"].get("url") or dep.get("data", {}).get("url")
    full = url if str(url).startswith("http") else f"https://{url}"
    mocked = _is_mocked(dep) or bool((poll.get("result") or {}).get("mocked"))
    return {"ok": True, "url": full, "mocked": mocked,
            "services": {"frontend": {"provider": "vercel", "url": full}}}


async def _http_get(url: str) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(url)
        return {"status_code": r.status_code}

async def _health_check(url: str, *, attempts: int = 6, delay_s: float = 10) -> dict:
    """GET the public URL; treat 502/503/timeout (cold-start-during-wake) as retryable.
    Render free services spin down on idle — the first request after deploy can cold-start."""
    last = None
    for i in range(attempts):
        try:
            last = await _http_get(url)
            code = last.get("status_code")
            if 200 <= code < 400:
                return {"ok": True, "passed": True, "status_code": code}
            if code not in (502, 503, 504):   # non-retryable server/client error
                return {"ok": True, "passed": False, "status_code": code}
        except Exception as e:                  # incl. SSRF ValueError / timeout → retry as "not yet public"
            last = {"error": str(e)}
        if i < attempts - 1:
            await asyncio.sleep(delay_s)
    return {"ok": True, "passed": False, "last": last}
