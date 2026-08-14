import base64
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st
from neola_cabinet_map import NEOLA_MISSION, neola_cabinet_knowledge, neola_first_greeting


BERLIN_TZ = ZoneInfo("Europe/Berlin")
UTC_TZ = ZoneInfo("UTC")
MIN_LODGES = 5


def _now_iso():
    return datetime.now(UTC_TZ).isoformat()


def _supabase_headers(prefer=None):
    headers = {
        "apikey": st.secrets["SUPABASE_SECRET_KEY"],
        "Authorization": f"Bearer {st.secrets['SUPABASE_SECRET_KEY']}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _supabase_url(path):
    return f"{st.secrets['SUPABASE_URL']}/rest/v1/{path.lstrip('/')}"


def _get_json(path, params=None, timeout=15):
    response = requests.get(
        _supabase_url(path),
        headers=_supabase_headers(),
        params=params or {},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def _post_json(path, payload, prefer="return=representation", timeout=15):
    response = requests.post(
        _supabase_url(path),
        headers=_supabase_headers(prefer),
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    if not response.text.strip():
        return []
    return response.json()


def _patch_json(path, params, payload, prefer="return=representation", timeout=15):
    response = requests.patch(
        _supabase_url(path),
        headers=_supabase_headers(prefer),
        params=params,
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    if not response.text.strip():
        return []
    return response.json()


def load_agency_members():
    return _get_json(
        "agency_members",
        params={
            "select": "telegram_id,first_name,username,member_code,referrer_code,created_at",
            "order": "created_at.desc",
            "limit": "10000",
        },
    )


def get_member_by_telegram_id(telegram_id):
    rows = _get_json(
        "agency_members",
        params={
            "telegram_id": f"eq.{int(telegram_id)}",
            "select": "telegram_id,first_name,username,member_code,referrer_code,created_at",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


def load_partner_activations(telegram_ids=None):
    params = {
        "select": (
            "telegram_id,neoxa_nickname,lodges_count,status,proof_filename,"
            "proof_mime,submitted_at,reviewed_at,reviewed_by,rejection_reason,"
            "onboarding_status,onboarding_step,last_action_at,attention_level"
        ),
        "limit": "10000",
    }
    if telegram_ids:
        ids = ",".join(str(int(item)) for item in telegram_ids)
        params["telegram_id"] = f"in.({ids})"
    try:
        return _get_json("partner_activations", params=params)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in {400, 404}:
            return []
        raise


def get_partner_activation(telegram_id):
    rows = load_partner_activations([telegram_id])
    return rows[0] if rows else None


def ensure_partner_activation(telegram_id):
    member = get_member_by_telegram_id(telegram_id)
    if not member:
        return None

    existing = get_partner_activation(telegram_id)
    if existing:
        return existing

    # Корневой/старый владелец кабинета не должен внезапно потерять доступ.
    # Новые люди, пришедшие по партнёрской ссылке, подтверждают 5 лож.
    has_referrer = bool(str(member.get("referrer_code") or "").strip())
    status = "awaiting_proof" if has_referrer else "legacy_active"
    onboarding_status = "not_started"

    try:
        rows = _post_json(
            "partner_activations?on_conflict=telegram_id",
            {
                "telegram_id": int(telegram_id),
                "status": status,
                "lodges_count": 0,
                "onboarding_status": onboarding_status,
                "onboarding_step": 0,
                "attention_level": "none",
                "last_action_at": _now_iso(),
            },
            prefer="resolution=merge-duplicates,return=representation",
        )
        return rows[0] if rows else get_partner_activation(telegram_id)
    except requests.HTTPError:
        return None


def activation_is_confirmed(activation):
    if not activation:
        return False
    return activation.get("status") in {"confirmed", "legacy_active"}


def activation_label(activation):
    if not activation:
        return "⚪ Статус не создан"
    status = activation.get("status")
    labels = {
        "awaiting_proof": "🟡 Ожидается подтверждение 5 лож",
        "proof_submitted": "⏳ Скриншот отправлен наставнику",
        "confirmed": "🟢 5 лож подтверждены",
        "rejected": "🔴 Нужен новый скриншот",
        "legacy_active": "🟢 Активный партнёр",
    }
    return labels.get(status, f"⚪ {status or 'Не определён'}")


def _extract_response_text(data):
    parts = []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))
    return "\n".join(parts).strip()


def analyze_neoxa_proof(image_bytes, mime_type):
    """ИИ только подсказывает, что видно на скриншоте. Решение принимает наставник."""
    api_key = st.secrets.get("OPENAI_API_KEY")
    if not api_key:
        return {
            "nickname": "",
            "lodges_count": 0,
            "looks_like_neoxa": False,
            "confidence": "низкая",
            "note": "OpenAI API key не найден — скриншот проверит наставник вручную.",
        }

    data_url = (
        f"data:{mime_type or 'image/png'};base64,"
        + base64.b64encode(image_bytes).decode("ascii")
    )
    body = {
        "model": "gpt-5-mini",
        "instructions": (
            "Ты проверяешь скриншот активации партнёра NeoXa/NeoNexa. "
            "Не подтверждай покупку окончательно — только извлеки видимые данные. "
            "Верни ТОЛЬКО JSON-объект: nickname (строка), lodges_count (целое число), "
            "looks_like_neoxa (true/false), confidence ('высокая'|'средняя'|'низкая'), "
            "note (кратко что видно или чего не хватает). Не додумывай." 
        ),
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Найди на скриншоте ник/идентификатор пользователя и "
                            "видимое количество приобретённых или активированных лож."
                        ),
                    },
                    {
                        "type": "input_image",
                        "image_url": data_url,
                        "detail": "high",
                    },
                ],
            }
        ],
        "store": False,
    }
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=90,
    )
    response.raise_for_status()
    text = _extract_response_text(response.json())
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise RuntimeError("Не удалось прочитать данные со скриншота.")
    data = json.loads(match.group(0))
    try:
        lodges = int(data.get("lodges_count") or 0)
    except (TypeError, ValueError):
        lodges = 0
    return {
        "nickname": str(data.get("nickname") or "").strip()[:120],
        "lodges_count": max(0, lodges),
        "looks_like_neoxa": bool(data.get("looks_like_neoxa", False)),
        "confidence": str(data.get("confidence") or "низкая")[:20],
        "note": str(data.get("note") or "")[:500],
    }


def submit_activation_proof(telegram_id, uploaded_file):
    """
    Финансовое доказательство сохраняется ПЕРВЫМ.
    Ошибка OpenAI не должна приводить к потере скриншота.
    """
    image_bytes = uploaded_file.getvalue()
    if not image_bytes:
        raise RuntimeError("Файл пуст.")
    if len(image_bytes) > 4 * 1024 * 1024:
        raise RuntimeError("Скриншот слишком большой. Максимум 4 МБ.")

    mime_type = uploaded_file.type or "image/png"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    submitted_at = _now_iso()

    # ШАГ 1. Надёжно сохраняем сам файл и переводим заявку в ожидание проверки.
    initial_payload = {
        "telegram_id": int(telegram_id),
        "status": "proof_submitted",
        "neoxa_nickname": None,
        "lodges_count": 0,
        "proof_filename": uploaded_file.name[:255],
        "proof_mime": mime_type[:100],
        "proof_image_base64": encoded,
        "proof_ai_result": {
            "nickname": "",
            "lodges_count": 0,
            "looks_like_neoxa": False,
            "confidence": "не проверено",
            "note": "Скриншот сохранён. ИИ-анализ ещё не выполнен.",
        },
        "submitted_at": submitted_at,
        "reviewed_at": None,
        "reviewed_by": None,
        "rejection_reason": None,
        "attention_level": "red",
        "last_action_at": submitted_at,
    }

    rows = _post_json(
        "partner_activations?on_conflict=telegram_id",
        initial_payload,
        prefer="resolution=merge-duplicates,return=representation",
        timeout=30,
    )
    saved_activation = rows[0] if rows else get_partner_activation(telegram_id)

    # ШАГ 2. ИИ — только помощник. Любая его ошибка НЕ отменяет сохранение.
    try:
        analysis = analyze_neoxa_proof(image_bytes, mime_type)
        ai_payload = {
            "neoxa_nickname": analysis["nickname"] or None,
            "lodges_count": int(analysis["lodges_count"]),
            "proof_ai_result": analysis,
            "last_action_at": _now_iso(),
        }
        patched = _patch_json(
            "partner_activations",
            {"telegram_id": f"eq.{int(telegram_id)}"},
            ai_payload,
        )
        if patched:
            saved_activation = patched[0]
        return saved_activation, analysis, None
    except Exception as exc:
        fallback = {
            "nickname": "",
            "lodges_count": 0,
            "looks_like_neoxa": False,
            "confidence": "не проверено",
            "note": (
                "Скриншот сохранён. Автоматический анализ временно недоступен; "
                "нужна ручная проверка наставником/владельцем."
            ),
        }
        # Сохраняем отметку об ошибке анализа, но не сам текст исключения целиком.
        try:
            _patch_json(
                "partner_activations",
                {"telegram_id": f"eq.{int(telegram_id)}"},
                {
                    "proof_ai_result": fallback,
                    "last_action_at": _now_iso(),
                },
            )
        except Exception:
            pass
        return saved_activation, fallback, str(exc)



def load_activation_proof(telegram_id):
    try:
        rows = _get_json(
            "partner_activations",
            params={
                "telegram_id": f"eq.{int(telegram_id)}",
                "select": "proof_image_base64,proof_mime,proof_ai_result,proof_filename,submitted_at,reviewed_at,reviewed_by,lodges_count,status",
                "limit": "1",
            },
        )
    except requests.HTTPError:
        return None
    return rows[0] if rows else None


def review_activation(
    telegram_id,
    reviewer_telegram_id,
    approved,
    reason="",
    confirmed_lodges=None,
):
    activation = get_partner_activation(telegram_id)
    if not activation:
        raise RuntimeError("Заявка на активацию не найдена.")

    payload = {
        "status": "confirmed" if approved else "rejected",
        "reviewed_at": _now_iso(),
        "reviewed_by": int(reviewer_telegram_id),
        "rejection_reason": None if approved else (reason.strip() or "Нужен более ясный скриншот."),
        "attention_level": "none" if approved else "orange",
        "last_action_at": _now_iso(),
    }
    if approved:
        try:
            confirmed_lodges = int(confirmed_lodges)
        except (TypeError, ValueError):
            confirmed_lodges = int(activation.get("lodges_count") or 0)
        if confirmed_lodges < MIN_LODGES:
            raise RuntimeError("Для активации нужно подтвердить не меньше 5 лож.")
        payload["lodges_count"] = confirmed_lodges
        payload["onboarding_status"] = "not_started"
        payload["onboarding_step"] = 0

    rows = _patch_json(
        "partner_activations",
        {"telegram_id": f"eq.{int(telegram_id)}"},
        payload,
    )
    return rows[0] if rows else get_partner_activation(telegram_id)


def _member_maps(members):
    by_code = {}
    children = {}
    for member in members:
        code = str(member.get("member_code") or "").strip()
        if code:
            by_code[code] = member
        ref = str(member.get("referrer_code") or "").strip()
        if ref:
            children.setdefault(ref, []).append(member)
    for value in children.values():
        value.sort(key=lambda x: str(x.get("first_name") or "").lower())
    return by_code, children


def descendants_for_member(members, root_member_code):
    _, children = _member_maps(members)
    result = []
    queue = [(root_member_code, 1)]
    seen = set()
    while queue:
        parent_code, depth = queue.pop(0)
        for member in children.get(parent_code, []):
            code = str(member.get("member_code") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            row = dict(member)
            row["depth"] = depth
            result.append(row)
            queue.append((code, depth + 1))
    return result


def direct_inviter_member(members, member):
    ref = str(member.get("referrer_code") or "").strip()
    if not ref:
        return None
    by_code, _ = _member_maps(members)
    return by_code.get(ref)


def _member_display_name(member):
    if not member:
        return ""
    name = str(member.get("first_name") or "").strip()
    username = str(member.get("username") or "").strip().lstrip("@")
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    if username:
        return f"@{username}"
    return f"Telegram {member.get('telegram_id') or '—'}"


def _parse_member_created_at(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC_TZ)
    return parsed


def _member_joined_today(member):
    parsed = _parse_member_created_at(member.get("created_at"))
    if not parsed:
        return False
    return parsed.astimezone(BERLIN_TZ).date() == datetime.now(BERLIN_TZ).date()


def _member_joined_label(member):
    parsed = _parse_member_created_at(member.get("created_at"))
    if not parsed:
        return "дата не определена"
    return parsed.astimezone(BERLIN_TZ).strftime("%d.%m.%Y %H:%M")


def _created_sort_key(member):
    parsed = _parse_member_created_at(member.get("created_at"))
    return parsed.timestamp() if parsed else 0


def _visible_members_for_viewer(members, current_member, current_member_code, is_root_owner):
    """Владелец видит весь реестр, обычный партнёр — только свою ветку вниз."""
    descendants = descendants_for_member(members, current_member_code)
    if not is_root_owner:
        return descendants

    depth_by_code = {
        str(item.get("member_code") or ""): int(item.get("depth") or 0)
        for item in descendants
    }
    current_id = int((current_member or {}).get("telegram_id") or 0)
    visible = []
    for member in members:
        member_id = int(member.get("telegram_id") or 0)
        if member_id == current_id:
            continue
        row = dict(member)
        code = str(row.get("member_code") or "")
        row["depth"] = depth_by_code.get(code)
        row["outside_owner_tree"] = code not in depth_by_code
        visible.append(row)
    return visible


def _compact_status(activation):
    if not activation:
        return "⚪ Зарегистрирован"
    status = activation.get("status")
    onboarding = activation.get("onboarding_status")
    if status == "proof_submitted":
        return "🔴 Ждёт подтверждения"
    if status == "rejected":
        return "🟠 Нужен скриншот"
    if status in {"confirmed", "legacy_active"}:
        if onboarding in {"in_progress", "started"}:
            return "🟡 Онбординг"
        if onboarding == "activated":
            return "🟢 Активирован"
        return "🟢 Активен"
    return "🟡 Ждёт 5 лож"


def _attention_label(activation):
    level = str((activation or {}).get("attention_level") or "none")
    return {
        "red": "🔴 Владелец",
        "orange": "🟠 Наставник",
        "yellow": "🟡 Неола",
        "none": "—",
    }.get(level, "—")


def render_my_activation(telegram_id):
    activation = ensure_partner_activation(telegram_id)
    st.markdown("#### 🔐 Моя активация Neonexa")
    st.write(activation_label(activation))

    if activation_is_confirmed(activation):
        lodges = int((activation or {}).get("lodges_count") or 0)
        if lodges:
            st.caption(f"Подтверждено лож: {lodges}")
        return activation

    if activation and activation.get("status") == "proof_submitted":
        st.info("Скриншот уже отправлен. После подтверждения наставником включится Неола.")
        return activation

    if activation and activation.get("status") == "rejected":
        reason = str(activation.get("rejection_reason") or "Нужен новый скриншот.")
        st.warning(f"Наставник попросил новый скриншот: {reason}")

    st.caption(
        "Загрузите скриншот Neonexa, на котором одновременно видны ваш ник и "
        "количество приобретённых/активированных лож (не меньше 5)."
    )
    uploaded = st.file_uploader(
        "Скриншот Neonexa",
        type=["png", "jpg", "jpeg", "webp"],
        key=f"neola_activation_proof_{telegram_id}",
    )
    if uploaded is not None and st.button(
        "📷 Отправить на подтверждение",
        type="primary",
        key=f"neola_submit_proof_{telegram_id}",
    ):
        try:
            with st.spinner("Сначала сохраняю скриншот, затем пробую его распознать..."):
                _, analysis, ai_error = submit_activation_proof(telegram_id, uploaded)

            st.success(
                "Скриншот сохранён и отправлен на подтверждение. "
                "Теперь он не потеряется даже при ошибке ИИ."
            )
            if ai_error:
                st.warning(
                    "Автоматическое распознавание временно недоступно. "
                    "Наставник или владелец структуры проверит скриншот вручную."
                )
            else:
                st.caption(
                    f"Предварительно распознано: ник — "
                    f"{analysis['nickname'] or 'не найден'}, "
                    f"ложи — {analysis['lodges_count']}. "
                    "Окончательное решение принимает человек."
                )
            st.rerun()
        except Exception as exc:
            st.error(
                "Не удалось сохранить скриншот. "
                f"Повторите попытку: {exc}"
            )
    return activation


def render_partner_center(current_telegram_id, current_member_code, current_name):
    st.markdown("### 🌳 Центр партнёров")

    with st.container(border=True):
        render_my_activation(current_telegram_id)

    try:
        members = load_agency_members()
    except Exception as exc:
        st.error(f"Не удалось загрузить структуру партнёров: {exc}")
        return

    current_member = next(
        (
            member for member in members
            if int(member.get("telegram_id") or 0) == int(current_telegram_id)
        ),
        None,
    )
    is_root_owner = bool(
        current_member
        and not str(current_member.get("referrer_code") or "").strip()
    )

    visible = _visible_members_for_viewer(
        members,
        current_member,
        current_member_code,
        is_root_owner,
    )
    visible_ids = {int(item.get("telegram_id") or 0) for item in visible}
    ids = sorted(visible_ids)
    activations = load_partner_activations(ids) if ids else []
    activation_by_id = {int(item["telegram_id"]): item for item in activations}

    if is_root_owner:
        st.info(
            "👑 Режим владельца Агентства W: здесь видны все зарегистрированные "
            "люди во всём Агентстве, независимо от ветки."
        )
    else:
        st.caption(
            "🔐 Вы видите только свою структуру вниз: личных партнёров и все поколения под ними."
        )

    unresolved_inviter = []
    for member in visible:
        ref_code = str(member.get("referrer_code") or "").strip()
        inviter = direct_inviter_member(members, member)
        if not ref_code or inviter is None:
            unresolved_inviter.append(member)

    if is_root_owner and unresolved_inviter:
        st.warning(
            f"⚠️ У {len(unresolved_inviter)} человек пригласитель не определён или "
            "цепочка приглашения нарушена. Они всё равно показаны владельцу Агентства."
        )

    total = len(visible)
    today_count = sum(1 for member in visible if _member_joined_today(member))
    waiting_count = sum(
        1 for member in visible
        if (activation_by_id.get(int(member["telegram_id"]), {}).get("status")
            in {None, "awaiting_proof", "proof_submitted", "rejected"})
    )
    onboarding_count = sum(
        1 for activation in activations
        if activation.get("onboarding_status") in {"started", "in_progress"}
    )
    attention_count = sum(
        1 for activation in activations
        if activation.get("attention_level") in {"red", "orange"}
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Всего", total)
    c2.metric("Новые сегодня", today_count)
    c3.metric("Ждут подтверждения", waiting_count)
    c4.metric("Онбординг", onboarding_count)
    c5.metric("Требуют внимания", attention_count)

    if not visible:
        if is_root_owner:
            st.info("В Агентстве пока нет других зарегистрированных участников.")
        else:
            st.info("В вашей ветке пока нет зарегистрированных партнёров.")
        return

    filter_options = [
        "Все",
        "Новые сегодня",
        "Ждут подтверждения",
        "Онбординг",
        "Активные",
        "Требуют внимания",
    ]
    filter_value = st.segmented_control(
        "Фильтр",
        filter_options,
        default="Все",
        key=f"partner_filter_{current_telegram_id}",
    )
    query = st.text_input(
        "🔎 Имя, ник или пригласитель",
        key=f"partner_search_{current_telegram_id}",
        placeholder="Начните вводить имя, @username или имя пригласившего",
    ).strip().lower().lstrip("@")

    def matches(member):
        activation = activation_by_id.get(int(member["telegram_id"]))
        inviter = direct_inviter_member(members, member)
        if query:
            haystack = " ".join(
                [
                    str(member.get("first_name") or ""),
                    str(member.get("username") or ""),
                    str(member.get("member_code") or ""),
                    str(member.get("referrer_code") or ""),
                    _member_display_name(inviter),
                ]
            ).lower()
            if query not in haystack:
                return False
        status = (activation or {}).get("status")
        onboarding = (activation or {}).get("onboarding_status")
        attention = (activation or {}).get("attention_level")
        if filter_value == "Новые сегодня":
            return _member_joined_today(member)
        if filter_value == "Ждут подтверждения":
            return status in {None, "awaiting_proof", "proof_submitted", "rejected"}
        if filter_value == "Онбординг":
            return onboarding in {"started", "in_progress"}
        if filter_value == "Активные":
            return status in {"confirmed", "legacy_active"}
        if filter_value == "Требуют внимания":
            return attention in {"red", "orange"}
        return True

    filtered = sorted(
        [member for member in visible if matches(member)],
        key=_created_sort_key,
        reverse=True,
    )

    by_code, children = _member_maps(members)
    list_tab, tree_tab = st.tabs(["📋 Список", "🌳 Структура"])

    with list_tab:
        if not filtered:
            st.info("По выбранному фильтру партнёров нет.")
        for member in filtered[:500]:
            member_id = int(member["telegram_id"])
            activation = activation_by_id.get(member_id)
            name = str(member.get("first_name") or "Партнёр")
            username = str(member.get("username") or "").strip()
            depth = member.get("depth")
            lodges = int((activation or {}).get("lodges_count") or 0)
            step = int((activation or {}).get("onboarding_step") or 0)
            status_text = _compact_status(activation)
            attention_text = _attention_label(activation)
            inviter = direct_inviter_member(members, member)
            inviter_name = _member_display_name(inviter)
            ref_code = str(member.get("referrer_code") or "").strip()
            member_code = str(member.get("member_code") or "").strip()
            direct_children = len(children.get(member_code, []))

            if depth:
                line_text = f"линия {int(depth)}"
            elif is_root_owner:
                line_text = "вне основной цепочки"
            else:
                line_text = "линия не определена"

            with st.expander(
                f"{name} · {line_text} · {status_text}",
                expanded=False,
            ):
                if inviter_name:
                    st.markdown(f"**Пригласил:** {inviter_name}")
                else:
                    st.error("⚠️ Пригласитель не определён")

                cols = st.columns([1.4, 1, 1, 1])
                cols[0].write(f"**@{username}**" if username else "Без username")
                cols[1].write(f"**Ложи:** {lodges or '—'}")
                cols[2].write(f"**Неола:** {step}/7")
                cols[3].write(f"**Личных партнёров:** {direct_children}")

                st.caption(
                    f"В Агентстве с: {_member_joined_label(member)} · "
                    f"код: {member_code or '—'} · "
                    f"код пригласившего: {ref_code or '—'} · "
                    f"внимание: {attention_text}"
                )

                # Скриншот может подтвердить:
                # 1) прямой пригласивший;
                # 2) корневой владелец кабинета — для любого человека в Агентстве.
                can_review_activation = (
                    str(member.get("referrer_code") or "") == str(current_member_code)
                    or is_root_owner
                )

                # Финансовое доказательство НЕ исчезает после подтверждения.
                proof = load_activation_proof(member_id) if can_review_activation else None
                has_proof = bool(proof and proof.get("proof_image_base64"))

                if has_proof:
                    with st.expander("🧾 Подтверждение 5 лож Neonexa", expanded=False):
                        if (
                            is_root_owner
                            and str(member.get("referrer_code") or "") != str(current_member_code)
                        ):
                            st.caption(
                                "👑 Вы видите это подтверждение как владелец Агентства."
                            )

                        try:
                            raw = base64.b64decode(proof["proof_image_base64"])
                            st.image(
                                raw,
                                caption="Финансовое подтверждение Neonexa",
                                width=420,
                            )
                        except Exception:
                            st.warning(
                                "Скриншот сохранён, но не удалось показать предпросмотр."
                            )

                        submitted_at = proof.get("submitted_at") or "—"
                        reviewed_at = proof.get("reviewed_at") or "—"
                        reviewer = proof.get("reviewed_by") or "—"
                        proof_lodges = int(proof.get("lodges_count") or 0)
                        proof_status = str(proof.get("status") or "")

                        st.caption(
                            f"Файл: {proof.get('proof_filename') or '—'} · "
                            f"загружен: {submitted_at}"
                        )

                        if proof_status in {"confirmed", "legacy_active"}:
                            st.success(
                                f"✅ Подтверждено лож: {proof_lodges or '—'} · "
                                f"кем: {reviewer} · дата: {reviewed_at}"
                            )
                        elif proof_status == "proof_submitted":
                            st.warning("⏳ Скриншот ждёт подтверждения.")
                        elif proof_status == "rejected":
                            st.warning(
                                "↩️ Этот скриншот был отклонён. "
                                "Он сохранён в истории как доказательство проверки."
                            )

                        ai_result = (proof or {}).get("proof_ai_result") or {}
                        if ai_result:
                            st.info(
                                "Предварительный разбор ИИ: "
                                f"ник — {ai_result.get('nickname') or 'не найден'}; "
                                f"ложи — {ai_result.get('lodges_count') or 0}; "
                                f"уверенность — "
                                f"{ai_result.get('confidence') or 'не проверено'}."
                            )

                if (
                    can_review_activation
                    and (activation or {}).get("status") == "proof_submitted"
                ):
                    recognized_lodges = int((activation or {}).get("lodges_count") or 0)
                    confirmed_lodges = st.number_input(
                        "Сколько лож вы видите на скриншоте?",
                        min_value=0,
                        max_value=100000,
                        value=max(0, recognized_lodges),
                        step=1,
                        key=f"confirmed_lodges_{current_telegram_id}_{member_id}",
                        help=(
                            "ИИ только помогает прочитать скриншот. "
                            "Окончательное число подтверждает наставник "
                            "или владелец структуры."
                        ),
                    )
                    reason_key = f"reject_reason_{current_telegram_id}_{member_id}"
                    reject_reason = st.text_input(
                        "Если нужно отклонить — причина",
                        key=reason_key,
                        placeholder="Например: не виден ник или количество лож",
                    )
                    a, b = st.columns(2)
                    if a.button(
                        "✅ Подтвердить 5 лож",
                        type="primary",
                        key=f"confirm_lodges_{current_telegram_id}_{member_id}",
                        disabled=int(confirmed_lodges) < MIN_LODGES,
                    ):
                        try:
                            review_activation(
                                member_id,
                                current_telegram_id,
                                True,
                                confirmed_lodges=int(confirmed_lodges),
                            )
                            st.success(
                                "Партнёр активирован. "
                                "Скриншот остаётся в финансовом архиве."
                            )
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
                    if b.button(
                        "❌ Попросить новый скриншот",
                        key=f"reject_lodges_{current_telegram_id}_{member_id}",
                    ):
                        try:
                            review_activation(
                                member_id,
                                current_telegram_id,
                                False,
                                reject_reason,
                            )
                            st.warning(
                                "Партнёру будет показана просьба загрузить "
                                "новый скриншот. Старый останется в истории "
                                "до следующей загрузки."
                            )
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))

    with tree_tab:
        connected = descendants_for_member(members, current_member_code)
        connected_ids = {int(item.get("telegram_id") or 0) for item in connected}

        def tree_label(child, depth):
            child_id = int(child.get("telegram_id") or 0)
            activation = activation_by_id.get(child_id)
            child_code = str(child.get("member_code") or "")
            direct_children = len(
                [item for item in children.get(child_code, [])
                 if int(item.get("telegram_id") or 0) in visible_ids]
            )
            inviter = direct_inviter_member(members, child)
            inviter_text = _member_display_name(inviter) or "⚠️ не определён"
            return (
                f"{'↳ ' * min(depth, 4)}{child.get('first_name') or 'Партнёр'} "
                f"· пригласил: {inviter_text} · {_compact_status(activation)} · "
                f"личных: {direct_children}"
            )

        def render_branch(parent_code, depth=0, max_depth=20):
            if depth >= max_depth:
                st.caption("… глубина скрыта")
                return
            for child in children.get(parent_code, []):
                child_id = int(child.get("telegram_id") or 0)
                if child_id not in visible_ids:
                    continue
                child_code = str(child.get("member_code") or "")
                direct_visible_children = [
                    item for item in children.get(child_code, [])
                    if int(item.get("telegram_id") or 0) in visible_ids
                ]
                label = tree_label(child, depth)
                if direct_visible_children:
                    with st.expander(label, expanded=False):
                        st.caption(f"В Агентстве с: {_member_joined_label(child)}")
                        render_branch(child_code, depth + 1, max_depth)
                else:
                    st.write(label)

        st.markdown(f"**{current_name}**")
        render_branch(str(current_member_code))

        if is_root_owner:
            disconnected_ids = visible_ids - connected_ids
            if disconnected_ids:
                visible_codes = {
                    str(item.get("member_code") or "")
                    for item in visible
                    if int(item.get("telegram_id") or 0) in disconnected_ids
                }
                disconnected_roots = []
                for member in visible:
                    member_id = int(member.get("telegram_id") or 0)
                    if member_id not in disconnected_ids:
                        continue
                    ref = str(member.get("referrer_code") or "").strip()
                    if not ref or ref not in visible_codes:
                        disconnected_roots.append(member)

                st.divider()
                st.markdown("**⚠️ Вне основной цепочки приглашений**")
                st.caption(
                    "Эти люди зарегистрированы в Агентстве, но их связь с основной "
                    "структурой Валентины не определяется. Владелец всё равно видит их."
                )

                shown_roots = set()
                for root_member in sorted(
                    disconnected_roots,
                    key=_created_sort_key,
                    reverse=True,
                ):
                    root_id = int(root_member.get("telegram_id") or 0)
                    if root_id in shown_roots:
                        continue
                    shown_roots.add(root_id)
                    root_code = str(root_member.get("member_code") or "")
                    direct_visible_children = [
                        item for item in children.get(root_code, [])
                        if int(item.get("telegram_id") or 0) in visible_ids
                    ]
                    label = tree_label(root_member, 0)
                    if direct_visible_children:
                        with st.expander(label, expanded=False):
                            st.caption(
                                f"В Агентстве с: {_member_joined_label(root_member)}"
                            )
                            render_branch(root_code, 1, 20)
                    else:
                        st.write(label)


def _save_neola_message(telegram_id, role, content):
    try:
        _post_json(
            "neola_messages",
            {
                "telegram_id": int(telegram_id),
                "role": role,
                "content": str(content)[:8000],
                "created_at": _now_iso(),
            },
            prefer="return=minimal",
        )
    except Exception:
        # Чат остаётся работоспособным в session_state даже до миграции БД.
        pass


def _load_neola_messages(telegram_id, limit=30):
    try:
        rows = _get_json(
            "neola_messages",
            params={
                "telegram_id": f"eq.{int(telegram_id)}",
                "select": "role,content,created_at",
                "order": "created_at.asc",
                "limit": str(int(limit)),
            },
        )
        return rows
    except Exception:
        return []


def _transcribe_audio(audio_file):
    api_key = st.secrets.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не найден.")
    audio_bytes = audio_file.getvalue()
    mime = getattr(audio_file, "type", None) or "audio/wav"
    name = getattr(audio_file, "name", None) or "neola_voice.wav"
    response = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        data={
            "model": "gpt-4o-mini-transcribe",
            "language": "ru",
            "response_format": "json",
        },
        files={"file": (name, audio_bytes, mime)},
        timeout=90,
    )
    response.raise_for_status()
    return str(response.json().get("text") or "").strip()


def _synthesize_speech(text):
    api_key = st.secrets.get("OPENAI_API_KEY")
    if not api_key or not text:
        return None
    response = requests.post(
        "https://api.openai.com/v1/audio/speech",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-4o-mini-tts",
            "voice": "marin",
            "input": str(text)[:4000],
            "instructions": (
                "Говори по-русски. Тёплый, спокойный, уверенный женский голос "
                "наставника. Короткие ясные фразы, без торопливости."
            ),
            "response_format": "mp3",
        },
        timeout=90,
    )
    response.raise_for_status()
    return response.content


def _neola_system_prompt(owner_name, ui_context, activation, member):
    onboarding_step = int((activation or {}).get("onboarding_step") or 0)
    cabinet_map = neola_cabinet_knowledge()
    return f"""
Ты — Неола, голосовой наставник партнёра в Агентстве W.
Партнёр: {owner_name}.
Текущий статус: {activation_label(activation)}.
Шаг онбординга: {onboarding_step}/7.
Текущий интерфейс: {ui_context}.
Код партнёра: {str((member or {}).get('member_code') or '')}.

Твоя главная миссия:
{NEOLA_MISSION}

Ты должна знать, где находятся разделы и кнопки, понимать порядок действий и вести человека по одному шагу.
Ты не читаешь курс и не заменяешь других агентов. Навигация нужна для того, чтобы обучать реальной работе, а не просто показывать меню.

ПРАВИЛА РАБОТЫ:
- Официальное название всегда: «Агентство W» — одна буква W. Никогда не говори и не пиши «Агентство WW».
- Говори просто, тепло и коротко. Обычно 1–3 коротких предложения.
- Один шаг = одно действие. После действия дождись ответа человека.
- Всегда учитывай текущий экран: {ui_context}.
- Если человек уже находится в нужном разделе, продолжай оттуда.
- Никогда не выдумывай кнопку, вкладку, статус или выполненное действие.
- Если реальный экран отличается от карты, доверься экрану пользователя: спроси, что на нём написано.
- Если человеку трудно, объясни проще; если просит повторить — повтори только последний шаг.
- Не выбирай кандидатов вместо владельца.
- Не редактируй и не оценивай тексты Неоны. Покажи, где владелец сам может их изменить.
- Не выполняй работу Неонии, Неоны, Стагирита или календаря вместо них.
- Telegram подключается один раз. Если он уже подключён, не предлагай подключать его снова.

АКТУАЛЬНАЯ КАРТА КАБИНЕТА:
{cabinet_map}

Если пользователь спрашивает «где это?», сначала найди точный маршрут в карте выше, но озвучь только один следующий шаг.
Если точного маршрута в карте нет, честно скажи, что не хочешь придумывать, и попроси назвать элементы текущего экрана.
""".strip()


def _ask_neola(owner_name, telegram_id, user_text, ui_context, activation, member, ask_openai_fn):
    history_key = f"neola_session_history_{telegram_id}"
    if history_key not in st.session_state:
        persisted = _load_neola_messages(telegram_id)
        st.session_state[history_key] = [
            {"role": item.get("role"), "content": item.get("content")}
            for item in persisted[-20:]
            if item.get("role") in {"user", "assistant"}
        ]
    history = st.session_state[history_key]
    history_text = "\n".join(
        f"{('Партнёр' if item['role'] == 'user' else 'Неола')}: {item['content']}"
        for item in history[-12:]
    )
    prompt = _neola_system_prompt(owner_name, ui_context, activation, member)
    request = (
        f"ПОСЛЕДНИЙ ДИАЛОГ:\n{history_text}\n\n"
        f"НОВОЕ СООБЩЕНИЕ ПАРТНЁРА:\n{user_text}\n\n"
        "Ответь как Неола. Не повторяй уже выполненные шаги."
    )
    answer = ask_openai_fn(prompt, request)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": answer})
    st.session_state[history_key] = history[-30:]
    _save_neola_message(telegram_id, "user", user_text)
    _save_neola_message(telegram_id, "assistant", answer)
    try:
        _patch_json(
            "partner_activations",
            {"telegram_id": f"eq.{int(telegram_id)}"},
            {
                "last_action_at": _now_iso(),
                "onboarding_status": "in_progress",
                "attention_level": "yellow",
            },
            prefer="return=minimal",
        )
    except Exception:
        pass
    return answer


def _render_neola_conversation(telegram_id, owner_name, ui_context, ask_openai_fn, compact=False):
    activation = ensure_partner_activation(telegram_id)
    member = get_member_by_telegram_id(telegram_id)

    if not activation_is_confirmed(activation):
        st.warning("Неола включится после подтверждения 5 лож.")
        st.caption(activation_label(activation))
        return

    history_key = f"neola_session_history_{telegram_id}"
    if history_key not in st.session_state:
        persisted = _load_neola_messages(telegram_id)
        st.session_state[history_key] = [
            {"role": item.get("role"), "content": item.get("content")}
            for item in persisted[-20:]
            if item.get("role") in {"user", "assistant"}
        ]

    history = st.session_state[history_key]
    if not history:
        greeting = neola_first_greeting(owner_name)
        history.append({"role": "assistant", "content": greeting})
        _save_neola_message(telegram_id, "assistant", greeting)
        st.session_state[history_key] = history

    show_count = 6 if compact else 14
    for item in history[-show_count:]:
        role_name = "Вы" if item["role"] == "user" else "Неола"
        st.markdown(f"**{role_name}:** {item['content']}")

    st.caption(f"Сейчас Неола видит контекст: {ui_context}")

    voice_text = ""
    if hasattr(st, "audio_input"):
        audio = st.audio_input(
            "🎙 Скажите Неоле",
            key=f"neola_audio_{telegram_id}_{'compact' if compact else 'full'}",
        )
        if audio is not None:
            audio_hash = hash(audio.getvalue())
            processed_key = f"neola_processed_audio_{telegram_id}_{audio_hash}"
            if not st.session_state.get(processed_key):
                try:
                    with st.spinner("Неола слушает..."):
                        voice_text = _transcribe_audio(audio)
                    st.session_state[processed_key] = True
                except Exception as exc:
                    st.error(f"Не удалось распознать голос: {exc}")
    else:
        st.caption("В этой версии Streamlit запись с микрофона недоступна; используйте текст.")

    typed_text = st.text_input(
        "Сообщение Неоле",
        key=f"neola_text_{telegram_id}_{'compact' if compact else 'full'}",
        placeholder="Например: Я уже в Telegram. Как изменить сообщение?",
    )

    user_text = voice_text or typed_text.strip()
    send_clicked = False
    if voice_text:
        st.info(f"Вы сказали: {voice_text}")
        send_clicked = st.button(
            "Отправить распознанный вопрос",
            type="primary",
            key=f"neola_send_voice_{telegram_id}_{'compact' if compact else 'full'}",
        )
    else:
        send_clicked = st.button(
            "Спросить Неолу",
            type="primary",
            key=f"neola_send_text_{telegram_id}_{'compact' if compact else 'full'}",
            disabled=not bool(user_text),
        )

    if send_clicked and user_text:
        try:
            with st.spinner("Неола отвечает..."):
                answer = _ask_neola(
                    owner_name,
                    telegram_id,
                    user_text,
                    ui_context,
                    activation,
                    member,
                    ask_openai_fn,
                )
                speech = _synthesize_speech(answer)
            st.success(answer)
            if speech:
                st.audio(speech, format="audio/mp3", autoplay=True)
        except Exception as exc:
            st.error(f"Неола не смогла ответить: {exc}")


def render_neola_quick_assistant(telegram_id, owner_name, ui_context, ask_openai_fn):
    activation = ensure_partner_activation(telegram_id)
    label = "🎙 Неола рядом" if activation_is_confirmed(activation) else "🔒 Неола"

    if hasattr(st, "popover"):
        with st.popover(label, use_container_width=True):
            st.markdown("#### 🎙 Неола рядом")
            _render_neola_conversation(
                telegram_id,
                owner_name,
                ui_context,
                ask_openai_fn,
                compact=True,
            )
    else:
        with st.expander(label):
            _render_neola_conversation(
                telegram_id,
                owner_name,
                ui_context,
                ask_openai_fn,
                compact=True,
            )


def render_neola_agent(telegram_id, owner_name, ui_context, ask_openai_fn):
    st.caption(
        "Неола — голосовой наставник. Она ведёт по реальным действиям, знает "
        "вложенную навигацию Агентства W и оставляет текстовые шаги в чате."
    )
    activation = ensure_partner_activation(telegram_id)
    if not activation_is_confirmed(activation):
        with st.container(border=True):
            render_my_activation(telegram_id)
        return

    step = int((activation or {}).get("onboarding_step") or 0)
    st.progress(min(max(step / 7.0, 0.0), 1.0), text=f"Прогресс Неолы: {step}/7")
    with st.container(border=True):
        _render_neola_conversation(
            telegram_id,
            owner_name,
            ui_context,
            ask_openai_fn,
            compact=False,
        )
