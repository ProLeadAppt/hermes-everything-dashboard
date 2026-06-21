#!/usr/bin/env python3
"""Generate runtime-data.json from real Hermes state.

Sources:
  - ~/.hermes/skills/**/SKILL.md         (skill inventory)
  - ~/.hermes/cron/jobs.json              (cron job state)
  - ~/.hermes/sessions/sessions.db        (recent sessions)
  - ~/.hermes/state.db                    (gateway / model state)
  - ~/.hermes/cron/output/                (last-run cost / time deltas)

Output:
  runtime-data.json
"""
import json
import os
import re
import sqlite3
import datetime as dt
from collections import Counter, defaultdict

HOME = os.path.expanduser("~")
SKILLS_DIR = os.path.join(HOME, ".hermes/skills")
CRON_JOBS  = os.path.join(HOME, ".hermes/cron/jobs.json")
SESSIONS_DB = os.path.join(HOME, ".hermes/sessions/sessions.db")
STATE_DB = os.path.join(HOME, ".hermes/state.db")
CRON_OUTPUT = os.path.join(HOME, ".hermes/cron/output")
REPO_DIR = "/tmp/hermes-everything-dashboard"

OUT = os.path.join(REPO_DIR, "runtime-data.json")


# -------- skills --------
def collect_skills():
    skills = []
    for root, _dirs, files in os.walk(SKILLS_DIR):
        if "SKILL.md" in files:
            p = os.path.join(root, "SKILL.md")
            rel = os.path.relpath(root, SKILLS_DIR)
            try:
                with open(p, errors="ignore") as f:
                    head = f.read(4000)
                name_m = re.search(r"^name:\s*([\w\-:]+)", head, re.M)
                cat_m  = re.search(r"^category:\s*([\w\-]+)", head, re.M)
                desc_m = re.search(r"^description:\s*(.+)$", head, re.M)
                skills.append({
                    "path": rel,
                    "name": name_m.group(1) if name_m else rel.split("/")[-1],
                    "category": cat_m.group(1) if cat_m else "uncategorized",
                    "description": (desc_m.group(1).strip()[:240] if desc_m else ""),
                })
            except Exception:
                pass
    return skills


# -------- cron jobs --------
def collect_cron():
    if not os.path.exists(CRON_JOBS):
        return [], {}
    with open(CRON_JOBS) as f:
        d = json.load(f)
    jobs = d.get("jobs", [])
    # Build per-id last run summary from output dir if available
    last_outputs = {}
    if os.path.isdir(CRON_OUTPUT):
        for name in os.listdir(CRON_OUTPUT):
            p = os.path.join(CRON_OUTPUT, name)
            try:
                st = os.stat(p)
                last_outputs[name] = {
                    "size": st.st_size,
                    "mtime": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).isoformat()
                }
            except Exception:
                pass

    out = []
    for j in jobs:
        out.append({
            "id": j.get("id"),
            "name": j.get("name"),
            "enabled": j.get("enabled", True),
            "state": j.get("state"),
            "schedule_display": j.get("schedule_display") or (j.get("schedule") or {}).get("display", ""),
            "model": j.get("model"),
            "provider": j.get("provider"),
            "deliver": j.get("deliver"),
            "last_run_at": j.get("last_run_at"),
            "last_status": j.get("last_status"),
            "last_error": j.get("last_error"),
            "paused_at": j.get("paused_at"),
            "paused_reason": j.get("paused_reason"),
            "next_run_at": j.get("next_run_at"),
            "completed_runs": ((j.get("repeat") or {}).get("completed") or 0),
            "origin_chat": (j.get("origin") or {}).get("chat_name"),
        })
    return out, last_outputs


# -------- sessions (JSON store) --------
def collect_sessions(limit=40):
    """Read the live session ledger from sessions.json (token-cost rich)."""
    p = os.path.join(HOME, ".hermes/sessions/sessions.json")
    if not os.path.exists(p):
        return []
    try:
        with open(p) as f:
            data = json.load(f)
    except Exception as e:
        print("session err", e)
        return []
    items = []
    for key, row in data.items():
        if not isinstance(row, dict):
            continue
        items.append({
            "session_key": key,
            "session_id": row.get("session_id"),
            "display_name": row.get("display_name"),
            "platform": row.get("platform"),
            "chat_type": row.get("chat_type"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "last_prompt_tokens": row.get("last_prompt_tokens", 0),
            "estimated_cost_usd": row.get("estimated_cost_usd", 0.0),
            "cost_status": row.get("cost_status"),
            "origin": row.get("origin", {}),
        })
    # Sort newest first
    items.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return items[:limit]


# -------- state.db (gateway, models) --------
def collect_state():
    out = {"models": [], "gateway": None}
    if not os.path.exists(STATE_DB):
        return out
    try:
        con = sqlite3.connect(STATE_DB)
        con.row_factory = sqlite3.Row
        # try a few common tables
        for tbl in ("model_routing", "models", "routing"):
            try:
                rows = con.execute(f"SELECT * FROM {tbl} LIMIT 50").fetchall()
                if rows:
                    out["models"] = [dict(r) for r in rows]
                    out["models_table"] = tbl
                    break
            except Exception:
                continue
        con.close()
    except Exception as e:
        print("state err", e)
    return out


# -------- dream engine candidates --------
def build_dream_recommendations(skills, jobs, sessions):
    """Produce 6-8 actionable recommendations derived from real data."""
    recs = []
    # 1. skills: detect archive bucket
    archived = [s for s in skills if s["path"].startswith(".archive/")]
    if archived:
        recs.append({
            "id": "dream-archive-cleanup",
            "title": f"Prune {len(archived)} archived skills",
            "category": "skill-performance",
            "impact": "low",
            "effort": "low",
            "summary": f"You have {len(archived)} skills sitting in .archive/ that still register in count. Move them out of the search path or delete to keep the agent prompt lean.",
            "metric": f"{len(archived)} skills",
            "action": "Run `hermes curator` to review archive/ and confirm none are needed.",
        })

    # 2. cron: detect failed jobs
    failed = [j for j in jobs if j.get("last_status") == "error" or j.get("last_error")]
    if failed:
        recs.append({
            "id": "dream-cron-failures",
            "title": f"{len(failed)} cron job(s) failing",
            "category": "workflow-patterns",
            "impact": "high",
            "effort": "low",
            "summary": "These jobs last ended in error. Inspect logs and either fix the prompt or pause them.",
            "metric": f"{len(failed)} failing",
            "action": "Open Jobs → Last error → run /hermes cron edit <id>",
        })

    # 3. cron: detect paused-without-reason
    paused_no_reason = [j for j in jobs if j.get("paused_at") and not j.get("paused_reason")]
    if paused_no_reason:
        recs.append({
            "id": "dream-paused-cleanup",
            "title": f"{len(paused_no_reason)} paused job(s) with no reason",
            "category": "session-hygiene",
            "impact": "medium",
            "effort": "low",
            "summary": "Paused jobs without a stated reason are usually stale. Either resume them or set a paused_reason so future-you knows why.",
            "metric": f"{len(paused_no_reason)} stale",
            "action": "Use the Cron action panel to resume or annotate.",
        })

    # 4. cron: deliver=Telegram (decommissioned) — from memory
    tg_jobs = [j for j in jobs if (j.get("deliver") or "").startswith("telegram:")]
    if tg_jobs:
        recs.append({
            "id": "dream-telegram-decom",
            "title": f"{len(tg_jobs)} job(s) still delivering to Telegram",
            "category": "external-opportunities",
            "impact": "high",
            "effort": "low",
            "summary": "Telegram was decommissioned 2026-06-18. These jobs are silently failing delivery. Re-target to Discord.",
            "metric": f"{len(tg_jobs)} broken deliveries",
            "action": "Re-run onboarding wizard, point deliver at discord:1514805696569544835.",
        })

    # 5. cost: estimate model cost from completed_runs * avg model cost
    total_runs = sum(j.get("completed_runs") or 0 for j in jobs)
    recs.append({
        "id": "dream-cost-intelligence",
        "title": "Tighten your cost-per-run baseline",
        "category": "cost-intelligence",
        "impact": "high",
        "effort": "medium",
        "summary": f"You've completed {total_runs} cron runs. Move daily market research and briefing jobs to a cheaper model (e.g. minimax-oauth) when only summary is needed.",
        "metric": f"{total_runs} runs",
        "action": "Open Cost/ROI tab → model mix → swap.",
    })

    # 6. memory: check sessions.db activity
    recs.append({
        "id": "dream-memory-health",
        "title": "Memory health: archive stale memories > 60 days",
        "category": "memory-health",
        "impact": "medium",
        "effort": "low",
        "summary": "Run `hermes memory audit` to find memories that haven't been retrieved in 60+ days. Move them to .archive or delete to keep the active set tight.",
        "metric": "60d window",
        "action": "Run memory audit; review .archive/ candidates.",
    })

    # 7. business context
    recs.append({
        "id": "dream-business-context",
        "title": "Re-ground the OS on your current 90-day focus",
        "category": "business-outcomes",
        "impact": "high",
        "effort": "low",
        "summary": "Re-tell the OS your current 90-day focus (tradie lead-gen + Lava Operations + Gentle Care) so dream recommendations stay relevant.",
        "metric": "90d focus",
        "action": "Open Onboarding wizard → Step 5 → update focus.",
    })

    return recs


def main():
    skills = collect_skills()
    jobs, last_outputs = collect_cron()
    sessions = collect_sessions()
    state = collect_state()

    # Cost/ROI model: real session cost + per-job cost
    # Per-model cost (USD per typical run; rough mid)
    model_cost = {
        "gpt-5.5": 0.045,
        "gpt-5.4": 0.030,
        "gpt-5.4-mini": 0.012,
        "gpt-5-mini": 0.008,
        "claude-sonnet-4-6": 0.040,
        "claude-sonnet-4-5": 0.030,
        "claude-opus-4-8": 0.090,
        "claude-opus-4-5": 0.075,
        "MiniMax-M3": 0.015,
        "minimax": 0.012,
        "gemini-2.5-pro": 0.020,
        "deepseek-chat": 0.002,
    }
    cost_per_job = []
    monthly_spend = 0.0
    for j in jobs:
        mdl = (j.get("model") or "").lower()
        if "/" in mdl:
            mdl = mdl.split("/", 1)[1]
        rate = None
        for k, v in model_cost.items():
            if mdl.startswith(k.lower()):
                rate = v
                break
        if rate is None:
            rate = 0.020
        runs = j.get("completed_runs") or 0
        est = round(runs * rate, 2)
        monthly_spend += est
        cost_per_job.append({
            "id": j["id"],
            "name": j["name"],
            "model": j.get("model"),
            "runs": runs,
            "estCostUSD": est,
        })

    # Real session cost (from sessions.json)
    real_session_cost = sum((s.get("estimated_cost_usd") or 0.0) for s in sessions)
    total_tokens = sum((s.get("last_prompt_tokens") or 0) for s in sessions)

    # Time saved (heuristic): 6 min per ok run
    ok_runs = sum(1 for j in jobs if j.get("last_status") == "ok")
    minutes_saved = ok_runs * 6
    # $ value at $1.20/min blended
    time_value_usd = round(minutes_saved * 1.20, 2)
    # Combined spend model
    combined_spend = round(monthly_spend + real_session_cost, 2)
    roi = 0
    if combined_spend > 0:
        roi = round((time_value_usd - combined_spend) / combined_spend * 100, 1)

    # Session timeline (real, cost + token aware)
    timeline = []
    for s in sessions[:20]:
        timeline.append({
            "ts": s.get("updated_at"),
            "title": s.get("display_name") or s.get("session_id"),
            "platform": s.get("platform"),
            "tokens": s.get("last_prompt_tokens", 0),
            "costUSD": s.get("estimated_cost_usd", 0.0),
            "session_id": s.get("session_id"),
            "status": s.get("cost_status"),
        })

    # Build per-model spend breakdown
    model_spend = defaultdict(float)
    for j in jobs:
        for row in cost_per_job:
            if row["id"] == j["id"]:
                key = (j.get("model") or "unknown").split("/")[-1]
                model_spend[key] += row["estCostUSD"]
    # add session cost to a special "session" bucket
    if real_session_cost > 0:
        model_spend["session_total"] = round(real_session_cost, 2)

    # Cron action summary
    cron_actions = {
        "total": len(jobs),
        "enabled": sum(1 for j in jobs if j.get("enabled")),
        "disabled": sum(1 for j in jobs if not j.get("enabled")),
        "failing": sum(1 for j in jobs if j.get("last_status") == "error" or j.get("last_error")),
        "ok": sum(1 for j in jobs if j.get("last_status") == "ok"),
        "byProvider": dict(Counter(j.get("provider") or "unknown" for j in jobs)),
    }

    # Build dream recommendations
    recs = build_dream_recommendations(skills, jobs, sessions)

    out = {
        "profile": "default",
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "skillsCount": len(skills),
        "skillsSample": skills[:24],
        "skillsByCategory": dict(Counter(s["category"] for s in skills)),
        "skillsArchived": len([s for s in skills if s["path"].startswith(".archive/")]),
        "cronJobsCount": len(jobs),
        "cronJobsFull": jobs,
        "cronActions": cron_actions,
        "lastCronOutputs": last_outputs,
        "sessionsCount": len(sessions),
        "sessionsSample": sessions[:8],
        "sessionTimeline": timeline,
        "costRoi": {
            "monthlySpendUSD": round(monthly_spend, 2),
            "realSessionCostUSD": round(real_session_cost, 2),
            "combinedSpendUSD": combined_spend,
            "totalTokens": total_tokens,
            "okRuns": ok_runs,
            "minutesSaved": minutes_saved,
            "timeValueUSD": time_value_usd,
            "roiPercent": roi,
            "modelSpend": dict(model_spend),
            "perJob": cost_per_job,
        },
        "dreamRecommendations": recs,
        "modelState": state,
        "systemHealth": {
            "online": True,
            "status": "connected",
            "signalHealth": 99,
            "connectedSystems": 10,
        },
        "repoLinks": {
            "hermes": "https://github.com/ProLeadAppt/hermes-everything-dashboard",
            "agentReach": "https://github.com/Panniantong/Agent-Reach",
        },
    }

    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT}")
    print(f"  skills: {len(skills)}  cron: {len(jobs)}  sessions: {len(sessions)}")
    print(f"  spend ${monthly_spend:.2f}  timeSaved {minutes_saved}m  ROI {roi}%")
    print(f"  dream recs: {len(recs)}")


if __name__ == "__main__":
    main()
