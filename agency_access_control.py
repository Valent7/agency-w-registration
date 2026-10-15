"""Agency W director-controlled access revocations.

Never remove historical partner records to revoke product access.
Compare Telegram numeric IDs, not mutable usernames.
"""
REVOKED_TELEGRAM_IDS = frozenset({291173537})


def is_agency_access_revoked(telegram_id: object) -> bool:
    try:
        return int(telegram_id) in REVOKED_TELEGRAM_IDS
    except (ValueError, TypeError, OverflowError):
        return False
