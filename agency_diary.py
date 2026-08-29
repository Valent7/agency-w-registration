from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape as xml_escape
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

import requests
import streamlit as st


UTC = ZoneInfo("UTC")
BERLIN = ZoneInfo("Europe/Berlin")

# 53 пары: отдельная короткая латинская фраза утром и вечером.
WEEKLY_QUOTES = (
    ('Veni, vidi, vici.', 'Пришёл, увидел, победил.', 'Memento mori.', 'Помни о смерти.'),
    ('Carpe diem.', 'Лови день.', 'Mea culpa.', 'Моя вина.'),
    ('Per aspera ad astra.', 'Через тернии к звёздам.', 'Nosce te ipsum.', 'Познай самого себя.'),
    ('Dum spiro, spero.', 'Пока дышу — надеюсь.', 'Esse quam videri.', 'Быть, а не казаться.'),
    ('Festina lente.', 'Спеши медленно.', 'Audi alteram partem.', 'Выслушай другую сторону.'),
    ('Sapere aude.', 'Осмелься быть мудрым.', 'Suum cuique.', 'Каждому — своё.'),
    ('Age quod agis.', 'Делай то, что делаешь.', 'Fiat iustitia.', 'Да свершится справедливость.'),
    ('Labor omnia vincit.', 'Труд всё побеждает.', 'Noli nocere.', 'Не навреди.'),
    ('Facta, non verba.', 'Дела, не слова.', 'Ne quid nimis.', 'Ничего сверх меры.'),
    ('Ad meliora.', 'К лучшему.', 'Sine ira et studio.', 'Без гнева и пристрастия.'),
    ('Ad maiora.', 'К большему.', 'Pacta sunt servanda.', 'Договорённости нужно соблюдать.'),
    ('Ad astra.', 'К звёздам.', 'Veritas vincit.', 'Истина побеждает.'),
    ('Nulla dies sine linea.', 'Ни дня без строки.', 'Conscientia mille testes.', 'Совесть — тысяча свидетелей.'),
    ('Ora et labora.', 'Молись и трудись.', 'Honeste vivere.', 'Жить честно.'),
    ('Nunc aut numquam.', 'Сейчас или никогда.', 'Alterum non laedere.', 'Не вредить другому.'),
    ('Audentes fortuna iuvat.', 'Судьба помогает смелым.', 'In medio stat virtus.', 'Добродетель — в мере.'),
    ('Fortes fortuna adiuvat.', 'Фортуна помогает храбрым.', 'Errare humanum est.', 'Ошибаться свойственно человеку.'),
    ('Vincit qui se vincit.', 'Побеждает тот, кто побеждает себя.', 'Respice finem.', 'Помни о последствиях.'),
    ('Memento vivere.', 'Помни, что надо жить.', 'Est modus in rebus.', 'Во всём есть мера.'),
    ('Fiat lux.', 'Да будет свет.', 'Bona fide.', 'Добросовестно.'),
    ('Ad lucem.', 'К свету.', 'Veritas ante omnia.', 'Истина прежде всего.'),
    ('Incipe.', 'Начни.', 'Lux et veritas.', 'Свет и истина.'),
    ('Sursum corda.', 'Возвысьте сердца.', 'Dura lex, sed lex.', 'Закон суров, но это закон.'),
    ('Plus ultra.', 'Дальше предела.', 'In dubio pro reo.', 'При сомнении — в пользу обвиняемого.'),
    ('Excelsior.', 'Всё выше.', 'Cui bono?', 'Кому выгодно?'),
    ('Ad victoriam.', 'К победе.', 'Res ipsa loquitur.', 'Дело говорит само за себя.'),
    ('Semper ad meliora.', 'Всегда к лучшему.', 'O tempora! O mores!', 'О времена! О нравы!'),
    ('Crescit eundo.', 'Растёт в движении.', 'Homo sum.', 'Я человек.'),
    ('Vires acquirit eundo.', 'Силы растут в движении.', 'Homo homini lupus.', 'Человек человеку волк.'),
    ('Sic itur ad astra.', 'Так идут к звёздам.', 'In vino veritas.', 'Истина в вине.'),
    ('Finis coronat opus.', 'Конец венчает дело.', 'Verba volant, scripta manent.', 'Слова улетают, написанное остаётся.'),
    ('Omnia vincit amor.', 'Любовь побеждает всё.', 'Ars longa, vita brevis.', 'Искусство вечно, жизнь коротка.'),
    ('Docendo discimus.', 'Уча других, учимся сами.', 'Tempus fugit.', 'Время летит.'),
    ('Usus magister est optimus.', 'Опыт — лучший учитель.', 'Vanitas vanitatum.', 'Суета сует.'),
    ('Non ducor, duco.', 'Я не ведомый — я веду.', 'Quid pro quo.', 'Одно за другое.'),
    ('Alis volat propriis.', 'Летит на собственных крыльях.', 'Sine qua non.', 'Непременное условие.'),
    ('Mens agitat molem.', 'Дух движет материю.', 'Pro et contra.', 'За и против.'),
    ('Excelsa petimus.', 'Стремимся к высшему.', 'De facto.', 'На деле.'),
    ('Ad infinitum.', 'До бесконечности.', 'De jure.', 'По праву.'),
    ('Pro bono.', 'Ради добра.', 'Sub rosa.', 'Втайне.'),
    ('Ad rem.', 'К делу.', 'Persona non grata.', 'Нежелательная персона.'),
    ('Sine metu.', 'Без страха.', 'Vox populi.', 'Глас народа.'),
    ('Vive hodie.', 'Живи сегодня.', 'Acta est fabula.', 'Представление окончено.'),
    ('Ad vitam.', 'К жизни.', 'Sic transit gloria mundi.', 'Так проходит мирская слава.'),
    ('Audere est facere.', 'Осмелиться — значит сделать.', 'Pax vobiscum.', 'Мир вам.'),
    ('Qui audet, vincit.', 'Кто смеет, тот побеждает.', 'Fiat veritas.', 'Да будет истина.'),
    ('Ad finem.', 'До конца.', 'Semper fidelis.', 'Всегда верен.'),
    ('Bis dat qui cito dat.', 'Кто даёт быстро, даёт вдвойне.', 'Tabula rasa.', 'Чистая доска.'),
    ('Concordia res parvae crescunt.', 'В согласии малое растёт.', 'Status quo.', 'Существующее положение.'),
    ('Lux in tenebris.', 'Свет во тьме.', 'Alter ego.', 'Другое я.'),
    ('Ad vitam meliorem.', 'К лучшей жизни.', 'Modus vivendi.', 'Образ жизни.'),
    ('Faber est suae quisque fortunae.', 'Каждый кузнец своей судьбы.', 'Mens sana in corpore sano.', 'В здоровом теле — здоровый дух.'),
    ('Alea iacta est.', 'Жребий брошен.', 'Amor fati.', 'Люби свою судьбу.'),
)


class DiaryStorageError(RuntimeError):
    pass


def _config() -> tuple[str, str]:
    url = str(st.secrets.get("SUPABASE_URL") or "").rstrip("/")
    key = str(st.secrets.get("SUPABASE_SECRET_KEY") or "")
    if not url or not key:
        raise DiaryStorageError("Не найдены SUPABASE_URL или SUPABASE_SECRET_KEY.")
    return url, key


def _headers(prefer: str | None = None) -> dict[str, str]:
    _, key = _config()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _weekly_quote(day: date, reflection_type: str) -> tuple[str, str, str]:
    iso_year, iso_week, _ = day.isocalendar()
    index = min(max(int(iso_week), 1), 53) - 1
    morning_latin, morning_ru, evening_latin, evening_ru = WEEKLY_QUOTES[index]
    latin, ru = (
        (morning_latin, morning_ru)
        if reflection_type == "morning"
        else (evening_latin, evening_ru)
    )
    return f"{iso_year}-W{iso_week:02d}", latin, ru


def _load_entry(owner_telegram_id: int, entry_date: date, reflection_type: str) -> dict[str, Any] | None:
    url, _ = _config()
    response = requests.get(
        f"{url}/rest/v1/agency_diary_entries",
        headers=_headers(),
        params={
            "owner_telegram_id": f"eq.{int(owner_telegram_id)}",
            "entry_date": f"eq.{entry_date.isoformat()}",
            "reflection_type": f"eq.{reflection_type}",
            "select": "*",
            "limit": 1,
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise DiaryStorageError("Не удалось загрузить запись дневника: " + response.text[:500])
    rows = response.json()
    return rows[0] if isinstance(rows, list) and rows else None

def _load_all_entries(owner_telegram_id: int) -> list[dict[str, Any]]:
    """Загрузить всю историю дневника владельца."""
    url, _ = _config()
    result: list[dict[str, Any]] = []
    page_size = 1000
    offset = 0

    while True:
        response = requests.get(
            f"{url}/rest/v1/agency_diary_entries",
            headers=_headers(),
            params={
                "owner_telegram_id": f"eq.{int(owner_telegram_id)}",
                "select": "*",
                "order": "entry_date.asc,created_at.asc",
                "limit": page_size,
                "offset": offset,
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise DiaryStorageError(
                "Не удалось загрузить историю дневника: "
                + response.text[:500]
            )

        rows = response.json()
        if not isinstance(rows, list):
            raise DiaryStorageError(
                "Supabase вернул неожиданный формат истории дневника."
            )

        result.extend(row for row in rows if isinstance(row, dict))
        if len(rows) < page_size:
            break
        offset += page_size

    reflection_order = {"morning": 0, "evening": 1}
    result.sort(
        key=lambda item: (
            str(item.get("entry_date") or ""),
            reflection_order.get(
                str(item.get("reflection_type") or ""),
                9,
            ),
        )
    )
    return result


_RU_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
_RU_MONTHS_TITLE = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)


def _parse_entry_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except Exception:
        return None


def _format_date_ru(day: date) -> str:
    return f"{day.day} {_RU_MONTHS_GENITIVE[day.month - 1]} {day.year}"


def _month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _month_label(key: str) -> str:
    try:
        year, month = (int(part) for part in key.split("-", 1))
        return f"{_RU_MONTHS_TITLE[month - 1]} {year}"
    except Exception:
        return key


def _entry_quote(entry: dict[str, Any]) -> tuple[str, str]:
    latin = str(entry.get("quote_latin") or "").strip()
    translation = str(entry.get("quote_translation") or "").strip()
    if latin or translation:
        return latin, translation

    day = _parse_entry_date(entry.get("entry_date"))
    reflection_type = str(entry.get("reflection_type") or "morning")
    if day is None:
        return "", ""

    _, latin, translation = _weekly_quote(day, reflection_type)
    return latin, translation


def _docx_run(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    size: int = 22,
) -> str:
    props: list[str] = []
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    props.append(f'<w:sz w:val="{int(size)}"/>')
    props.append(f'<w:szCs w:val="{int(size)}"/>')

    return (
        "<w:r><w:rPr>"
        + "".join(props)
        + "</w:rPr>"
        + f'<w:t xml:space="preserve">{xml_escape(str(text))}</w:t>'
        + "</w:r>"
    )


def _docx_paragraph(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    size: int = 22,
    align: str | None = None,
    before: int = 0,
    after: int = 120,
) -> str:
    ppr = [
        f'<w:spacing w:before="{int(before)}" '
        f'w:after="{int(after)}"/>'
    ]
    if align:
        ppr.append(f'<w:jc w:val="{align}"/>')

    return (
        "<w:p><w:pPr>"
        + "".join(ppr)
        + "</w:pPr>"
        + _docx_run(
            text,
            bold=bold,
            italic=italic,
            size=size,
        )
        + "</w:p>"
    )


def _docx_page_break() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def _build_diary_docx(entries: list[dict[str, Any]]) -> bytes:
    """Собрать настоящий .docx без дополнительной библиотеки."""
    clean_entries = [
        item
        for item in entries
        if str(item.get("entry_text") or "").strip()
        and _parse_entry_date(item.get("entry_date")) is not None
    ]
    clean_entries.sort(
        key=lambda item: (
            str(item.get("entry_date") or ""),
            0
            if str(item.get("reflection_type") or "") == "morning"
            else 1,
        )
    )

    body: list[str] = [
        _docx_paragraph(
            "Мой дневник",
            bold=True,
            size=38,
            align="center",
            after=180,
        ),
        _docx_paragraph(
            "Агентство W",
            italic=True,
            size=22,
            align="center",
            after=360,
        ),
    ]

    current_month = ""
    current_day = ""

    for entry in clean_entries:
        day = _parse_entry_date(entry.get("entry_date"))
        if day is None:
            continue

        month = _month_key(day)
        if month != current_month:
            if current_month:
                body.append(_docx_page_break())
            body.append(
                _docx_paragraph(
                    _month_label(month),
                    bold=True,
                    size=30,
                    before=80,
                    after=260,
                )
            )
            current_month = month
            current_day = ""

        day_key = day.isoformat()
        if day_key != current_day:
            body.append(
                _docx_paragraph(
                    _format_date_ru(day),
                    bold=True,
                    size=26,
                    before=120,
                    after=180,
                )
            )
            current_day = day_key

        reflection_type = str(entry.get("reflection_type") or "")
        section_title = (
            "Утренние размышления"
            if reflection_type == "morning"
            else "Вечерние размышления"
        )
        body.append(
            _docx_paragraph(
                section_title,
                bold=True,
                size=23,
                before=100,
                after=100,
            )
        )

        latin, translation = _entry_quote(entry)
        if latin:
            body.append(
                _docx_paragraph(
                    latin,
                    italic=True,
                    size=21,
                    after=40,
                )
            )
        if translation:
            body.append(
                _docx_paragraph(
                    translation,
                    italic=True,
                    size=19,
                    after=140,
                )
            )

        entry_text = str(entry.get("entry_text") or "").strip()
        paragraphs = [
            part.strip()
            for part in entry_text.split("\n\n")
            if part.strip()
        ]
        if not paragraphs:
            paragraphs = [entry_text]

        for paragraph in paragraphs:
            body.append(
                _docx_paragraph(
                    paragraph,
                    size=22,
                    after=160,
                )
            )

    body.append(
        '<w:sectPr>'
        '<w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" '
        'w:bottom="1440" w:left="1440" '
        'w:header="720" w:footer="720" w:gutter="0"/>'
        '</w:sectPr>'
    )

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document '
        'xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships">'
        '<w:body>'
        + "".join(body)
        + '</w:body></w:document>'
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/'
        '2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.'
        'relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.document.main+xml"/>'
        '</Types>'
    )

    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
        '2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        '</Relationships>'
    )

    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document_xml)

    return buffer.getvalue()


def _render_book(owner_telegram_id: int) -> None:
    st.markdown("### 📖 Моя книга")
    st.caption(
        "Все сохранённые записи собраны здесь по датам. "
        "Утренние и вечерние размышления остаются отдельными разделами."
    )

    try:
        entries = _load_all_entries(int(owner_telegram_id))
    except DiaryStorageError as exc:
        st.error(str(exc))
        return

    entries = [
        item
        for item in entries
        if str(item.get("entry_text") or "").strip()
    ]
    if not entries:
        st.info("В книге пока нет сохранённых записей.")
        return

    month_keys = sorted(
        {
            _month_key(day)
            for item in entries
            if (
                day := _parse_entry_date(item.get("entry_date"))
            ) is not None
        },
        reverse=True,
    )

    selected_period = st.selectbox(
        "Период",
        ["all", *month_keys],
        format_func=lambda value: (
            "Все записи"
            if value == "all"
            else _month_label(value)
        ),
        key=f"diary_book_period_{owner_telegram_id}",
    )

    try:
        docx_bytes = _build_diary_docx(entries)
        st.download_button(
            "⬇️ Скачать всю книгу в Word (.docx)",
            data=docx_bytes,
            file_name="Мой_дневник_Agency_W.docx",
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            width="stretch",
            key=f"diary_download_docx_{owner_telegram_id}",
        )
    except Exception as exc:
        st.error(f"Не удалось подготовить Word-файл: {exc}")

    visible_entries: list[dict[str, Any]] = []
    for item in entries:
        day = _parse_entry_date(item.get("entry_date"))
        if day is None:
            continue
        if (
            selected_period != "all"
            and _month_key(day) != selected_period
        ):
            continue
        visible_entries.append(item)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in visible_entries:
        key = str(item.get("entry_date") or "")[:10]
        grouped.setdefault(key, []).append(item)

    for day_key in sorted(grouped.keys(), reverse=True):
        day = _parse_entry_date(day_key)
        if day is None:
            continue

        st.markdown(f"### {_format_date_ru(day)}")
        day_entries = sorted(
            grouped[day_key],
            key=lambda item: (
                0
                if str(item.get("reflection_type") or "") == "morning"
                else 1
            ),
        )

        for item in day_entries:
            is_morning = (
                str(item.get("reflection_type") or "") == "morning"
            )
            st.markdown(
                "#### ☀️ Утренние размышления"
                if is_morning
                else "#### 🌙 Вечерние размышления"
            )

            latin, translation = _entry_quote(item)
            if latin or translation:
                _quote_block(latin, translation)

            st.write(str(item.get("entry_text") or "").strip())

        st.divider()




def _save_entry(
    owner_telegram_id: int,
    entry_date: date,
    reflection_type: str,
    voice_transcript: str,
    entry_text: str,
) -> None:
    url, _ = _config()
    quote_week, quote_latin, quote_ru = _weekly_quote(entry_date, reflection_type)
    payload = {
        "owner_telegram_id": int(owner_telegram_id),
        "entry_date": entry_date.isoformat(),
        "reflection_type": reflection_type,
        "voice_transcript": str(voice_transcript or "").strip() or None,
        "entry_text": str(entry_text or "").strip(),
        "quote_week": quote_week,
        "quote_latin": quote_latin,
        "quote_translation": quote_ru,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    response = requests.post(
        f"{url}/rest/v1/agency_diary_entries?on_conflict=owner_telegram_id,entry_date,reflection_type",
        headers=_headers("resolution=merge-duplicates,return=minimal"),
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        raise DiaryStorageError("Не удалось сохранить запись дневника: " + response.text[:500])


def _transcribe_audio(audio_file: Any) -> str:
    api_key = str(st.secrets.get("OPENAI_API_KEY") or "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не найден в Streamlit Secrets.")

    audio_bytes = audio_file.getvalue()
    if not audio_bytes:
        raise RuntimeError("Аудиозапись пустая.")

    response = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        files={
            "file": (
                str(getattr(audio_file, "name", "") or "reflection.wav"),
                audio_bytes,
                str(getattr(audio_file, "type", "") or "audio/wav"),
            )
        },
        data={
            "model": "gpt-4o-mini-transcribe",
            "language": "ru",
            "response_format": "json",
        },
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError("Не удалось распознать голос: " + response.text[:500])

    transcript = str(response.json().get("text") or "").strip()
    if not transcript:
        raise RuntimeError("Распознавание вернуло пустой текст.")
    return transcript


def _quote_block(latin: str, translation: str) -> None:
    st.markdown(
        f"""
        <div style="margin:0.35rem 0 1.25rem 0;">
          <div style="font-size:1.35rem;font-weight:700;line-height:1.4;">{latin}</div>
          <div style="font-size:0.95rem;font-weight:400;opacity:0.74;line-height:1.4;margin-top:0.22rem;">{translation}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_reflection_page(owner_telegram_id: int, reflection_type: str) -> None:
    is_morning = reflection_type == "morning"
    st.markdown("### ☀️ Утренние размышления" if is_morning else "### 🌙 Вечерние размышления")

    selected_date = st.date_input(
        "Дата записи",
        value=datetime.now(BERLIN).date(),
        max_value=datetime.now(BERLIN).date(),
        key=f"diary_date_{reflection_type}_{owner_telegram_id}",
    )

    _, quote_latin, quote_ru = _weekly_quote(selected_date, reflection_type)
    _quote_block(quote_latin, quote_ru)

    entry = _load_entry(int(owner_telegram_id), selected_date, reflection_type)
    suffix = f"{owner_telegram_id}_{reflection_type}_{selected_date}"
    voice_key = f"diary_voice_{suffix}"
    text_key = f"diary_text_{suffix}"
    transcript_key = f"diary_transcript_{suffix}"
    loaded_key = f"diary_loaded_{suffix}"

    if not st.session_state.get(loaded_key):
        st.session_state[text_key] = str((entry or {}).get("entry_text") or "")
        st.session_state[transcript_key] = str((entry or {}).get("voice_transcript") or "")
        st.session_state[loaded_key] = True

    st.markdown("#### 🎙 Сказать вслух")
    audio = st.audio_input("Запишите размышления голосом", key=voice_key)

    if audio is not None and st.button(
        "📝 Расшифровать голос",
        type="primary",
        key=f"diary_transcribe_{suffix}",
    ):
        with st.spinner("Расшифровываю..."):
            try:
                transcript = _transcribe_audio(audio)
                previous = str(st.session_state.get(text_key) or "").strip()
                st.session_state[transcript_key] = transcript
                st.session_state[text_key] = (
                    f"{previous}\n\n{transcript}".strip()
                    if previous
                    else transcript
                )
                st.success("Готово. Текст можно исправить или дополнить.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    st.markdown("#### ✍️ Мои размышления")
    st.text_area(
        "Текст записи",
        key=text_key,
        height=260,
        placeholder="Говорите в микрофон или пишите здесь. Текст можно свободно редактировать.",
        label_visibility="collapsed",
    )

    if st.button("💾 Сохранить", type="primary", key=f"diary_save_{suffix}"):
        try:
            _save_entry(
                int(owner_telegram_id),
                selected_date,
                reflection_type,
                str(st.session_state.get(transcript_key) or ""),
                str(st.session_state.get(text_key) or ""),
            )
            st.success("Запись сохранена.")
        except DiaryStorageError as exc:
            st.error(str(exc))

    if entry and str((entry or {}).get("entry_text") or "").strip():
        st.caption("Эта запись уже была сохранена. Её можно редактировать.")


def render_agency_diary(owner_telegram_id: int) -> None:
    st.markdown("## 📖 Дневник")
    st.caption(
        "Записывайте мысли утром и вечером, а в «Моей книге» "
        "читайте всю историю подряд. "
        "После распознавания в дневнике остаётся только текст."
    )

    if "agency_diary_mode" not in st.session_state:
        st.session_state["agency_diary_mode"] = "morning"

    morning_col, evening_col, book_col = st.columns(3)

    with morning_col:
        if st.button(
            "☀️ Утренние размышления",
            type=(
                "primary"
                if st.session_state["agency_diary_mode"] == "morning"
                else "secondary"
            ),
            width="stretch",
            key="open_diary_morning",
        ):
            st.session_state["agency_diary_mode"] = "morning"
            st.rerun()

    with evening_col:
        if st.button(
            "🌙 Вечерние размышления",
            type=(
                "primary"
                if st.session_state["agency_diary_mode"] == "evening"
                else "secondary"
            ),
            width="stretch",
            key="open_diary_evening",
        ):
            st.session_state["agency_diary_mode"] = "evening"
            st.rerun()

    with book_col:
        if st.button(
            "📖 Моя книга",
            type=(
                "primary"
                if st.session_state["agency_diary_mode"] == "book"
                else "secondary"
            ),
            width="stretch",
            key="open_diary_book",
        ):
            st.session_state["agency_diary_mode"] = "book"
            st.rerun()

    st.divider()

    mode = str(st.session_state["agency_diary_mode"])
    if mode == "book":
        _render_book(int(owner_telegram_id))
    else:
        _render_reflection_page(
            int(owner_telegram_id),
            mode,
        )
