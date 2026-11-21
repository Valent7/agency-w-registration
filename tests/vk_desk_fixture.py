"""Offline deterministic repository data for AppTest and browser acceptance."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import vk_scout
import vk_scout_ui

OWNER = 71


class VKDeskFixture:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.today = date.today().isoformat()
        self.yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.time = (now - timedelta(days=2)).isoformat()
        self.next_time = (now + timedelta(days=1)).isoformat()
        self.assignments = [
            {"id": uid, "vk_user_id": uid, "owner_telegram_id": OWNER,
             "status": "reserved" if uid <= 5 or uid >= 42 else "invited",
             "assignment_date": self.today if uid <= 5 else self.yesterday,
             "daily_position": min(uid, 5), "score": 71,
             "invited_at": self.time if 6 <= uid <= 41 else None}
            for uid in range(1, 48)
        ]
        self.people = [{"vk_user_id": uid, "first_name": f"Кандидат {uid:02d}"} for uid in range(1, 48)]
        self.states = []
        self.comments = []
        self.meetings = []
        self.reads = 0

        self.manual_reply(34, OWNER, "Готова обсудить условия")
        self.state(35, {"vk_last_incoming_text": "Могу созвониться завтра",
                        "vk_last_processed_at": self.time})
        for uid in (36, 37):
            self.comments.append({
                "id": uid, "owner_telegram_id": OWNER, "candidate_vk_user_id": uid,
                "post_owner_id": uid, "post_id": 13, "last_inbound_comment_id": 7,
                "last_inbound_at": self.time, "last_inbound_text": "Интересно, напишите подробнее",
            })
        self.state(38, {"meeting_id": 501}, stage="scheduled")
        self.meetings.append({"id": 501, "owner_telegram_id": OWNER,
                              "contact_telegram_id": vk_scout._vk_dialog_contact_id(38),
                              "start_at": self.next_time, "end_at": self.next_time, "status": "Подтверждена"})
        for uid in (39, 40, 41):
            self.state(uid, {"vk_desk_refused_at": self.time}, personal=True)

    def state(self, uid, context, *, personal=False, stage="idle", owner=OWNER):
        key = vk_scout._vk_dialog_contact_id(uid, personal=personal)
        existing = next((row for row in self.states if row["contact_telegram_id"] == key
                         and row["owner_telegram_id"] == owner), None)
        if existing:
            existing["context"].update(context)
        else:
            self.states.append({"owner_telegram_id": owner, "contact_telegram_id": key,
                                "last_incoming_message_id": 10, "stage": stage,
                                "context": context, "updated_at": self.time})

    def manual_reply(self, assignment_id, owner, text):
        self.state(assignment_id, {
            "vk_manual_last_processed_at": datetime.now(timezone.utc).isoformat(),
            "vk_manual_last_inbound_text": text, "vk_manual_pending_inbound": text,
            "vk_manual_pending_reply": "Спасибо, обсудим детали.",
        }, personal=True, owner=owner)

    def get(self, table, params):
        self.reads += 1
        rows = {
            "agency_vk_assignments": self.assignments,
            "agency_dialog_states": self.states,
            "agency_vk_candidates": self.people,
            "agency_vk_comment_threads": self.comments,
            "agency_meetings": self.meetings,
        }[table]
        for key, value in params.items():
            if key in {"select", "order", "limit", "offset"}:
                continue
            if value.startswith("eq."):
                rows = [row for row in rows if str(row.get(key)) == value[3:]]
            elif value.startswith("in.("):
                rows = [row for row in rows if str(row.get(key)) in value[4:-1].split(",")]
            elif value == "not.is.null":
                rows = [row for row in rows if row.get(key) is not None]
            elif value.startswith("neq."):
                rows = [row for row in rows if str(row.get(key)) != value[4:]]
            elif value.startswith("gt."):
                rows = [row for row in rows if str(row.get(key)) > value[3:]]
        offset = params.get("offset", 0)
        return rows[offset:offset + params.get("limit", len(rows))]

    def post(self, table, payload):
        assert table == "agency_dialog_states"
        self.states.append(dict(payload))
        return [payload]

    def update(self, table, filters, payload, **kwargs):
        rows = self.get(table, filters)
        for row in rows:
            row.update(payload)
        return rows

    @contextmanager
    def connected(self):
        with patch.multiple(vk_scout, _sb_get=self.get, _sb_post=self.post, _sb_patch=self.update), patch.multiple(
            vk_scout_ui, process_manual_vk_private_reply=self.manual_reply,
            mark_manual_vk_private_reply_sent=lambda *args: self.sent(*args),
        ):
            yield

    def sent(self, assignment_id, owner, text):
        self.state(assignment_id, {
            "vk_manual_last_outbound_at": datetime.now(timezone.utc).isoformat(),
            "vk_manual_last_outbound_text": text, "vk_manual_pending_reply": None,
        }, personal=True, owner=owner)
