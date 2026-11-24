"""Cabinet entry point for the single-project text interview pilot."""
from datetime import date

import requests
import streamlit as st

from marketing_interview_voice import transcribe_locally, voice_engine
from marketing_results import AMOUNTS, COUNTS, commercial_proposals, commercial_results
from marketing_passport import PHILOSOPHY, PassportStore, passport_draft, search_segment
from marketing_interview import BLOCKS, QUESTION_IDS, InterviewStore, change_answer, pilot_project
from workspace_persistence import (
    _decrypt_state,
    _encrypt_state,
    _require_runtime_secret,
    _runtime_secret,
)


def _request(method, table, **kwargs):
    secret = _require_runtime_secret("SUPABASE_SECRET_KEY")
    url = _require_runtime_secret("SUPABASE_URL").rstrip("/")
    response = requests.request(
        method, f"{url}/rest/v1/{table}", timeout=15,
        headers={"apikey": secret, "Authorization": f"Bearer {secret}",
                 "Prefer": "return=representation"},
        **kwargs,
    )
    response.raise_for_status()
    return response.json()


def render_marketing_interview(owner_id, legacy_passport=None):
    project = pilot_project(
        owner_id, _runtime_secret("AGENCY_MARKETING_PILOT_OWNER"),
        _runtime_secret("AGENCY_MARKETING_PILOT_PROJECT"),
    )
    if project is None:
        return
    store = InterviewStore(_request, owner_id, project, _encrypt_state, _decrypt_state)
    prefix = f"marketing_interview_{int(owner_id)}_{project}"
    with st.expander("Маркетинговое ядро · Интеллектуальное интервью"):
        try:
            revision, state = store.load()
        except (requests.RequestException, LookupError, ValueError, RuntimeError):
            st.error("Интервью сейчас недоступно. Попробуйте открыть его позже.")
            return

        answers = state["answers"]
        confirmed = state["confirmed"]
        st.caption("Ответы видны вам. Пропускайте вопросы, на которые не хотите отвечать.")
        progress = len(answers) / len(QUESTION_IDS)
        st.progress(progress, text=f"Пройдено {progress:.0%} · {len(answers)} из {len(QUESTION_IDS)}")
        current = next((block for block in BLOCKS if block["id"] not in confirmed), None)
        next_question = next(
            (q for q in current["questions"] if q["id"] not in answers), None
        ) if current else None

        edits = {"": "Продолжить интервью"}
        for block in BLOCKS:
            edits.update({q["id"]: q["text"] for q in block["questions"] if q["id"] in answers})
        selected = st.selectbox("Выбрать ранее пройденный вопрос", list(edits),
                                format_func=edits.get, key=prefix + "_select")
        question = {"id": selected, "text": edits[selected]} if selected else next_question
        updated = None
        if question:
            identifier = question["id"]
            form_key = f"{prefix}_{revision}_{identifier}"
            voice_key = form_key + "_transcript"
            engine = voice_engine()
            if engine:
                audio = st.audio_input("Ответить голосом", sample_rate=16000, key=form_key + "_audio")
                if audio and st.button("Распознать запись", key=form_key + "_decode"):
                    try:
                        st.session_state[voice_key] = transcribe_locally(audio.getvalue(), engine)
                    except ValueError as error:
                        st.warning(str(error))
                    else:
                        st.session_state.pop(form_key + "_answer", None)
            with st.form(form_key):
                response = st.text_area(
                    question["text"], value=st.session_state.get(voice_key, answers.get(identifier) or ""),
                    max_chars=2000, key=form_key + "_answer",
                )
                save = st.form_submit_button("Сохранить")
                skip = st.form_submit_button("Пропустить вопрос")
            if skip or save and response.strip():
                updated = change_answer(state, identifier, None if skip else response)
            elif save:
                st.warning("Введите ответ или пропустите вопрос.")
        elif current:
            st.subheader(current["title"])
            for item in current["questions"]:
                response = answers[item["id"]]
                if response:
                    st.text(item["text"])
                    st.text(response)
            if st.button("Подтвердить ответы блока", key=f"{prefix}_confirm_{revision}"):
                updated = {**state, "confirmed": [*confirmed, current["id"]]}
        else:
            st.success("Все блоки интервью подтверждены.")

        if updated is not None:
            try:
                store.save(revision, updated)
            except (requests.RequestException, RuntimeError, ValueError):
                st.error("Сохранение не подтверждено. Обновите страницу, чтобы проверить ответы.")
            else:
                if question is not None:
                    st.session_state.pop(voice_key, None)
                st.rerun()

    if next_question is None and current is None:
        _render_passport(store, prefix, revision, state, legacy_passport)
    _render_results(store, prefix, revision, state)
    _render_feedback(store, prefix, revision, state)


def _render_passport(interview, prefix, revision, state, legacy_passport):
    versions = PassportStore(interview)
    with st.expander("Маркетинговый паспорт"):
        try:
            history = versions.history()
            latest = history[0]["version"] if history else 0
            active = versions.load(latest) if latest else None
            existing = {item["role"]: item for item in active["audience"]["segments"]} if active else {}
            with st.form(f"{prefix}_passport_{latest}_{revision}"):
                segments = []
                answers = state["answers"]
                for role, title, answer_key in (
                    ("client", "Клиенты", "audience-04"),
                    ("partner", "Партнёры", "audience-05"),
                ):
                    st.subheader(title)
                    saved = existing.get(role, {})
                    enabled = st.checkbox(f"Вести поиск · {title}", value=saved.get("enabled", False))
                    if answers.get(answer_key):
                        st.text(answers[answer_key])
                    name = st.text_input(f"Название сегмента · {title}", value=saved.get("title", ""), max_chars=200)
                    evidence = st.text_area(
                        f"Обязательные публичные признаки · {title}",
                        value="\n".join(saved["required_public_evidence"]) if saved else answers.get("audience-21") or "",
                        max_chars=2000,
                    )
                    exclusions = st.text_area(
                        f"Исключения · {title}",
                        value="\n".join(saved["exclusions"]) if saved else answers.get("audience-22") or "",
                        max_chars=2000,
                    )
                    segments.append((role, name, evidence, exclusions, enabled))
                use_legacy = bool(legacy_passport) and st.checkbox("Включить прежний портрет ЦА в этот проект")
                st.caption("Каждый обязательный признак и исключение запишите с новой строки.")
                _display_passport(passport_draft(state, None, []))
                approve = st.form_submit_button("Утвердить маркетинговый паспорт")
            if approve:
                allowed = [
                    search_segment(role, name, evidence, exclusions, enabled=enabled)
                    for role, name, evidence, exclusions, enabled in segments
                ]
                versions.approve(latest, passport_draft(
                    state, legacy_passport if use_legacy else None, allowed,
                ))
                st.rerun()
            if history:
                _render_passport_history(versions, prefix, history)
        except ValueError as error:
            st.warning(str(error))
            return
        except (requests.RequestException, LookupError, RuntimeError):
            st.error("Операция с паспортом не подтверждена. Обновите страницу и проверьте историю.")
            return
    if latest:
        from marketing_journal_ui import render_journal

        render_journal(interview, prefix, revision, state, latest, active)


def _render_passport_history(versions, prefix, history):
    numbers = [entry["version"] for entry in history]
    latest = numbers[0]
    history_key = f"{prefix}_history_{latest}"
    index = st.session_state.get(history_key, 0)
    number = numbers[index]
    st.subheader("История утверждённых версий")
    st.caption(f"Версия {number} из {latest}")
    newer, older = st.columns(2)
    if newer.button("Следующая версия", disabled=index == 0, key=history_key + "_next"):
        st.session_state[history_key] = index - 1
        st.rerun()
    if older.button("Предыдущая версия", disabled=index == len(numbers) - 1, key=history_key + "_prev"):
        st.session_state[history_key] = index + 1
        st.rerun()
    previous = versions.load(number)
    _display_passport(previous)
    if number != latest and st.button("Восстановить выбранную версию",
                                      key=f"{prefix}_restore_{number}_{latest}"):
        versions.approve(latest, previous, restored_from=number)
        st.rerun()


def _render_results(store, prefix, revision, state):
    with st.expander("Коммерческие результаты"):
        st.caption("Укажите подтверждённые итоги периода. Суммы вводятся в евро.")
        reports = state.get("commercial_reports", {})
        start = st.date_input("Начало периода", date.today().replace(day=1), key=prefix + "_start")
        end = st.date_input("Конец периода", date.today(), key=prefix + "_end")
        period = f"{start.isoformat()}/{end.isoformat()}"
        report = reports.get(period, {})
        with st.form(f"{prefix}_results_{period}_{revision}"):
            counts = {
                key: st.number_input(label, min_value=0, value=report.get("counts", {}).get(key),
                                     step=1, key=f"{prefix}_{period}_{revision}_{key}")
                for key, label in COUNTS.items()
            }
            amounts = {
                key: st.number_input(label, min_value=0.0, value=report.get("amounts", {}).get(key),
                                     step=0.01, key=f"{prefix}_{period}_{revision}_{key}")
                for key, label in AMOUNTS.items()
            }
            submit = st.form_submit_button("Сохранить итоги")
        if submit:
            try:
                if end < start:
                    raise ValueError("Конец периода должен быть позже начала или совпадать с ним.")
                commercial_results(counts, amounts)
                store.save(revision, {**state, "commercial_reports": {
                    **reports, period: {"counts": counts, "amounts": amounts},
                }})
            except ValueError as error:
                st.warning(str(error))
            except (requests.RequestException, RuntimeError):
                st.error("Сохранение итогов не подтверждено. Обновите страницу.")
            else:
                st.rerun()
        if report:
            results = commercial_results(report["counts"], report["amounts"])
            for key, label in (
                ("profit", "Прибыль, €"),
                ("cost_per_customer", "Стоимость привлечения клиента, €"),
                ("marketing_roi_pct", "Окупаемость маркетинга, %"),
                ("content_roi_pct", "Окупаемость контента, %"),
            ):
                value = results[key]
                st.metric(label, "Нет базы для расчёта" if value is None else str(value))


def pilot_interview_store(owner_id, project_id):
    configured = pilot_project(
        owner_id, _runtime_secret("AGENCY_MARKETING_PILOT_OWNER"),
        _runtime_secret("AGENCY_MARKETING_PILOT_PROJECT"),
    )
    if configured != project_id or configured is None:
        return None
    return InterviewStore(_request, owner_id, configured, _encrypt_state, _decrypt_state)


def read_approved_passport(owner_id, project_id):
    configured = pilot_project(
        owner_id, _runtime_secret("AGENCY_MARKETING_PILOT_OWNER"),
        _runtime_secret("AGENCY_MARKETING_PILOT_PROJECT"),
    )
    if configured is None or configured != project_id:
        return None
    from marketing_passport import current_passport

    return current_passport(
        lambda table, params: _request("GET", table, params=params), _decrypt_state, owner_id, project_id,
    )


def _display_passport(passport):
    for key, label in (("mission", "Миссия"), ("principle", "Принцип"), ("goal", "Цель")):
        st.text(f"{label} · {PHILOSOPHY[key]}")
    for values, headings in (
        (passport["personality"], {"values": "Ценности"}),
        (passport["expertise"], {
            "competencies": "Компетенции", "results_reported_by_owner": "Результаты со слов владельца",
            "offer": "Предложение", "problem": "Какую проблему решает предложение",
        }),
        (passport["strategy"], {
            "professional_direction": "Профессиональное направление",
            "first_touch_offer": "Предложение для первого знакомства",
        }),
    ):
        for key, label in headings.items():
            st.text(f"{label} · {values.get(key) or 'Сведения пока отсутствуют'}")
    for segment in passport["audience"]["segments"]:
        if segment["enabled"]:
            st.text(f"{segment['title']} · " + ("Клиенты" if segment["role"] == "client" else "Партнёры"))
            st.text("Обязательные признаки · " + ", ".join(segment["required_public_evidence"]))
            if segment["exclusions"]:
                st.text("Исключения · " + ", ".join(segment["exclusions"]))
    if passport["audience"]["legacy_profile"]:
        st.caption("К этой версии приложен ранее сохранённый портрет ЦА.")


def _render_feedback(store, prefix, revision, state):
    with st.expander("Наблюдения и предложения"):
        observations = state.get("observations", [])
        for entry in reversed(observations):
            st.text(entry["observation"])
            st.text(f"Подтверждение · {entry['evidence']}")
        with st.form(f"{prefix}_observations_{revision}"):
            observation = st.text_area("Что произошло", max_chars=600)
            evidence = st.text_input("Где это подтверждено", max_chars=300)
            submit = st.form_submit_button("Сохранить наблюдение")
        if submit:
            if not observation.strip() or not evidence.strip():
                st.warning("Укажите наблюдение и источник.")
            else:
                try:
                    store.save(revision, {**state, "observations": [
                        *observations, {"observation": observation.strip(), "evidence": evidence.strip()},
                    ]})
                except (requests.RequestException, RuntimeError):
                    st.error("Сохранение наблюдения не подтверждено. Обновите страницу.")
                else:
                    st.rerun()
        reports = state.get("commercial_reports", {})
        if reports:
            period = max(reports)
            report = reports[period]
            proposals = commercial_proposals(report["counts"], report["amounts"])
            if proposals:
                st.caption(f"Предложения по итогам периода {period}")
                for proposal in proposals:
                    st.text(proposal)


def render_stagirite_marketing_signals(owner_id):
    project_id = pilot_project(
        owner_id, _runtime_secret("AGENCY_MARKETING_PILOT_OWNER"),
        _runtime_secret("AGENCY_MARKETING_PILOT_PROJECT"),
    )
    if project_id is None:
        return
    store = InterviewStore(_request, owner_id, project_id, _encrypt_state, _decrypt_state)
    try:
        _, state = store.load()
    except (requests.RequestException, LookupError, ValueError, RuntimeError):
        st.caption("Коммерческие итоги пилота временно недоступны.")
        return
    from marketing_journal_ui import render_editorial_run_status

    render_editorial_run_status()
    publications = state.get("journal_publications", {})
    if publications:
        last = max(publications.values(), key=lambda item: item["verified_at"])
        with st.expander("Подтверждённые выпуски журнала"):
            st.metric("Проверено выпусков", len(publications))
            st.link_button(last["title"], last["url"])
            st.caption(f"Последняя проверка · {last['verified_at'][:16].replace('T', ' ')} UTC")
    reports = state.get("commercial_reports", {})
    if not reports:
        return
    period = max(reports)
    report = reports[period]
    with st.expander(f"Предложения по коммерческим итогам · {period}"):
        proposals = commercial_proposals(report["counts"], report["amounts"])
        if not proposals:
            st.caption("По внесённым показателям рекомендации пока не сформированы.")
        for proposal in proposals:
            st.text(proposal)
