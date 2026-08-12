import os

from neona_dialog_policy import worker_forever


if __name__ == "__main__":
    worker_forever(int(os.getenv("NEONA_POLL_SECONDS", "15")))
