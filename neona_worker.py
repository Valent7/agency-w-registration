import os

from neona_telegram_dialogs import worker_forever


if __name__ == "__main__":
    worker_forever(int(os.getenv("NEONA_POLL_SECONDS", "15")))
