"""Reservation ledger for the single approved Neonia control search."""
from contextlib import closing
from pathlib import Path
import sqlite3

MODEL = "gpt-5.4-mini-2026-03-17"
# Quarter microdollars, to represent both token prices with integers.
LIMIT = 1_000_000
INPUT_RATE = 3
OUTPUT_RATE = 18


class ControlBudgetExceeded(RuntimeError):
    pass


def reserve_control_request(ledger: Path, *, input_tokens: int, max_output_tokens: int) -> int:
    """Reserve the worst case before sending. Never refund an uncertain attempt."""
    if type(input_tokens) is not int or type(max_output_tokens) is not int:
        raise ValueError("Token limits must be integers")
    if input_tokens <= 0 or max_output_tokens <= 0:
        raise ValueError("Token limits must be positive")
    reserved = input_tokens * INPUT_RATE + max_output_tokens * OUTPUT_RATE
    with closing(sqlite3.connect(ledger)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS reservations (cost INTEGER NOT NULL)")
        total = db.execute("SELECT COALESCE(SUM(cost), 0) FROM reservations").fetchone()[0]
        if total + reserved > LIMIT:
            raise ControlBudgetExceeded("The approved USD 0.25 cap would be exceeded")
        db.execute("INSERT INTO reservations (cost) VALUES (?)", (reserved,))
    return reserved


def request_control_assessment(
    session, ledger: Path | None = None, *, api_key: str, input_text: str,
    max_output_tokens: int = 2_000, reserve=None,
):
    """Send one explicitly requested, tool-free assessment without retrying."""
    endpoint = "https://api.openai.com/v1/responses"
    headers = {"Authorization": f"Bearer {api_key}"}
    prompt = {"model": MODEL, "input": input_text}
    count = session.post(
        endpoint + "/input_tokens", headers=headers, json=prompt,
        timeout=30, allow_redirects=False,
    )
    count.raise_for_status()
    input_tokens = count.json()["input_tokens"]
    if type(input_tokens) is not int or input_tokens <= 0:
        raise ValueError("Invalid provider token count")
    if type(max_output_tokens) is not int or max_output_tokens <= 0:
        raise ValueError("Invalid output token limit")
    ceiling = input_tokens * INPUT_RATE + max_output_tokens * OUTPUT_RATE
    if ceiling > LIMIT:
        raise ControlBudgetExceeded("Assessment exceeds USD 0.25")
    if reserve is None:
        if ledger is None:
            raise ValueError("A durable reservation is required")
        reserve_control_request(ledger, input_tokens=input_tokens, max_output_tokens=max_output_tokens)
    else:
        reserve(ceiling)

    response = session.post(
        endpoint, headers=headers,
        json={**prompt, "max_output_tokens": max_output_tokens,
              "service_tier": "default", "store": False},
        timeout=60, allow_redirects=False,
    )
    response.raise_for_status()
    return response.json()


CONTROL_RUN_ID = "vk-pilot-control-001"


def cloud_vk_assessor(session, owner_id, api_key, insert_claim, stats):
    """The cloud database grants at most one model request across all replicas."""
    attempted = False

    def assess(instructions, prompt):
        nonlocal attempted
        if attempted:
            raise ControlBudgetExceeded("The control request has already been used")
        attempted = True

        def reserve(ceiling):
            rows = insert_claim({
                "run_id": CONTROL_RUN_ID, "owner_id": owner_id, "reserved_units": ceiling,
            })
            if len(rows) != 1 or rows[0].get("run_id") != CONTROL_RUN_ID:
                raise ControlBudgetExceeded("The control reservation could not be confirmed")
            stats["reserved_usd"] = ceiling / 4_000_000

        response = request_control_assessment(
            session, api_key=api_key, input_text=f"{instructions}\n\n{prompt}",
            max_output_tokens=16_000, reserve=reserve,
        )
        usage = response.get("usage") or {}
        if all(type(usage.get(key)) is int for key in ("input_tokens", "output_tokens")):
            cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
            stats["reported_usage"] = usage
            stats["estimated_usd_from_usage"] = (
                (usage["input_tokens"] - cached) * 0.75
                + cached * 0.075 + usage["output_tokens"] * 4.5
            ) / 1_000_000
        return response

    return assess
