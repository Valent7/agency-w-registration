"""Stagirite's 00:00 Moscow nightly quality watch.

Safe, bounded self-healing only. Never sends messages, alters AI models or
budgets, changes the owner's chosen audience, deletes contact history, grants
access, books meetings, or deploys code. Unresolved issues remain visible in
the owner's audit report.

Source of truth: encrypted agency_workspace_states and existing
agency_stagirite_tasks. Per-owner report lives in one nightly task per date.
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from zoneinfo import ZoneInfo

from neonia_daily_source import owner_source, selected_chat_details, source_signature, matches_daily_batch

MSK = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc
RECHECK_HOURS = (0, 1, 3, 5)
REPORT_KIND = "night_audit"
ROOT = Path(__file__).resolve().parent

AGENT_POLICY_FILES = {
    "Неона": ("NEONA_CONSTITUTION.txt", "neona_dialog_policy_v3.py"),
    "Неония": ("neonia_candidate_policy.py", "neonia_chat_daily_worker.py"),
    "Неола": ("neola_role_policy.py", "neola_cabinet_map.py"),
    "Тео": ("THEO_CONSTITUTION.txt", "THEO_KNOWLEDGE_BASE.txt"),
    "Стагирит": ("stagirite_center.py", "agency_core.py"),
}


def schedule_slot(now: datetime) -> tuple[str, date] | None:
    """00/01/03/05 Moscow with bounded catch-up until 06:00 local Moscow."""
    local = now.astimezone(MSK)
    if local.hour >= 6:
        return None
    recent = [h for h in RECHECK_HOURS if h <= local.hour]
    if not recent:
        return None
    slot = f"{recent[-1]:02d}:00"
    return slot, local.date()


def check_policy_presence(root: Path = ROOT, env=None) -> list[dict]:
    """Presence is necessary, NOT proof that the AI is intelligent."""
    env = os.environ if env is None else env
    results = []
    for agent, filenames in AGENT_POLICY_FILES.items():
        missing = [name for name in filenames
                   if not (root / name).is_file()
                   or (root / name).stat().st_size < 100]
        if missing:
            results.append({"agent":agent,"status":"critical",
                            "code":"policy_missing","details":missing})
        else:
            results.append({"agent":agent,"status":"ok",
                            "code":"policy_files_present"})
    if not env.get("OPENAI_API_KEY"):
        results.append({"agent":"ИИ","status":"critical",
                        "code":"openai_key_missing"})
    # Read configuration; do not infer actual reasoning quality from model name.
    primary = str(env.get("NEONA_DIALOG_MODEL") or "gpt-6.1-sol").strip()
    neola = "gpt-realtime"
    theo = str(env.get("THEO_MODEL") or "gpt-5.6").strip()
    results.extend([
        {"agent":"Неона","status":"needs_live_quality_test",
         "code":"configured_model","model":primary},
        {"agent":"Неола","status":"needs_live_quality_test",
         "code":"configured_voice_model","model":neola},
        {"agent":"Тео","status":"needs_live_quality_test",
         "code":"configured_model","model":theo},
        {"agent":"Неония","status":"needs_live_quality_test",
         "code":"relevance_not_proven_by_model_name"},
        {"agent":"Стагирит","status":"needs_live_quality_test",
         "code":"reasoning_not_proven_by_model_name"},
    ])
    return results


def _ints(seq) -> list[int]:
    found = []
    for v in seq if isinstance(seq, list) else []:
        try:
            i = int(v)
        except (ValueError,TypeError):
            continue
        if i > 0:
            found.append(i)
    return found


def latest_batch(tasks: list[dict], day: date) -> dict:
    daykey = day.isoformat()
    latest = {}
    timestamp = ""
    for task in tasks:
        if not isinstance(task, dict) or task.get("task_kind")==REPORT_KIND:
            continue
        result = task.get("result") if isinstance(task.get("result"),dict) else {}
        for bucket in ("daily_candidate_feed","weekly_goal"):
            body = result.get(bucket)
            days = body.get("daily_batches") if isinstance(body,dict) else None
            b = (days or {}).get(daykey) if isinstance(days,dict) else None
            if isinstance(b,dict) and str(b.get("prepared_at") or "") >= timestamp:
                timestamp = str(b.get("prepared_at") or "")
                latest = b
    return latest


def evaluate_owner(state: dict, tasks: list[dict], day: date) -> dict:
    """Pure audit: no real telegram network, no model bill, no guessed fixes."""
    mode, chat_id = owner_source(state)
    issues, info = [], {}
    if not isinstance(state,dict) or not state:
        return {"status":"critical","issues":[{"code":"workspace_unavailable"}],
                "repairs":[],"signals":{}}
    if not isinstance(state.get("passport"),dict):
        issues.append({"code":"target_audience_not_defined","severity":"attention"})
    if mode=="chats":
        if chat_id is None or selected_chat_details(state) is None:
            issues.append({"code":"selected_chat_unavailable","severity":"critical"})
        else:
            info["selected_source"]="telegram_chat"
    else:
        info["selected_source"]="personal_contacts"
    b = latest_batch(tasks,day)
    ids = _ints(b.get("candidate_ids"))
    approved = _ints(b.get("approved_ids"))
    info["candidate_count"] = len(ids)
    info["approved_count"] = len(approved)
    info["reference_day"] = day.isoformat()
    if ids and len(set(ids)) != len(ids):
        issues.append({"code":"duplicate_candidate_ids","severity":"critical"})
    if ids and not matches_daily_batch(b,mode,chat_id):
        issues.append({"code":"daily_batch_source_mismatch","severity":"critical"})
    if b and (len(ids) < 5):
        issues.append({"code":"daily_five_incomplete","severity":"attention",
                       "found":len(ids),"expected":5})
    if not b and isinstance(state.get("passport"),dict):
        issues.append({"code":"daily_batch_missing","severity":"attention"})
    if ids and any(cid not in ids for cid in approved):
        issues.append({"code":"approved_not_in_daily_batch","severity":"critical"})
    p = state.get("source_progress")
    p = p if isinstance(p,dict) else {}
    if mode=="chats" and chat_id is not None:
        # This is a *metadata* mismatch; source preference is authoritative.
        if (str(p.get("preferred_source_mode") or "")!="chats"
            or str(p.get("preferred_chat_id") or "")!=str(chat_id)):
            issues.append({"code":"source_badge_out_of_sync",
                           "severity":"auto_fixable"})
    info["active_tasks"] = sum(1 for t in tasks if
        isinstance(t,dict) and t.get("task_kind") != REPORT_KIND and
        t.get("status") not in ("Выполнено","Отменено"))
    return {
        "status": ("critical" if any(i["severity"]=="critical" for i in issues)
                   else "needs_attention" if issues else "ok"),
        "issues":issues, "signals":info, "repairs":[],
    }


def safe_repair_metadata(state: dict, audit: dict) -> tuple[dict, list[str]]:
    """Only restore display hints; never select a chat or change daily IDs."""
    issues = [x["code"] for x in audit.get("issues",[])]
    if "source_badge_out_of_sync" not in issues:
        return state, []
    mode, chat_id = owner_source(state)
    chat = selected_chat_details(state)
    if mode!="chats" or chat_id is None or chat is None:
        return state, []
    changed = dict(state)
    p = changed.get("source_progress")
    p = dict(p) if isinstance(p,dict) else {}
    p.update({
        "preferred_source_mode":"chats",
        "preferred_chat_id":chat_id,
        "preferred_chat_title":str(chat.get("title") or "")[:160],
        "source_type":"telegram_chat",
        "source_name":"Telegram-чат: "+str(chat.get("title") or "")[:160],
    })
    changed["source_progress"]=p
    return changed, ["source_badge_out_of_sync"]


def persist_repair(owner_id, audited_state, repair_codes, storage):
    """CAS protected; re-evaluate preference on latest DB state before patch."""
    if not repair_codes:
        return False
    for _ in range(3):
        row=storage._workspace_row(owner_id)
        if not row:
            return False
        live=storage._decrypt_json(str(row.get("encrypted_state") or ""))
        live_audit=evaluate_owner(live,[],date.today())
        corrected, actual=safe_repair_metadata(live,live_audit)
        if not actual:
            return True
        stamp=str(row.get("updated_at") or "")
        if not stamp:
            return False
        updated=storage._sb_patch("agency_workspace_states",
            {"telegram_id":f"eq.{owner_id}","updated_at":f"eq.{stamp}"},
            {"encrypted_state":storage._encrypt_json(corrected),
             "updated_at":datetime.now(UTC).isoformat()})
        if updated:
            return True
        time.sleep(0.2)
    return False


def nightly_task(storage, owner_id, night_date) -> dict | None:
    rows=storage._sb_get("agency_stagirite_tasks",{
        "owner_telegram_id":f"eq.{owner_id}",
        "task_kind":f"eq.{REPORT_KIND}",
        "select":"*","order":"created_at.desc","limit":12,
    })
    for item in rows:
        result=item.get("result")
        if isinstance(result,dict) and result.get("night_date")==night_date.isoformat():
            return item
    return None


def report_owner(owner_id, storage, now, agent_checks) -> dict:
    """One per-owner report per night, revised at fixed night checkpoints."""
    slot, night_date = schedule_slot(now)
    existing=nightly_task(storage,owner_id,night_date)
    old_result=(existing or {}).get("result") or {}
    attempts=old_result.get("attempts") or []
    if slot in [a.get("slot") for a in attempts if isinstance(a,dict)]:
        return {"status":"already_checked", "slot":slot}
    state, _=storage._workspace_state(owner_id)
    day=night_date-timedelta(days=1)
    tasks=storage._load_tasks(owner_id)
    report=evaluate_owner(state,tasks,day)
    candidate_state, patches=safe_repair_metadata(state,report)
    repaired=[]
    if patches and persist_repair(owner_id,candidate_state,patches,storage):
        repaired=patches
        report["issues"]=[i for i in report["issues"]
                          if i.get("code") not in repaired]
        report["repairs"]=repaired
        if not report["issues"]:
            report["status"]="ok"
        elif any(i.get("severity")=="critical" for i in report["issues"]):
            report["status"]="critical"
        else:
            report["status"]="needs_attention"
    timestamp=now.astimezone(MSK).isoformat()
    attempts=[*attempts[-4:],{"slot":slot,"ran_at":timestamp,
          "status":report["status"],"issues":len(report["issues"]),
          "repairs":repaired}]
    result={
        "night_date":night_date.isoformat(),"reference_day":day.isoformat(),
        "last_run":timestamp,"last_slot":slot,"status":report["status"],
        "checks":agent_checks,
        "issues":report["issues"],
        "repairs":report["repairs"],
        "signals":report["signals"],
        "attempts":attempts[-5:],
        "requires_owner_attention": report["status"]!="ok"
        or any(x.get("status") in ("critical","needs_live_quality_test")
               for x in agent_checks),
    }
    payload={"result":result,"status":("В норме" if not result[
        "requires_owner_attention"] else "Нужна проверка"),
        "updated_at":datetime.now(UTC).isoformat()}
    if existing:
        changed=storage._sb_patch("agency_stagirite_tasks",
            {"id":f"eq.{existing['id']}"},payload)
    else:
        changed=storage._sb_post("agency_stagirite_tasks",{
            "owner_telegram_id":int(owner_id),
            "assignment":"Стагирит · ночной контроль качества 00:00 МСК",
            "task_kind":REPORT_KIND,"plan":{"msk_slots":["00:00","01:00","03:00","05:00"]},
            **payload,
        })
    if not changed:
        raise RuntimeError("Audit report was not persisted")
    return {"status":report["status"],"slot":slot,
            "issue_count":len(report["issues"]),
            "repair_count":len(repaired)}


def run_cycle(storage, now=None):
    now=now or datetime.now(UTC)
    when=schedule_slot(now)
    if not when:
        return {"status":"outside_night_window"}
    checks=check_policy_presence()
    totals={"checked":0,"ok":0,"attention":0,"errors":0,"repaired":0}
    for row in storage._owner_sessions():
        try:
            owner=int(row.get("telegram_id"))
            from agency_access_control import is_agency_access_revoked
            if is_agency_access_revoked(owner):
                continue
            r=report_owner(owner,storage,now,checks)
            if r["status"]=="already_checked":
                continue
            totals["checked"]+=1
            totals["repaired"]+=r.get("repair_count",0)
            if r["status"]=="ok":
                totals["ok"]+=1
            else:
                totals["attention"]+=1
        except Exception as exc:
            totals["errors"]+=1
            print(f"[StagiriteNight] owner_check_failed={type(exc).__name__}",
                  flush=True)
    print(f"[StagiriteNight] slot={when[0]} totals={totals}",flush=True)
    return totals


def worker_forever(poll_seconds=60):
    """Runs even while the Telegram scouting worker is busy."""
    import telegram_scout_worker as storage
    poll_seconds=max(30,min(120,int(poll_seconds)))
    print("[StagiriteNight] watchdog active; Moscow 00:00/01:00/03:00/05:00",
          flush=True)
    while True:
        try:
            run_cycle(storage)
        except Exception as exc:
            print(f"[StagiriteNight] cycle_failure={type(exc).__name__}",flush=True)
        time.sleep(poll_seconds)
