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
