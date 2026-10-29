"""Sequential, per-owner Telegram-group scout for workspace schema v7+.

Only the Telegram group explicitly chosen by the owner may feed its daily five.
Does not send direct messages. Prefers people who actually write in the selected
Telegram group, then ranks them against the owner's target-audience passport.
If Telegram hides the member list, recent group messages still provide a lawful
visible source. No fallback to personal contacts or another group is allowed.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone
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


async def _read_active_group_authors(session_string, api_id, api_hash, chat,
                                     excluded, desired=30, message_limit=800):
    """Read recent HUMAN authors from the exact selected group.

    This is the primary path for large groups where Telegram may hide the full
    participant list. Only messages visible to the owner's authenticated session
    are used. Group messages are public context, never treated as private DM
    history.
    """
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
        authors = {}
        scanned = 0

        async for message in client.iter_messages(entity, limit=max(50, int(message_limit))):
            scanned += 1
            sender_id = getattr(message, "sender_id", None)
            try:
                uid = int(sender_id)
            except (TypeError, ValueError):
                continue
            if uid <= 0 or uid == own_id or uid in excluded:
                continue

            text = str(getattr(message, "message", "") or "").strip()
            if not text:
                continue

            try:
                sender = await message.get_sender()
            except Exception:
                sender = None
            if sender is None:
                continue
            # Anonymous admins/channel identities are not people-candidates.
            if getattr(sender, "first_name", None) is None and getattr(sender, "last_name", None) is None:
                continue
            if (getattr(sender, "deleted", False) or getattr(sender, "bot", False)
                or getattr(sender, "scam", False) or getattr(sender, "fake", False)):
                continue

            first_name = str(getattr(sender, "first_name", "") or "").strip()
            last_name = str(getattr(sender, "last_name", "") or "").strip()
            name = " ".join(x for x in (first_name, last_name) if x).strip() or "Без имени"
            raw_date = getattr(message, "date", None)
            if isinstance(raw_date, datetime):
                if raw_date.tzinfo is None:
                    raw_date = raw_date.replace(tzinfo=UTC)
                local_date = raw_date.astimezone(BERLIN)
                today = datetime.now(BERLIN).date()
                if local_date.date() == today:
                    activity_label = f"писал в выбранном чате сегодня в {local_date:%H:%M}"
                elif local_date.date() == today - timedelta(days=1):
                    activity_label = f"писал в выбранном чате вчера в {local_date:%H:%M}"
                else:
                    activity_label = f"писал в выбранном чате {local_date:%d.%m.%Y}"
                last_seen_at = local_date.isoformat()
            else:
                activity_label = "активный автор выбранного чата"
                last_seen_at = ""

            item = authors.get(uid)
            if item is None:
                item = {
                    "telegram_id": uid,
                    "name": name,
                    "first_name": first_name,
                    "last_name": last_name,
                    "username": str(getattr(sender, "username", "") or "").strip(),
                    "access_hash": getattr(sender, "access_hash", None),
                    "source_chat_id": target,
                    "source_chat_title": str(chat.get("title") or "Telegram-чат"),
                    "source_type": "telegram_chat",
                    "source": "Telegram-чат: " + str(chat.get("title") or "Без названия"),
                    "is_owner_contact": False,
                    "activity_eligible": True,
                    "telegram_activity_label": activity_label,
                    "last_seen_at": last_seen_at,
                    "activity_precision": "group_message_exact",
                    "_stagirite_activity_tier": 0,
                    "has_message_history": False,
                    "recent_messages": [],
                    "group_messages": [],
                }
                authors[uid] = item

            if len(item["group_messages"]) < 4:
                item["group_messages"].append(text[:700])

            if len(authors) >= desired and scanned >= min(message_limit, 120):
                break

        for item in authors.values():
            snippets = " | ".join(item.get("group_messages") or [])
            item["public_info"] = (
                f"Активный автор сообщений в выбранной Telegram-группе "
                f"«{chat.get('title') or 'Telegram-чат'}». "
                f"Публичные сообщения в этой группе: {snippets}"
            )[:3200]
        return list(authors.values()), scanned
    finally:
        await client.disconnect()


def _local_business_signal(context):
    """Conservative no-AI fallback based only on visible group text."""
    text = " ".join([
        str(context.get("public_info") or ""),
        " ".join(str(x) for x in context.get("group_messages") or []),
    ]).lower()
    tokens = (
        "бизнес", "предприним", "партнер", "партнёр", "команда", "клиент",
        "маркет", "продаж", "проект", "доход", "заработ", "инвест",
        "искусствен", " ии ", "ai", "автоматизац", "нейросет", "обучен",
        "лидер", "сервис", "контент", "рекрут", "рутин", "времени",
        "не успева", "занят", "перегруз", "админист", "организ", "встреч",
        "сообщен", "заявк", "рассыл", "публикац", "соцсет",
    )
    return any(token in text for token in tokens)


def _rank_active_group_authors(state, storage, authors, desired):
    """Apply workability + target-audience analysis to active group authors."""
    passport = state.get("passport") if isinstance(state.get("passport"), dict) else None
    if not passport or not authors:
        return []

    sent_ids = storage._sent_ids(state)
    blocked_ids = storage._blocked_ids(state)
    known_ids = storage._known_contact_ids(state)
    workable = []
    for source in authors:
        state_info = storage._classify_workability(
            source,
            already_sent_ids=sent_ids,
            known_contact_ids=known_ids,
            blocked_ids=blocked_ids,
        )
        enriched = {**source, **state_info}
        if enriched.get("work_state") == "available" and not bool(enriched.get("selection_blocked")):
            workable.append(enriched)

    ranked = []
    passport_analysis = passport.get("analysis") or passport.get("profile") or passport
    for start in range(0, len(workable), 10):
        if len(ranked) >= desired:
            break
        batch = workable[start:start+10]
        informative, empty = storage._split_analysis_value(batch)
        batch_results = []
        if informative:
            try:
                batch_results.extend(storage._analyze_candidates(passport_analysis, informative))
            except Exception:
                # Keep service useful if the AI analysis endpoint is temporarily down,
                # but only for visible authors with an explicit local business signal.
                for source in informative:
                    if _local_business_signal(source):
                        batch_results.append({
                            **source,
                            "potential_interest": "средний",
                            "actuality": "активен сейчас",
                            "warmth": "холодный",
                            "obstacles": [],
                            "short_portrait": "Активный автор выбранного тематического чата; требуется ручная проверка профиля.",
                            "owner_hint": "Проверьте профиль перед первым сообщением.",
                            "message_angle": "Начать с контекста его публичного сообщения в группе.",
                            "business_relevant": True,
                            "business_evidence": ["активное публичное сообщение по деловой/проектной теме"],
                            "business_gate_reason": "Локальный резервный фильтр по публичному сообщению.",
                            "score": 45,
                            "recommendation": "Решает владелец",
                            "selection_blocked": False,
                            "block_reason": "",
                            "analysis_cost_mode": "local_group_signal",
                            "status": "Проанализирован",
                        })

        source_by_id = {int(x["telegram_id"]): x for x in batch}
        for item in batch_results:
            try:
                cid = int(item["telegram_id"])
            except (KeyError, TypeError, ValueError):
                continue
            source = source_by_id.get(cid)
            if not source:
                continue
            merged = {
                **source,
                **item,
                "is_owner_contact": False,
                "source_chat_id": int(source.get("source_chat_id") or 0),
                "source_chat_title": str(source.get("source_chat_title") or ""),
                "source_type": "telegram_chat",
                "source": str(source.get("source") or ""),
            }
            if bool(merged.get("selection_blocked")):
                continue
            if merged.get("business_relevant") is not True:
                continue
            ranked.append(merged)

    dedup = {}
    for item in ranked:
        dedup[int(item["telegram_id"])] = item
    ranked = list(dedup.values())
    ranked.sort(key=storage._candidate_priority)
    return ranked[:max(1, int(desired))]


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
        previous_today = set()
        if (str(progress.get("last_batch_date") or "") == today
            and str(progress.get("last_batch_source_signature") or "") == signature):
            previous_today = _int_set(progress.get("last_batch_ids", []))
        today_ids = previous_today | {int(c["telegram_id"]) for c in candidates}
        progress.update({
            "chat_stats":stats, "source_mode":"chats",
            "preferred_source_mode":"chats", "preferred_chat_id":chat_id,
            "preferred_chat_title":str(chat.get("title") or ""),
            "current_chat_id":chat_id,"source_type":"telegram_chat",
            "source_name":"Telegram-чат: "+str(chat.get("title") or ""),
            "today":len(today_ids),"updated_at":datetime.now(BERLIN).isoformat(),
            "last_batch_date":today,"last_batch_ids":sorted(today_ids),
            "last_batch_source_signature":signature,
            "selection_method":"active_group_authors",
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
        "source":"neonia_group_activity_targeting",
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
    needed=max(1,5-len(existing_cards))
    cards=[]
    scanned=0
    exhausted=False

    # PRIMARY: people who actually write in the selected group. This works even
    # when Telegram hides the full 12k-member participant list.
    try:
        authors,scanned=asyncio.run(
            _read_active_group_authors(
                session_string,api_id,api_hash,chat,excluded,
                desired=max(120,needed*24),message_limit=3000,
            )
        )
        cards=_rank_active_group_authors(state,storage,authors,needed)
    except Exception as exc:
        print(
            f"[NeoniaChat] owner={owner_id} active-author scan failed: {type(exc).__name__}",
            flush=True,
        )

    # SECONDARY: keep the old participant-list path only as a fallback. It is
    # useful in small open groups, but never falls back to personal contacts.
    new_offset=offset
    if len(cards)<needed:
        try:
            raw_members,new_offset,member_scanned,exhausted=asyncio.run(
                _read_selected_members(
                    session_string,api_id,api_hash,chat,offset,
                    excluded | {int(x["telegram_id"]) for x in cards},
                    desired=max(1,needed-len(cards)),
                )
            )
            scanned += member_scanned
            # Participant-only cards have no evidence for target-audience ranking.
            # Keep only locally business-signaled records; usually the active-author
            # path above supplies the full five.
            for item in raw_members:
                if len(cards)>=needed:
                    break
                if _local_business_signal(item):
                    cards.append(item)
        except Exception as exc:
            print(
                f"[NeoniaChat] owner={owner_id} participant fallback unavailable: {type(exc).__name__}",
                flush=True,
            )

    if not cards and not existing_cards:
        if scanned>0:
            _persist_group_selection(storage,owner_id,mode,chat,[],
                                     new_offset,scanned,exhausted)
        return {"owner_id":owner_id,"status":"empty",
                "reason":"no target active authors found in selected chat",
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
