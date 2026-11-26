from datetime import datetime, timedelta, timezone
from unittest import TestCase

from neonia_network_proof import current_network_proof


class NetworkProofTests(TestCase):
    def test_professional_admission(self):
        now = datetime(2026, 11, 24, tzinfo=timezone.utc)
        def proof(text, day=0, url="https://vk.com/wall-20_30?reply=40"):
            return current_network_proof([{
                "text": text, "timestamp": int((now - timedelta(days=day)).timestamp()), "url": url,
            }], now)

        negatives = (
            "Отрицательный случай А. Профессиональная принадлежность не подтверждена.",
            "Отрицательный случай Б. 59 из 100, но профессиональная принадлежность не подтверждена.",
            "Раньше работала в MLM; сейчас не работаю в MLM, продаю продукты и собираю команду",
            "Состою в группе про сетевой маркетинг и лайкаю посты",
            "У меня 50 тысяч подписчиков",
            "Продаю товары своего магазина и ищу клиентов",
            "MLM. Предлагаю продукты и не занимаюсь построением команды",
        )
        for text in negatives:
            self.assertIsNone(proof(text), text)
        for activity in ("развиваю команду", "привлекаю клиентов", "ищу партнёров"):
            self.assertIsNotNone(proof(f"Работаю в MLM, продаю продукты и {activity}"))
        self.assertIsNone(proof("Работаю в MLM, продаю продукты и развиваю команду", day=31))
        self.assertIsNone(proof("Работаю в MLM, продаю продукты и развиваю команду", url="https://vk.com/id1"))

    def test_more_recent_notice_of_departure_invalidates_older_proof(self):
        now = datetime(2026, 11, 24, tzinfo=timezone.utc)
        rows = [{
            "timestamp": int((now - timedelta(days=1)).timestamp()),
            "url": "https://vk.com/wall-20_30?reply=40",
            "text": "Работаю в MLM, продаю продукты и развиваю команду",
        }, {
            "timestamp": int(now.timestamp()),
            "url": "https://vk.com/wall-20_30?reply=41", "text": "Ушла из MLM.",
        }]
        self.assertIsNone(current_network_proof(rows, now))

    def test_wall_reader_accepts_only_original_posts_by_profile_owner(self):
        from unittest.mock import patch
        import vk_scout

        now = int(datetime.now(timezone.utc).timestamp())
        record = {"id": 3, "from_id": 7, "owner_id": 7, "date": now,
                  "text": "Работаю в MLM, продаю продукты и развиваю команду"}
        with patch.object(vk_scout, "_vk_api", return_value={"items": [
            {**record, "id": 1, "from_id": 8},
            {**record, "id": 2, "copy_history": [{"id": 99}]},
            {**record, "id": 4, "date": now - 31 * 86400},
            record,
        ]}):
            rows = vk_scout._read_vk_professional_posts(7)
        self.assertEqual([row["url"] for row in rows], ["https://vk.com/wall7_3"])
        from neonia_network_proof import vk_network_proof
        raw = {"public_professional_posts": rows}
        self.assertIsNotNone(vk_network_proof({"vk_user_id": 7, "raw_profile": raw}))
        self.assertIsNone(vk_network_proof({"vk_user_id": 8, "raw_profile": raw}))

    def test_vanity_activity_does_not_replace_profession_and_own_post_can_prove_recent_use(self):
        from vk_scout import _vk_recent_activity
        from neonia_network_proof import vk_network_proof

        now = datetime.now(timezone.utc)
        text = "Работаю в MLM, продаю продукты и развиваю команду"
        own_posts = [{"url": "https://vk.com/wall7_3", "text": text,
                      "timestamp": int((now - timedelta(days=10)).timestamp())}]
        candidate = {"vk_user_id": 7, "raw_profile": {
            "last_seen": {"time": int((now - timedelta(hours=10)).timestamp())},
            "public_professional_posts": own_posts,
        }}
        self.assertTrue(_vk_recent_activity(candidate, now=now)["active"])
        self.assertIsNotNone(vk_network_proof(candidate))
        candidate["raw_profile"].pop("last_seen")
        self.assertFalse(_vk_recent_activity(candidate, now=now)["active"])
        own_posts[0]["timestamp"] = int(now.timestamp())
        self.assertTrue(_vk_recent_activity(candidate, now=now)["active"])
        candidate["raw_profile"].pop("public_professional_posts")
        self.assertIsNone(vk_network_proof(candidate))

    def test_denied_walls_do_not_prevent_scanning_next_source(self):
        from unittest.mock import patch
        import vk_scout

        now = int(datetime.now(timezone.utc).timestamp())
        sources = [{"community_id": uid} for uid in range(1, 5)]
        def members(source, **kwargs):
            uid = source["community_id"]
            return {"members": [{
                "vk_user_id": uid, "source_community_id": uid,
                "can_write_private_message": True, "status_text": "MLM",
            }]}
        def wall(uid):
            if uid < 4:
                raise vk_scout.VKScoutError("VK API wall.get: 15 — Access denied")
            return [{"url": "https://vk.com/wall4_1", "timestamp": now,
                     "text": "Работаю в MLM, продаю продукты и развиваю команду"}]
        with (
            patch.object(vk_scout, "load_vk_sources", return_value=sources),
            patch.object(vk_scout, "fetch_vk_members", side_effect=members),
            patch.object(vk_scout, "network_only", return_value=True),
            patch.object(vk_scout, "save_vk_candidates", return_value=4),
            patch.object(vk_scout, "_read_vk_professional_posts", side_effect=wall),
        ):
            result = vk_scout.scan_vk_sources(77, include_author_posts=True)
        self.assertEqual(result["author_walls_failed"], 3)
        self.assertEqual(result["author_walls_read"], 1)
        self.assertEqual(result["active_context_ids"], [4])

    def test_wall_budget_prioritizes_public_professional_context(self):
        from unittest.mock import patch
        import vk_scout

        now = int(datetime.now(timezone.utc).timestamp())
        ordinary = [
            {"vk_user_id": uid, "source_community_id": 1,
             "raw_profile": {"last_seen": {"time": now}}}
            for uid in range(1, 41)
        ]
        ordinary[0]["raw_profile"]["public_professional_comments"] = [{"text": "Спасибо за эфир"}]
        topical = {"vk_user_id": 71, "source_community_id": 1, "status_text": "Работаю в MLM"}
        with (
            patch.object(vk_scout, "load_vk_sources", return_value=[{"community_id": 1}]),
            patch.object(vk_scout, "fetch_vk_members", return_value={"members": ordinary + [topical]}),
            patch.object(vk_scout, "network_only", return_value=True),
            patch.object(vk_scout, "save_vk_candidates", return_value=41),
            patch.object(vk_scout, "_read_vk_professional_posts", return_value=[]) as read_wall,
        ):
            result = vk_scout.scan_vk_sources(77, include_author_posts=True)

        self.assertEqual(read_wall.call_args_list[0].args, (71,))
        self.assertEqual(result["author_walls_read"], 20)
