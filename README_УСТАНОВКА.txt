АГЕНТСТВО W — INSTAGRAM-КОММЕНТАРИИ

Что уже сделано
1. OAuth Instagram Login запрашивает разрешение instagram_business_manage_comments.
2. После подключения аккаунт автоматически подписывается на webhooks messages и comments.
3. Webhook принимает комментарии к Reels и постам и сохраняет их в Supabase.
4. Неона автоматически готовит черновик публичного ответа.
5. В интерфейсе Агентства W есть отдельный блок «Instagram комментарии».
6. Ответ можно отредактировать; он отправляется только по кнопке «Утвердить и отправить».
7. Показываются 20 последних веток, остальные остаются в архиве.

Порядок установки
1. В Supabase откройте SQL Editor и один раз выполните весь файл
   instagram_comments_setup.sql.
2. В GitHub замените основной streamlit_app.py готовым файлом streamlit_app.py.
3. В репозитории Render замените instagram_webhook.py готовым файлом
   instagram_webhook.py и дождитесь успешного Deploy.
4. В Meta Webhooks убедитесь, что для Instagram отмечено поле comments.
5. В Агентстве W откройте «Разрешения Instagram» и нажмите
   «Обновить разрешения Instagram». Подтвердите доступ к комментариям.
6. Оставьте тестовый комментарий под своим Reels или постом НЕ с аккаунта
   владельца. Откройте: Агенты → Неона → Instagram комментарии.

Новые секреты добавлять не нужно. Используются уже существующие FERNET_KEY,
INSTAGRAM_OAUTH_SERVICE_URL, SUPABASE_URL, SUPABASE_SECRET_KEY и OPENAI_API_KEY.

Facebook Page для этой схемы не требуется: используется прямой Instagram Login.
