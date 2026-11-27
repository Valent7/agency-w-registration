import os
import threading
from stagirite_night_control import worker_forever as stagirite_night_worker_forever

from neona_dialog_policy_v3 import worker_forever as neona_worker_forever

if __name__ == "__main__":
    # Independent from Neona's dialogue loop.
    # UTC 21:00 -> 00:00 Moscow, followed by bounded night rechecks.
    threading.Thread(target=stagirite_night_worker_forever,
                     name="stagirite-night-watch",daemon=True).start()
    # Legacy loops may charge each owner's OpenAI account on startup.
    # The crisis pause persists across service restarts. The model-free v85
    # pilot is run separately via neonia_pilot_daily.py.
    print("NEONIA_SPEND_GUARD legacy VK and Telegram loops paused", flush=True)

    # Не возвращать частый опрос Telegram: он уже приводил к лишним запросам,
    # расходам и FloodWait. Даже если в окружении случайно осталось старое
    # значение 15/30 секунд, ниже действует безопасный минимум 120 секунд.
    requested_poll = max(120, int(os.getenv("NEONA_POLL_SECONDS", "300")))

    # Realtime is opt-in per owner. All other owners retain unchanged polling.
    # The existing polling worker automatically resumes a pilot owner whenever
    # that owner's realtime connection is not confirmed healthy.
    from neona_realtime_worker import launch_from_environment
    pilots = launch_from_environment(requested_poll)
    neona_worker_forever(
        requested_poll,
        owner_active_check=pilots.is_ready if pilots else None,
    )
