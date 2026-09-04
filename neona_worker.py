import os
import threading

from neona_dialog_policy import worker_forever as neona_worker_forever
from vk_scout_worker import worker_forever as vk_scout_worker_forever


def _run_vk_scout():
    """VK Scout живёт в отдельном потоке и не блокирует Неону."""
    vk_scout_worker_forever()


if __name__ == "__main__":
    # VK Scout запускается рядом с Неоной, но в отдельном фоновом потоке.
    # Даже если VK временно недоступен, основной цикл Неоны продолжает работать.
    threading.Thread(
        target=_run_vk_scout,
        name="vk-scout-worker",
        daemon=True,
    ).start()

    # Основной процесс Render остаётся Неоной — как и раньше.
    neona_worker_forever(
        int(os.getenv("NEONA_POLL_SECONDS", "30"))
    )
