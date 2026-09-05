import os
import threading

from neona_dialog_policy import worker_forever as neona_worker_forever
from vk_scout_worker import worker_forever as vk_scout_worker_forever

try:
    from telegram_scout_worker import worker_forever as telegram_scout_worker_forever
except Exception as exc:
    telegram_scout_worker_forever = None
    print(f"Telegram Scout disabled at import: {exc}", flush=True)


def _run_vk_scout():
    """VK Scout живёт в отдельном потоке и не блокирует Неону."""
    try:
        vk_scout_worker_forever()
    except Exception as exc:
        print(f"VK Scout stopped: {exc}", flush=True)


def _run_telegram_scout():
    """Telegram Scout заранее готовит резерв и дневную пятёрку Стагирита."""
    if telegram_scout_worker_forever is None:
        return
    try:
        telegram_scout_worker_forever(
            int(os.getenv("TELEGRAM_SCOUT_INTERVAL_SECONDS", "3600"))
        )
    except Exception as exc:
        print(f"Telegram Scout stopped: {exc}", flush=True)


if __name__ == "__main__":
    # Фоновые разведчики не должны блокировать основной цикл Неоны.
    threading.Thread(
        target=_run_vk_scout,
        name="vk-scout-worker",
        daemon=True,
    ).start()

    if telegram_scout_worker_forever is not None:
        threading.Thread(
            target=_run_telegram_scout,
            name="telegram-scout-worker",
            daemon=True,
        ).start()

    # Основной процесс Render остаётся Неоной — как и раньше.
    neona_worker_forever(
        int(os.getenv("NEONA_POLL_SECONDS", "30"))
    )
