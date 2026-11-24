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
