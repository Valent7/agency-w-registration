Agency W — этап 1: Неония-разведчик

Готовые файлы:
1. neonia_intelligence_v2_PUBLIC_SIGNALS.py
   - заменяет текущий neonia_intelligence_v2.py;
   - добавляет универсальные public_signals с обратной совместимостью telegram_signals.

2. telegram_scout_worker_BUSINESS_GATE.py
   - заменяет текущий telegram_scout_worker.py;
   - подключает единый neonia_candidate_policy;
   - без подтвержденного бизнес-сигнала человек не попадает в холодную пятёрку.

3. neonia_public_scout.py
   - новый модуль;
   - веб-поиск через OpenAI Responses API web_search;
   - режим web + режим youtube;
   - First Customer Finder-подход: публичный сигнал -> первоисточник -> scoring -> shortlist;
   - YouTube-комментарий только как черновик, без автопубликации.

4. neonia_public_scout_ui.py
   - новый UI-модуль для ручного тестирования качества/стоимости.

Подключение в streamlit_app.py (после получения самой свежей версии файла):
- импортировать render_neonia_public_scout;
- добавить пункт "🌐 Неония-разведчик";
- передать сохранённый passport/profile/analysis как target_profile.

Важно:
- первую версию запускаем вручную, а не фоном, чтобы проверить качество и не расходовать web_search на всех партнёров без контроля;
- после 2–3 удачных тестов можно добавить background worker и Supabase-историю;
- публикацию YouTube-комментария через OAuth подключаем отдельным этапом после проверки текста.
