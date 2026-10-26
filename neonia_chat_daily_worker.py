"""Sequential, per-owner Telegram-group scout for workspace schema v7+.

Only the Telegram group explicitly chosen by the owner may feed its daily five.
Does not send direct messages. Reads visible participants through the owner's
existing authenticated Telegram session, respects Telegram restrictions, and
stores selection under the owner's encrypted workspace for the UI and Stagirite.
"""
import asyncio
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.sessions import StringSession

from neonia_daily_source import (
    owner_source, selected_chat_details, source_signature, matches_daily_batch,
)

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc


def _int_set(items):
    result = set()
    for value in items or []:
        try:
            v = int(value)
            if v > 0:
                result.add(v)
        except (TypeError, ValueError):
            pass
    return result


def _from_history(state, storage, owner_id):
    processed = _int_set(state.get("processed_candidates", []))
    for person in state.get("sent_log", []) or []:
        if isinstance(person, dict):
            processed.update(_int_set([person.get("telegram_id")]))
    known = state.get("owner_known_contacts", [])
    if isinstance(known, dict):
        known = list(known.values())
    for item in known or []:
        if isinstance(item, dict):
            processed.update(_int_set([item.get("telegram_id")]))
    try:
        tasks = storage._load_tasks(owner_id)
        processed |= storage._shown_candidate_ids(
            tasks, exclude_day=datetime.now(BERLIN).date().isoformat()
        )
    except Exception:
        # Do not replace global history with only today's items.
        pass
    return processed


def _daily_task_source(storage, owner_id, day):
    tasks = storage._load_tasks(owner_id)
    task = storage._active_weekly_task(tasks, day)
    if task is None:
        task = storage._daily_task(tasks)
    if task is None:
        return None, {}
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    container = (result.get("weekly_goal")
                 if isinstance(result.get("weekly_goal"), dict)
                 else result.get("daily_candidate_feed"))
    container = container if isinstance(container, dict) else {}
    batches = container.get("daily_batches")
    return task, ((batches or {}).get(day.isoformat()) or {})


async def _read_selected_members(session_string, api_id, api_hash, chat, offset,
                                 excluded, desired=5):
    """Read at most 250 new visible member positions from EXACT chosen group."""
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session authorization expired")
        target = int(chat["chat_id"])
        entity = None
        async for dialog in client.iter_dialogs(limit=500):
            if not bool(getattr(dialog, "is_group", False)):
                continue
            if int(getattr(dialog.entity, "id", -1)) == target:
                entity = dialog.entity
                break
        if entity is None:
            raise RuntimeError("Selected Telegram group is inaccessible")
        self_user = await client.get_me()
        own_id = int(self_user.id)
        results = []
        position = 0
        scanned = 0
        exhausted = True
        async for user in client.iter_participants(entity, limit=None):
            position += 1
            if position <= offset:
                continue
            scanned += 1
            uid = int(user.id)
            if (not getattr(user,"deleted",False)
                and not getattr(user,"bot",False)
                and not getattr(user,"scam",False)
                and not getattr(user,"fake",False)
                and uid != own_id and uid not in excluded):
                name = " ".join(x for x in
                    (str(getattr(user,"first_name","") or "").strip(),
                     str(getattr(user,"last_name","") or "").strip()) if x)
                results.append({
                    "telegram_id": uid,
                    "name": name or "Без имени",
                    "first_name": str(getattr(user,"first_name","") or ""),
                    "last_name": str(getattr(user,"last_name","") or ""),
                    "username": str(getattr(user,"username","") or ""),
                    "access_hash": getattr(user,"access_hash",None),
                    "source_chat_id": target,
                    "source_chat_title": str(chat.get("title") or "Telegram-чат"),
                    "source_type": "telegram_chat",
                    "source": "Telegram-чат: "+str(chat.get("title") or "Без названия"),
                    "segment": "Последовательная очередь",
                    "recommendation": "Выдан в ежедневной пятёрке",
                    "status": "Выдан Неонией",
                    "is_owner_contact": False,
                    "telegram_activity_label": "участник группы (время активности не проверено)",
                })
                excluded.add(uid)
            if len(results) >= desired or scanned >= 250:
                exhausted = False
                break
        return results, position, scanned, exhausted
    finally:
        await client.disconnect()


def _persist_group_selection(storage, owner_id, mode, chat, candidates,
                             new_offset, scanned, exhausted):
    """Patch latest encrypted workspace with CAS; never clobber chat preference."""
    chat_id = int(chat["chat_id"])
    today = datetime.now(BERLIN).date().isoformat()
    signature = source_signature(mode, chat_id)
    for attempt in range(3):
        row = storage._workspace_row(owner_id)
        if not row:
            return False
        old = storage._decrypt_json(str(row.get("encrypted_state") or ""))
        current_mode, current_chat_id = owner_source(old)
        if current_mode != mode or current_chat_id != chat_id:
            return False
        updated = dict(old)
        offsets = dict(updated.get("chat_offsets") or {})
        offsets[str(chat_id)] = max(int(offsets.get(str(chat_id)) or 0),new_offset)
        updated["chat_offsets"] = offsets
        prev_cards = storage._normalize_candidate_list(updated.get("candidates", []))
        updated["candidates"] = storage._merge_candidate_results(prev_cards,candidates)
        processed = _int_set(updated.get("processed_candidates", []))
        processed.update(int(c["telegram_id"]) for c in candidates)
        updated["processed_candidates"] = sorted(processed)
        progress = dict(updated.get("source_progress") or {})
        stat = dict((progress.get("chat_stats") or {}).get(str(chat_id)) or {})
        stats = dict(progress.get("chat_stats") or {})
        stat["title"] = str(chat.get("title") or "")
        stat["total"] = int(chat.get("participants_count") or stat.get("total") or 0)
        stat["shown"] = int(stat.get("shown") or 0) + len(candidates)
        stat["technical_skipped"] = int(stat.get("technical_skipped") or 0)
        stat["completed"] = bool(exhausted)
        stat.pop("error",None)
        stats[str(chat_id)] = stat
        progress.update({
            "chat_stats":stats, "source_mode":"chats",
            "preferred_source_mode":"chats", "preferred_chat_id":chat_id,
            "preferred_chat_title":str(chat.get("title") or ""),
            "current_chat_id":chat_id,"source_type":"telegram_chat",
            "source_name":"Telegram-чат: "+str(chat.get("title") or ""),
            "today":len(candidates),"updated_at":datetime.now(BERLIN).isoformat(),
            "last_batch_date":today,"last_batch_ids":[int(c["telegram_id"]) for c in candidates],
            "last_batch_source_signature":signature,
        })
        if exhausted:
            progress["completed_chat_ids"] = sorted(
                _int_set(progress.get("completed_chat_ids",[])) | {chat_id})
        updated["source_progress"] = progress
        filters={"telegram_id":f"eq.{owner_id}"}
        existing_timestamp=str(row.get("updated_at") or "")
        if existing_timestamp:
            filters["updated_at"]=f"eq.{existing_timestamp}"
        changed=storage._sb_patch("agency_workspace_states",filters,{
            "encrypted_state":storage._encrypt_json(updated),
            "updated_at":datetime.now(UTC).isoformat(),
        })
        if changed:
            return True
        time.sleep(0.3)
    return False


def _write_daily_group_task(storage, owner_id, day, chat_id, candidates):
    """A daily batch must carry its group ID, not just 'sequential queue'."""
    task, current = _daily_task_source(storage,owner_id,day)
    signature=source_signature("chats",chat_id)
    if matches_daily_batch(current,"chats",chat_id) and len(
        _int_set(current.get("candidate_ids",[])))>=5:
        return True
    if current.get("approved_ids") and not matches_daily_batch(
        current,"chats",chat_id):
        # Do not retract anyone already approved by the owner.
        return False
    if task is None:
        task=storage._ensure_daily_task(owner_id,storage._load_tasks(owner_id))
    task_id=str(task.get("id") or "")
    if not task_id:
        return False
    tasks=storage._sb_get("agency_stagirite_tasks",{
        "id":f"eq.{task_id}","select":"*","limit":1})
    if tasks:
        task=tasks[0]
    result=dict(task.get("result") or {})
    key=("weekly_goal" if isinstance(result.get("weekly_goal"),dict)
        else "daily_candidate_feed")
    goal=dict(result.get(key) or {})
    batches=dict(goal.get("daily_batches") or {})
    last=dict(batches.get(day.isoformat()) or {})
    confirmed=_int_set(last.get("approved_ids",[]))
    if confirmed and not matches_daily_batch(last,"chats",chat_id):
        return False
    selected_ids=list(dict.fromkeys(
        [int(x) for x in last.get("candidate_ids",[])
         if str(x).isdigit()] if matches_daily_batch(last,"chats",chat_id)
        else []))
    for card in candidates:
        cid=int(card["telegram_id"])
        if cid not in selected_ids:
            selected_ids.append(cid)
    selected_ids=selected_ids[:5]
    last.update({
        "candidate_ids":selected_ids,
        "approved_ids":[cid for cid in confirmed if cid in selected_ids],
        "source":"neonia_sequential_queue",
        "source_mode":"chats",
        "source_chat_id":int(chat_id),
        "source_signature":signature,
        "prepared_at":datetime.now(UTC).isoformat(),
        "complete":len(selected_ids)>=5,
    })
    batches[day.isoformat()]=last
    goal["daily_batches"]=batches
    goal["last_daily_prepare_date"]=day.isoformat()
    result[key]=goal
    rows=storage._sb_patch("agency_stagirite_tasks",{
        "id":f"eq.{task_id}"},{
        "status":"В работе","result":result,
        "updated_at":datetime.now(UTC).isoformat()
    })
    return bool(rows)


def process(owner_id,session_string,state,storage):
    today=datetime.now(BERLIN).date()
    mode,chat_id=owner_source(state)
    if mode!="chats":
        return {"owner_id":owner_id,"status":"skip","reason":"contact mode"}
    if chat_id is None:
        return {"owner_id":owner_id,"status":"skip","reason":"selected group missing"}
    chat=selected_chat_details(state)
    if not chat:
        return {"owner_id":owner_id,"status":"skip","reason":"selected group unavailable in saved chats"}
    # Runs only during the user's daytime; at most once per day if 5 delivered.
    if not 6 <= datetime.now(BERLIN).hour < 22:
        return {"owner_id":owner_id,"status":"skip","reason":"outside daily window"}
    task, current=_daily_task_source(storage,owner_id,today)
    if matches_daily_batch(current,mode,chat_id) and len(
        _int_set(current.get("candidate_ids",[])))>=5:
        return {"owner_id":owner_id,"status":"ready","count":5,"source_chat_id":chat_id}
    if current.get("approved_ids") and not matches_daily_batch(
        current,mode,chat_id):
        return {"owner_id":owner_id,"status":"skip",
                "reason":"old batch already approved; new chat takes effect tomorrow"}
    progress=state.get("source_progress") or {}
    sig=source_signature(mode,chat_id)
    if (progress.get("last_batch_date")==today.isoformat()
        and progress.get("last_batch_source_signature")==sig
        and progress.get("last_batch_ids")):
        cached=_int_set(progress["last_batch_ids"])
        saved=[x for x in storage._normalize_candidate_list(state.get("candidates",[]))
               if int(x["telegram_id"]) in cached
               and int(x.get("source_chat_id") or 0)==chat_id]
        if saved:
            _write_daily_group_task(storage,owner_id,today,chat_id,saved)
            if len(saved)>=5:
                return {"owner_id":owner_id,"status":"prepared",
                        "count":len(saved),"source_chat_id":chat_id}
            # A partial recovered batch must still be topped up, not frozen.
            task,current=_daily_task_source(storage,owner_id,today)
    excluded=_from_history(state,storage,owner_id)
    # Avoid duplicating any already delivered same-source partial daily set.
    existing_cards=[]
    if matches_daily_batch(current,mode,chat_id):
        have=_int_set(current.get("candidate_ids",[]))
        existing_cards=[x for x in storage._normalize_candidate_list(state.get("candidates",[]))
                        if int(x["telegram_id"]) in have
                        and int(x.get("source_chat_id") or 0)==chat_id]
    offset=int((state.get("chat_offsets") or {}).get(str(chat_id)) or 0)
    api_id,api_hash=storage._telegram_api_credentials()
    try:
        cards,new_offset,scanned,exhausted=asyncio.run(
            _read_selected_members(session_string,api_id,api_hash,chat,offset,
                                   excluded,desired=max(1,5-len(existing_cards))))
    except Exception as exc:
        return {"owner_id":owner_id,"status":"skip",
                "reason":"selected Telegram group cannot be enumerated",
                "error_type":type(exc).__name__}
    if not cards and not existing_cards:
        # Advance the cursor past previously seen/inaccessible participants;
        # otherwise an owner in a 12k-member group can be stuck forever on
        # the same 250 duplicate profiles every morning.
        if scanned>0:
            _persist_group_selection(storage,owner_id,mode,chat,[],
                                     new_offset,scanned,exhausted)
        return {"owner_id":owner_id,"status":"empty",
                "reason":"no accessible new members in selected chat",
                "source_chat_id":chat_id}
    if not _persist_group_selection(storage,owner_id,mode,chat,cards,
                                    new_offset,scanned,exhausted):
        return {"owner_id":owner_id,"status":"skip",
                "reason":"workspace changed during selection"}
    combined=existing_cards+cards
    if not _write_daily_group_task(storage,owner_id,today,chat_id,combined):
        return {"owner_id":owner_id,"status":"skip","reason":"daily task update not confirmed"}
    return {"owner_id":owner_id,"status":"prepared","count":len(combined),
            "source_chat_id":chat_id}
