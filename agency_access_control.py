"""Agency W director-controlled access revocations.

Never remove historical partner records to revoke product access.
Compare Telegram numeric IDs, not mutable usernames.
"""
# Former members retain their registration records for audit/structure history.
# W6541575252 is derived from the verified W{telegram_id} registration rule.
REVOKED_TELEGRAM_IDS = frozenset({291173537, 6541575252})


def is_agency_access_revoked(telegram_id: object) -> bool:
    try:
        return int(telegram_id) in REVOKED_TELEGRAM_IDS
    except (ValueError, TypeError, OverflowError):
        return False
