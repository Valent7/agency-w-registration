"""Build owner-reviewed marketing passports without external model calls."""
from copy import deepcopy


PHILOSOPHY = {
    "mission": "Мы возвращаем человеку время.",
    "principle": "Служить людям, создавать ценность, продавать и зарабатывать.",
    "goal": "Финансовое процветание через масштабирование пользы людям.",
}


def passport_draft(interview, legacy_profile, segments):
    answers = interview["answers"]

    def lookup(key):
        return answers.get(key) or None

    return {
        "schema": 1,
        "philosophy": PHILOSOPHY.copy(),
        "personality": {"values": lookup("personality-10")},
        "expertise": {
            "competencies": lookup("expertise-01"),
            "results_reported_by_owner": lookup("expertise-08"),
            "offer": lookup("audience-01"),
            "problem": lookup("audience-02"),
        },
        "audience": {
            "segments": deepcopy(segments),
            "legacy_profile": deepcopy(legacy_profile),
        },
        "strategy": {
            "professional_direction": lookup("expertise-06"),
            "public_evidence_to_review": lookup("audience-21"),
            "exclusions_to_review": lookup("audience-22"),
            "first_touch_offer": lookup("audience-25"),
        },
        "analytics": {},
    }


def search_segment(role, title, evidence, exclusions, *, enabled):
    if role not in {"client", "partner"}:
        raise ValueError("Неизвестный вид сегмента.")
    required = [line.strip() for line in evidence.splitlines() if line.strip()]
    if enabled and (not title.strip() or not required):
        raise ValueError("Для активного сегмента укажите название и публичные признаки соответствия.")
    return {
        "role": role,
        "title": title.strip(),
        "enabled": enabled,
        "required_public_evidence": required,
        "exclusions": [line.strip() for line in exclusions.splitlines() if line.strip()],
    }


class PassportStore:
    def __init__(self, interview_store):
        self.scope = interview_store

    def history(self):
        return self.scope.request("GET", "marketing_passport_versions", params={
            **self.scope.filters,
            "select": "version,restored_from,created_at",
            "order": "version.desc",
            "limit": 100,
        })

    def load(self, version):
        rows = self.scope.request("GET", "marketing_passport_versions", params={
            **self.scope.filters, "version": f"eq.{int(version)}", "select": "encrypted_passport",
        })
        if len(rows) != 1:
            raise LookupError("Версия паспорта недоступна.")
        passport = self.scope.decrypt(rows[0]["encrypted_passport"])
        if passport.get("schema") != 1:
            raise ValueError("Не удалось прочитать паспорт.")
        return passport

    def approve(self, expected, passport, *, restored_from=None):
        # Two concurrent approvals share the same next key. The database accepts one.
        rows = self.scope.request("POST", "marketing_passport_versions", json={
            **self.scope.identity,
            "version": expected + 1,
            "restored_from": restored_from,
            "encrypted_passport": self.scope.encrypt(passport),
        })
        if len(rows) != 1:
            raise RuntimeError("Сохранение утверждённой версии не подтверждено.")


def scout_audience(passport, version):
    """Give the scout only criteria the owner explicitly enabled for search."""
    segments = [
        {key: segment[key] for key in ("role", "title", "required_public_evidence", "exclusions")}
        for segment in passport["audience"]["segments"] if segment["enabled"]
    ]
    if not segments:
        return None
    return {
        "marketing_passport_version": version,
        "segments": segments,
        "instructions": (
            "Учитывай только подтверждённые публичные признаки. Каждый кандидат должен "
            "соответствовать всем обязательным признакам одного сегмента и пройти исключения. "
            "Соблюдай действующие правила VK и приоритет сетевых предпринимателей."
        ),
    }


def current_passport(read, decrypt, owner_id, project_id):
    rows = read("marketing_passport_versions", {
        "owner_id": f"eq.{int(owner_id)}",
        "project_id": f"eq.{project_id}",
        "order": "version.desc",
        "select": "version,encrypted_passport",
        "limit": 1,
    })
    if not rows:
        return None
    row = rows[0]
    passport = decrypt(row["encrypted_passport"])
    if passport.get("schema") != 1:
        raise ValueError("Не удалось прочитать утверждённый паспорт.")
    return row["version"], passport


def matched_segment(segments, analysis, public_profile):
    """Require one exact, cited profile excerpt for every mandatory criterion."""
    index = analysis.get("marketing_segment_index")
    if type(index) is not int or not 0 <= index < len(segments):
        return None
    segment = segments[index]
    evidence = analysis.get("marketing_evidence")
    if not isinstance(evidence, list) or len(evidence) != len(segment["required_public_evidence"]):
        return None

    def text_fields(value):
        if isinstance(value, str):
            return [value.casefold()]
        if isinstance(value, dict):
            value = value.values()
        elif not isinstance(value, list):
            return []
        return [field for item in value for field in text_fields(item)]

    fields = text_fields(public_profile)
    for quote in evidence:
        if (not isinstance(quote, str) or len(quote.strip()) < 5
                or not any(quote.strip().casefold() in field for field in fields)):
            return None
    for excluded in segment["exclusions"]:
        if any(excluded.casefold() in field for field in fields):
            return None
    return segment
