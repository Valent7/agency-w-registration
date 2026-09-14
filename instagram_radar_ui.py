import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
import streamlit as st
from cryptography.fernet import Fernet, InvalidToken


USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def _supabase_headers() -> dict:
    key = str(st.secrets.get("SUPABASE_SECRET_KEY") or "").strip()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _load_radar_connection(owner_telegram_id: int) -> dict | None:
    url = str(st.secrets.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = str(st.secrets.get("SUPABASE_SECRET_KEY") or "").strip()
    if not url or not key:
        return None
    try:
        response = requests.get(
            f"{url}/rest/v1/agency_instagram_radar_connections",
            headers=_supabase_headers(),
            params={
                "owner_telegram_id": f"eq.{int(owner_telegram_id)}",
                "status": "eq.connected",
                "select": (
                    "owner_telegram_id,facebook_page_id,facebook_page_name,"
                    "instagram_account_id,instagram_username,"
                    "page_access_token_encrypted,token_expires_at,status,connected_at"
                ),
                "limit": 1,
            },
            timeout=12,
        )
        response.raise_for_status()
        rows = response.json() if response.text.strip() else []
        return dict(rows[0]) if isinstance(rows, list) and rows else None
    except Exception:
        return None


def _cipher() -> Fernet:
    key = str(st.secrets.get("FERNET_KEY") or "").strip()
    if not key:
        raise RuntimeError("FERNET_KEY не найден в Streamlit Secrets.")
    return Fernet(key.encode("utf-8"))


def _decrypt_secret(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        return _cipher().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Не удалось расшифровать токен Instagram Radar.") from exc


def _radar_connect_state(owner_telegram_id: int, owner_name: str) -> str:
    payload = {
        "owner_id": int(owner_telegram_id),
        "owner_name": str(owner_name or "").strip(),
        "purpose": "agency_w_instagram_radar_connect",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return _cipher().encrypt(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("utf-8")


def _radar_connect_url(owner_telegram_id: int, owner_name: str) -> str:
    base_url = str(
        st.secrets.get("INSTAGRAM_OAUTH_SERVICE_URL") or ""
    ).strip().rstrip("/")
    if not base_url:
        raise RuntimeError("INSTAGRAM_OAUTH_SERVICE_URL не найден в Streamlit Secrets.")
    state = _radar_connect_state(owner_telegram_id, owner_name)
    return f"{base_url}/facebook/connect?" + urlencode({"state": state})


def _target_audience_text(owner_telegram_id: int) -> str:
    passport_key = f"neonia_target_audience_passport_{int(owner_telegram_id)}"
    passport = st.session_state.get(passport_key)
    if not isinstance(passport, dict):
        return ""
    analysis = str(passport.get("analysis") or "").strip()
    if analysis:
        return analysis
    profile = passport.get("profile")
    if isinstance(profile, dict):
        return json.dumps(profile, ensure_ascii=False, indent=2)
    return ""


def _extract_json(text: str):
    raw = str(text or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    for candidate in (raw,):
        try:
            return json.loads(candidate)
        except Exception:
            pass
    object_match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if object_match:
        try:
            return json.loads(object_match.group(0))
        except Exception:
            pass
    array_match = re.search(r"\[.*\]", raw, flags=re.DOTALL)
    if array_match:
        try:
            return json.loads(array_match.group(0))
        except Exception:
            pass
    return None


def _normalize_username(value: str) -> str:
    value = str(value or "").strip()
    value = value.replace("https://www.instagram.com/", "")
    value = value.replace("https://instagram.com/", "")
    value = value.split("?", 1)[0].split("#", 1)[0].strip("/")
    value = value.lstrip("@").strip()
    return value if USERNAME_RE.fullmatch(value) else ""


def _discover_usernames(
    ask_openai_fn,
    target_audience: str,
    manual_usernames: list[str],
) -> list[str]:
    system_prompt = """
Ты — Неония, агент подбора кандидатов Агентства W.
Используй web search и найди публичные Instagram-аккаунты, максимально близкие к
заданному портрету целевой аудитории. Нас интересуют прежде всего профессиональные
аккаунты Business/Creator, а не случайные личные страницы.

Нужны реальные Instagram usernames, которые можно проверить через официальный
Instagram Graph API. Не придумывай username. Не включай магазины и крупные бренды,
если портрет ЦА описывает человека-предпринимателя или специалиста.

Верни ТОЛЬКО JSON:
{"usernames":["name1","name2",...]}

Дай до 20 usernames, без @, без пояснений и без повторов.
""".strip()
    user_message = (
        "Портрет целевой аудитории:\n"
        + target_audience
        + "\n\nНайди подходящие публичные профессиональные Instagram-аккаунты."
    )
    raw = ask_openai_fn(
        system_prompt,
        user_message,
        use_web_search=True,
    )
    payload = _extract_json(raw)
    found = []
    if isinstance(payload, dict):
        candidates = payload.get("usernames") or []
        if isinstance(candidates, list):
            found.extend(candidates)

    if not found:
        found.extend(
            re.findall(
                r"(?:instagram\.com/|@)([A-Za-z0-9._]{2,30})",
                str(raw or ""),
                flags=re.IGNORECASE,
            )
        )

    found.extend(manual_usernames)

    result = []
    seen = set()
    for value in found:
        username = _normalize_username(value)
        key = username.casefold()
        if not username or key in seen:
            continue
        seen.add(key)
        result.append(username)
        if len(result) >= 24:
            break
    return result


def _graph_business_profile(
    instagram_account_id: str,
    page_access_token: str,
    username: str,
) -> dict | None:
    api_version = str(
        st.secrets.get("FACEBOOK_API_VERSION") or "v23.0"
    ).strip() or "v23.0"
    fields = (
        f"business_discovery.username({username})"
        "{id,username,name,biography,followers_count,follows_count,"
        "media_count,website,"
        "media.limit(3){id,caption,media_type,permalink,timestamp,"
        "thumbnail_url,media_url}}"
    )
    response = requests.get(
        f"https://graph.facebook.com/{api_version}/{instagram_account_id}",
        params={
            "fields": fields,
            "access_token": page_access_token,
        },
        timeout=25,
    )
    if not response.ok:
        return None
    payload = response.json() if response.text.strip() else {}
    business = payload.get("business_discovery") if isinstance(payload, dict) else None
    return dict(business) if isinstance(business, dict) else None


def _parse_timestamp(value: str) -> datetime | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _collect_recent_posts(
    usernames: list[str],
    connection: dict,
    *,
    max_age_days: int = 45,
    limit: int = 10,
) -> tuple[list[dict], int]:
    token = _decrypt_secret(connection.get("page_access_token_encrypted") or "")
    own_ig_id = str(connection.get("instagram_account_id") or "").strip()
    own_username = str(connection.get("instagram_username") or "").strip().casefold()
    if not token or not own_ig_id:
        raise RuntimeError("Instagram Radar подключён неполно. Переподключите его.")

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    profiles = []
    checked = 0

    for username in usernames:
        if username.casefold() == own_username:
            continue
        checked += 1
        profile = _graph_business_profile(own_ig_id, token, username)
        if not profile:
            continue
        media_data = (
            ((profile.get("media") or {}).get("data"))
            if isinstance(profile.get("media"), dict)
            else []
        ) or []
        posts = []
        for media in media_data:
            if not isinstance(media, dict):
                continue
            timestamp = _parse_timestamp(media.get("timestamp"))
            if timestamp and timestamp < cutoff:
                continue
            permalink = str(media.get("permalink") or "").strip()
            if not permalink:
                continue
            posts.append(
                {
                    "media_id": str(media.get("id") or "").strip(),
                    "caption": str(media.get("caption") or "").strip(),
                    "media_type": str(media.get("media_type") or "").strip(),
                    "permalink": permalink,
                    "timestamp": timestamp.isoformat() if timestamp else "",
                    "thumbnail_url": str(media.get("thumbnail_url") or "").strip(),
                    "media_url": str(media.get("media_url") or "").strip(),
                }
            )
        posts.sort(
            key=lambda row: row.get("timestamp") or "",
            reverse=True,
        )
        if posts:
            profiles.append(
                {
                    "username": str(profile.get("username") or username).strip(),
                    "name": str(profile.get("name") or "").strip(),
                    "biography": str(profile.get("biography") or "").strip(),
                    "followers_count": profile.get("followers_count"),
                    "website": str(profile.get("website") or "").strip(),
                    "posts": posts,
                }
            )
        if len(profiles) >= 14:
            break

    # First pass: one freshest post per account, to keep the radar diverse.
    chosen = []
    for profile in profiles:
        if profile["posts"]:
            post = dict(profile["posts"][0])
            post.update(
                {
                    "username": profile["username"],
                    "name": profile["name"],
                    "biography": profile["biography"],
                    "followers_count": profile["followers_count"],
                }
            )
            chosen.append(post)

    chosen.sort(key=lambda row: row.get("timestamp") or "", reverse=True)

    # If fewer than 10 accounts were found, fill the rest with a second/third
    # recent post from already verified professional accounts.
    if len(chosen) < limit:
        used_ids = {row["media_id"] for row in chosen}
        extras = []
        for profile in profiles:
            for media in profile["posts"][1:]:
                if media["media_id"] in used_ids:
                    continue
                row = dict(media)
                row.update(
                    {
                        "username": profile["username"],
                        "name": profile["name"],
                        "biography": profile["biography"],
                        "followers_count": profile["followers_count"],
                    }
                )
                extras.append(row)
        extras.sort(key=lambda row: row.get("timestamp") or "", reverse=True)
        chosen.extend(extras[: max(0, limit - len(chosen))])

    return chosen[:limit], checked


def _openai_story_comments(posts: list[dict]) -> list[dict]:
    if not posts:
        return []
    api_key = str(st.secrets.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не найден в Streamlit Secrets.")

    manifest = []
    for index, post in enumerate(posts, start=1):
        manifest.append(
            {
                "index": index,
                "username": post.get("username"),
                "name": post.get("name"),
                "media_type": post.get("media_type"),
                "caption": post.get("caption"),
                "biography": post.get("biography"),
                "timestamp": post.get("timestamp"),
            }
        )

    system_prompt = """
Ты — Неона. Ты помогаешь владельцу Агентства W естественно знакомиться с людьми
через Instagram. Эти аккаунты уже предварительно отобраны Неонией как подходящие
по целевой аудитории и подтверждены официальным Instagram API как профессиональные.

Для КАЖДОЙ переданной публикации подготовь один короткий человеческий комментарий,
который владелец сможет вручную оставить под Reel или постом.

Правила:
- до 10 комментариев, по одному на публикацию;
- 1–2 коротких предложения;
- опирайся только на подпись и видимый превью-кадр, ничего не выдумывай;
- даже простой бытовой пост можно тепло отметить, если человек нам подходит;
- не упоминай Агентство W, ИИ, партнёрство, доход, бизнес-предложение или ссылку;
- не продавай и не приглашай на встречу;
- вопрос только если он звучит естественно, не обязан быть в каждом комментарии;
- избегай пустых фраз вроде «Классный пост!» без конкретики;
- не оценивай возраст, здоровье, достаток или внешность человека;
- не повторяй одинаковые формулировки.

Верни ТОЛЬКО JSON-массив:
[{"index":1,"reason":"коротко почему комментарий уместен","comment":"текст"}]
""".strip()

    content = [
        {
            "type": "input_text",
            "text": (
                "Публикации:\n"
                + json.dumps(manifest, ensure_ascii=False, indent=2)
                + "\n\nПодготовь комментарий к каждой публикации."
            ),
        }
    ]
    for index, post in enumerate(posts, start=1):
        preview = str(
            post.get("thumbnail_url")
            or post.get("media_url")
            or ""
        ).strip()
        if preview:
            content.append(
                {
                    "type": "input_text",
                    "text": f"Превью публикации #{index}:",
                }
            )
            content.append(
                {
                    "type": "input_image",
                    "image_url": preview,
                    "detail": "low",
                }
            )

    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": str(
                st.secrets.get("INSTAGRAM_RADAR_OPENAI_MODEL")
                or "gpt-5-mini"
            ),
            "instructions": system_prompt,
            "input": [{"role": "user", "content": content}],
            "store": False,
        },
        timeout=180,
    )
    if not response.ok:
        raise RuntimeError(
            f"OpenAI Instagram Radar HTTP {response.status_code}: "
            f"{response.text[:800]}"
        )
    payload = response.json()
    pieces = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                pieces.append(str(part.get("text") or ""))
    parsed = _extract_json("\n".join(pieces))
    return parsed if isinstance(parsed, list) else []


def _merge_comments(posts: list[dict], comments: list[dict]) -> list[dict]:
    by_index = {}
    for item in comments:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except Exception:
            continue
        by_index[index] = item

    result = []
    for index, post in enumerate(posts, start=1):
        comment = by_index.get(index) or {}
        row = dict(post)
        row["reason"] = str(comment.get("reason") or "").strip()
        row["comment"] = str(comment.get("comment") or "").strip()
        result.append(row)
    return result


def render_instagram_radar(
    owner_telegram_id: int,
    owner_name: str,
    ask_openai_fn,
) -> None:
    st.markdown("### 🌿 Instagram Radar")
    st.caption(
        "Неония находит подходящие профессиональные аккаунты, Radar проверяет их "
        "через официальный Instagram API и берёт свежие Reels/посты. "
        "Неона готовит комментарии, а отправляете их пока вы сами."
    )

    connection = _load_radar_connection(int(owner_telegram_id))
    if not connection:
        if str(st.query_params.get("instagram_radar") or "").strip() == "connected":
            st.info(
                "Facebook авторизация завершена. Обновите страницу, "
                "если статус ещё не стал зелёным."
            )
        with st.container(border=True):
            st.markdown("**Подключение Instagram Radar**")
            st.caption(
                "Это отдельный официальный Facebook Login для Business Discovery. "
                "Ваш действующий Instagram Direct он не меняет."
            )
            try:
                connect_url = _radar_connect_url(owner_telegram_id, owner_name)
            except Exception as exc:
                st.warning("Instagram Radar ещё не готов к подключению.")
                st.caption(f"Техническая причина: {exc}")
                return
            st.link_button(
                "🔗 Подключить Instagram Radar через Facebook",
                connect_url,
                use_container_width=True,
            )
        return

    username = str(connection.get("instagram_username") or "").strip()
    page_name = str(connection.get("facebook_page_name") or "").strip()
    suffix = f" · @{username}" if username else ""
    if page_name:
        suffix += f" · {page_name}"
    st.success(f"🟢 Instagram Radar подключён{suffix}")

    target_audience = _target_audience_text(int(owner_telegram_id))
    if not target_audience:
        st.warning(
            "Сначала откройте «Определить мою целевую аудиторию» и сохраните портрет ЦА. "
            "Без него Неония не сможет качественно искать Instagram-кандидатов."
        )
        return

    with st.expander("➕ Добавить известные Instagram usernames — необязательно"):
        manual_text = st.text_area(
            "По одному username или ссылке на строку",
            placeholder="name1\nhttps://instagram.com/name2",
            key=f"instagram_radar_manual_{owner_telegram_id}",
            height=100,
        )

    state_key = f"instagram_radar_results_{owner_telegram_id}"

    if st.button(
        "🔎 Найти 10 свежих публикаций Instagram",
        key=f"instagram_radar_scan_{owner_telegram_id}",
        use_container_width=True,
        type="primary",
    ):
        manual_usernames = [
            value.strip()
            for value in re.split(r"[\n,;]+", manual_text or "")
            if value.strip()
        ]
        try:
            with st.spinner("Неония ищет подходящие профессиональные аккаунты..."):
                usernames = _discover_usernames(
                    ask_openai_fn,
                    target_audience,
                    manual_usernames,
                )
            if not usernames:
                st.warning("Неония пока не нашла подходящих Instagram usernames.")
                return

            with st.spinner(
                "Instagram Radar проверяет аккаунты и собирает свежие Reels/посты..."
            ):
                posts, checked = _collect_recent_posts(
                    usernames,
                    connection,
                    max_age_days=45,
                    limit=10,
                )

            if not posts:
                st.warning(
                    "Подходящие профессиональные аккаунты нашлись, "
                    "но свежих доступных публикаций для теста не оказалось."
                )
                st.caption(f"Проверено usernames: {checked}.")
                return

            with st.spinner("Неона готовит естественные комментарии..."):
                comments = _openai_story_comments(posts)
                results = _merge_comments(posts, comments)

            st.session_state[state_key] = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "checked": checked,
                "usernames": usernames,
                "results": results,
            }
        except Exception as exc:
            st.error(f"Instagram Radar: {exc}")
            return

    radar_state = st.session_state.get(state_key)
    if not isinstance(radar_state, dict):
        st.info(
            "Первый запуск ничего не отправит. Мы только посмотрим, "
            "кого нашла Неония и какие комментарии предлагает Неона."
        )
        return

    results = radar_state.get("results") or []
    checked = int(radar_state.get("checked") or 0)
    st.caption(
        f"Проверено профессиональных аккаунтов: {checked} · "
        f"Свежих публикаций в Radar: {len(results)}"
    )

    for index, item in enumerate(results, start=1):
        username = str(item.get("username") or "").strip()
        name = str(item.get("name") or "").strip()
        caption = str(item.get("caption") or "").strip()
        permalink = str(item.get("permalink") or "").strip()
        media_type = str(item.get("media_type") or "").strip()
        timestamp = str(item.get("timestamp") or "").strip()
        preview = str(
            item.get("thumbnail_url")
            or item.get("media_url")
            or ""
        ).strip()
        reason = str(item.get("reason") or "").strip()
        default_comment = str(item.get("comment") or "").strip()

        with st.container(border=True):
            title = f"**{index}. @{username}**"
            if name:
                title += f" · {name}"
            st.markdown(title)
            meta = media_type or "Публикация"
            if timestamp:
                try:
                    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    meta += f" · {dt.strftime('%d.%m.%Y %H:%M')}"
                except Exception:
                    pass
            st.caption(meta)

            if preview:
                try:
                    st.image(preview, width=360)
                except Exception:
                    pass

            if caption:
                short_caption = caption if len(caption) <= 900 else caption[:900] + "…"
                st.write(short_caption)
            else:
                st.caption("Подпись к публикации отсутствует.")

            if reason:
                st.caption(f"Почему Неона выбрала этот отклик: {reason}")

            comment_key = (
                f"instagram_radar_comment_{owner_telegram_id}_"
                f"{item.get('media_id') or index}"
            )
            if comment_key not in st.session_state:
                st.session_state[comment_key] = default_comment
            st.text_area(
                "Комментарий Неоны",
                key=comment_key,
                height=90,
            )

            if permalink:
                st.link_button(
                    "↗️ Открыть публикацию в Instagram",
                    permalink,
                    use_container_width=True,
                )

    st.caption(
        "Сейчас отправка ручная: откройте публикацию, скопируйте комментарий Неоны "
        "и вставьте его в Instagram. Автоматическое комментирование чужих публикаций "
        "мы не включаем."
    )
