import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from openai import OpenAI


BASE_DIR = Path(__file__).resolve().parent

THEO_MODEL = os.getenv("THEO_MODEL", "gpt-5.6").strip() or "gpt-5.6"
CONSTITUTION_FILE = BASE_DIR / "THEO_CONSTITUTION.txt"
KNOWLEDGE_FILE = BASE_DIR / "THEO_KNOWLEDGE_BASE.txt"

ALLOWED_STAGES = {"consulting", "interested", "meeting_ready", "closed"}


THEO_FALLBACK_RULES = """
Ты — Тео, консультант Агентства W в VK-сообществе.

Твоя роль:
— содержательно отвечать на вопросы об Агентстве W;
— сначала отвечать на вопрос, а не превращать каждый ответ в вопрос;
— объяснять простым языком;
— показывать применимость Агентства W к ситуации человека только по известным данным;
— не выдумывать функции, цифры, цены, результаты и факты о человеке;
— не обещать доход, клиентов, партнёров или гарантированный результат;
— при реальном интересе мягко предложить короткую встречу с Директором,
  который пригласил человека в сообщество;
— никогда не говорить, что встреча уже назначена, если календарная запись
  технически не создана;
— если точного ответа нет, честно сказать, что подтверждённого ответа нет,
  и предложить уточнить у Директора.

Главная идея Агентства W:
«Мы возвращаем человеку время».

Агентство W — система специализированных ИИ-помощников:
Неония — анализ проекта, целевой аудитории и подбор подходящих людей.
Неона — первичное общение и создание интереса.
Тео — консультация и понимание возможностей Агентства W.
Неола — сопровождение партнёра после регистрации и активации.
Стагирит — координация и порядок всей системы.
Человек остаётся Директором и принимает ключевые решения.

Стиль:
спокойный, умный, доброжелательный, без рекламного пафоса.
Обычно 1–4 коротких абзаца.
Не задавай вопрос только ради продолжения разговора.
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _supabase_config() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SECRET_KEY")
    return url, key


def _headers(prefer: str = "") -> dict[str, str]:
    _, key = _supabase_config()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _sb_get(params: dict) -> list[dict]:
    url, _ = _supabase_config()
    response = requests.get(
        f"{url}/rest/v1/agency_theo_vk_dialogs",
        headers=_headers(),
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json() if response.text.strip() else []
    return data if isinstance(data, list) else []


def _sb_post(payload: dict) -> list[dict]:
    url, _ = _supabase_config()
    response = requests.post(
        f"{url}/rest/v1/agency_theo_vk_dialogs?on_conflict=vk_user_id",
        headers=_headers(
            "resolution=merge-duplicates,return=representation"
        ),
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json() if response.text.strip() else []
    return data if isinstance(data, list) else []


def _read_text(path: Path) -> str:
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        print(
            "THEO_KNOWLEDGE_READ_WARNING:",
            f"{path.name}: {type(exc).__name__}: {exc}",
            flush=True,
        )
    return ""


def _knowledge_bundle() -> str:
    constitution = _read_text(CONSTITUTION_FILE)
    knowledge = _read_text(KNOWLEDGE_FILE)

    parts = [THEO_FALLBACK_RULES]

    if constitution:
        parts.append(
            "\n\n===== КОНСТИТУЦИЯ ТЕО =====\n"
            + constitution[:30000]
        )

    if knowledge:
        parts.append(
            "\n\n===== БАЗА ЗНАНИЙ АГЕНТСТВА W =====\n"
            + knowledge[:60000]
        )

    return "".join(parts)


def _load_dialog(vk_user_id: int) -> dict | None:
    rows = _sb_get(
        {
            "vk_user_id": f"eq.{int(vk_user_id)}",
            "select": "*",
            "limit": 1,
        }
    )
    return rows[0] if rows else None


def _clean_history(value) -> list[dict]:
    if not isinstance(value, list):
        return []

    result = []

    for item in value[-24:]:
        if not isinstance(item, dict):
            continue

        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()

        if role not in {"user", "assistant"} or not content:
            continue

        result.append(
            {
                "role": role,
                "content": content[:2500],
                "at": str(item.get("at") or "").strip() or None,
            }
        )

    return result


def _strip_json_fence(text: str) -> str:
    value = str(text or "").strip()

    value = re.sub(
        r"^```(?:json)?\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\s*```$",
        "",
        value,
    )

    return value.strip()


def _parse_model_result(raw: str) -> dict:
    clean = _strip_json_fence(raw)

    try:
        data = json.loads(clean)
    except Exception:
        data = {"reply": clean}

    if not isinstance(data, dict):
        data = {"reply": str(clean)}

    reply = str(data.get("reply") or "").strip()
    stage = str(
        data.get("stage") or "consulting"
    ).strip()
    summary = str(
        data.get("summary") or ""
    ).strip()

    if stage not in ALLOWED_STAGES:
        stage = "consulting"

    if not reply:
        raise RuntimeError(
            "Theo returned an empty reply."
        )

    return {
        "reply": reply,
        "stage": stage,
        "summary": summary[:2000],
    }


def _build_instructions(
    owner_name: str,
    first_contact: bool,
) -> str:

    owner = (
        str(owner_name or "").strip()
        or "Директор"
    )

    if first_contact:
        intro_rule = (
            "Это первое сообщение Тео этому человеку. "
            "Представься один раз: "
            "«Я Тео, консультант Агентства W». "
        )
    else:
        intro_rule = (
            "Вы уже общались. "
            "Не представляйся заново без причины. "
        )

    return f"""
{_knowledge_bundle()}

===== ТЕКУЩАЯ ЗАДАЧА =====

Ты отвечаешь человеку в сообщениях официального VK-сообщества
Агентства W.

{intro_rule}

Директор, который закреплён за этим человеком:
{owner}.

Алгоритм ответа:

1. Сначала пойми, что именно человек спросил или сообщил.

2. Дай содержательный ответ на его вопрос.

3. Если уместно, покажи, как это относится к его ситуации.

4. Не задавай лишний вопрос.

5. Если человек явно заинтересован в демонстрации,
подключении, обсуждении своего проекта, условиях
или личной консультации —
предложи короткую встречу с Директором {owner}.

6. Не назначай время самостоятельно и не говори,
что встреча внесена в календарь.

7. При явном отказе уважительно заверши тему.

8. Если точного подтверждённого ответа нет —
не фантазируй.

Определи stage:

consulting —
обычная консультация.

interested —
человек проявил заметный интерес,
но встречу ещё не просит.

meeting_ready —
человек готов обсудить систему с Директором
или согласен на встречу.

closed —
человек отказался или попросил прекратить тему.

Верни ТОЛЬКО JSON без markdown:

{{
  "reply": "готовый ответ человеку",
  "stage": "consulting|interested|meeting_ready|closed",
  "summary": "краткая внутренняя сводка: интерес, задача, сомнения, что уже объяснено"
}}
""".strip()


def _model_reply(
    *,
    owner_name: str,
    visitor_first_name: str,
    incoming_text: str,
    history: list[dict],
    first_contact: bool,
) -> dict:

    api_key = os.getenv(
        "OPENAI_API_KEY",
        "",
    ).strip()

    if not api_key:
        raise RuntimeError(
            "Missing OPENAI_API_KEY"
        )

    client = OpenAI(
        api_key=api_key
    )

    conversation = []

    for item in history[-16:]:
        conversation.append(
            {
                "role": item["role"],
                "content": item["content"],
            }
        )

    person = str(
        visitor_first_name or ""
    ).strip()

    if person:
        current = (
            f"Имя посетителя: {person}\n"
            f"Сообщение: {incoming_text}"
        )
    else:
        current = (
            f"Сообщение посетителя: "
            f"{incoming_text}"
        )

    conversation.append(
        {
            "role": "user",
            "content": current,
        }
    )

    response = client.responses.create(
        model=THEO_MODEL,
        instructions=_build_instructions(
            owner_name,
            first_contact,
        ),
        input=conversation,
        max_output_tokens=700,
    )

    return _parse_model_result(
        response.output_text
    )


def process_vk_community_message(
    *,
    vk_user_id: int,
    vk_peer_id: int,
    owner: dict,
    visitor_first_name: str,
    incoming_text: str,
    message_id: str = "",
) -> dict:

    vk_user_id = int(vk_user_id)
    vk_peer_id = int(vk_peer_id)

    incoming_text = str(
        incoming_text or ""
    ).strip()

    message_id = str(
        message_id or ""
    ).strip()

    if not incoming_text:
        raise RuntimeError(
            "Theo requires a non-empty incoming message."
        )

    owner = dict(
        owner or {}
    )

    owner_id = int(
        owner.get("telegram_id") or 0
    )

    if owner_id <= 0:
        raise RuntimeError(
            "Theo requires a valid Agency W owner."
        )

    owner_name = str(
        owner.get("first_name") or ""
    ).strip()

    owner_member_code = str(
        owner.get("member_code") or ""
    ).strip()

    existing = _load_dialog(
        vk_user_id
    )

    if existing and message_id:

        if (
            str(
                existing.get(
                    "last_vk_message_id"
                )
                or ""
            ).strip()
            == message_id
        ):

            return {
                "reply": str(
                    existing.get(
                        "last_reply_text"
                    )
                    or ""
                ).strip(),
                "stage": str(
                    existing.get("stage")
                    or "consulting"
                ),
                "summary": str(
                    existing.get("summary")
                    or ""
                ).strip(),
                "duplicate": True,
            }

    history = _clean_history(
        existing.get("dialogue_history")
        if existing
        else []
    )

    first_contact = not bool(history)

    result = _model_reply(
        owner_name=owner_name,
        visitor_first_name=visitor_first_name,
        incoming_text=incoming_text,
        history=history,
        first_contact=first_contact,
    )

    now = _now_iso()

    history.append(
        {
            "role": "user",
            "content": incoming_text[:2500],
            "at": now,
        }
    )

    history.append(
        {
            "role": "assistant",
            "content": result["reply"][:2500],
            "at": now,
        }
    )

    history = history[-24:]

    payload = {
        "vk_user_id": vk_user_id,
        "vk_peer_id": vk_peer_id,
        "owner_telegram_id": owner_id,
        "owner_member_code":
            owner_member_code or None,
        "owner_name":
            owner_name or None,
        "visitor_first_name":
            str(
                visitor_first_name or ""
            ).strip() or None,
        "stage": result["stage"],
        "dialogue_history": history,
        "summary":
            result["summary"] or None,
        "last_incoming_text":
            incoming_text[:3000],
        "last_reply_text":
            result["reply"][:3000],
        "last_vk_message_id":
            message_id or None,
        "last_seen_at": now,
        "updated_at": now,
    }

    if not existing:
        payload["first_seen_at"] = now

    _sb_post(payload)

    return {
        **result,
        "duplicate": False,
    }
