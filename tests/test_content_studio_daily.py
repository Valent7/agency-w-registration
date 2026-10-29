import inspect
import unittest

import content_studio_daily as daily


PROJECT = {
    "id": "project-1",
    "name": "Агентство W",
    "product": "ИИ-сотрудники, которые помогают передавать рутину",
    "audience": "люди, которым важно освободить время",
    "goal": "показывать практическую пользу ИИ",
    "tone": "ясно и по-человечески",
    "cta": "узнать больше",
    "facts": "Агентство помогает организовать поиск, диалоги и контент",
    "visual_style": "голубой и золото",
    "avoid": "не обещать доход",
    "platforms": ["instagram", "vk", "threads"],
    "formats": ["post", "carousel", "reel"],
}


class Store:
    def list_connections(self, owner_id, project_id):
        self.seen = (owner_id, project_id)
        return [
            {"platform": "instagram"},
            {"platform": "vk"},
        ]


class Service:
    def __init__(self):
        self.calls = []

    def create_material(
        self, owner_id, project_id, topic, content_format, platforms, *, request_id
    ):
        self.calls.append(
            (owner_id, project_id, topic, content_format, list(platforms), request_id)
        )
        return [{"id": content_format + "-" + platform} for platform in platforms]


PACK = {
    "core_idea": "ИИ нужен не ради моды, а чтобы вернуть человеку время.",
    "cta": "Посмотрите, какую одну задачу можно передать ИИ уже сегодня.",
    "reel": {
        "hook": "Куда исчез ваш вечер?",
        "script": "Покажем одну повторяющуюся задачу и более лёгкий путь.",
        "caption": "Начните с одной рутины.",
    },
    "story": {
        "frames": ["Сколько времени съедает рутина?", "Выберите одну задачу.", "Передайте подготовку ИИ."],
        "status": "Верните себе хотя бы один час.",
    },
    "post": {
        "instagram": "Живой пост для Instagram.",
        "whatsapp": "Короткий статус.",
        "threads": "Разговорный Threads.",
        "vk": "Подробный пост ВК.",
        "linkedin": "Профессиональный LinkedIn.",
    },
    "carousel": {
        "title": "Куда уходит время?",
        "slides": ["Рутина незаметна", "Посчитайте повторы", "Выберите одно", "Передайте ИИ", "Верните время"],
        "caption": "Одна задача — хороший старт.",
    },
}


class DailyContentTests(unittest.TestCase):
    def test_normalise_keeps_only_selected_channels_and_formats(self):
        result = daily._normalise_pack(
            PACK,
            ["whatsapp", "linkedin"],
            ["story", "post"],
        )
        self.assertEqual(set(result["post"]), {"whatsapp", "linkedin"})
        self.assertEqual(result["reel"], {})
        self.assertEqual(result["carousel"], {})
        self.assertEqual(len(result["story"]["frames"]), 3)

    def test_manual_channels_are_prepared_but_never_sent_to_factory_publishers(self):
        store, service = Store(), Service()
        jobs, skipped = daily._queue_supported(
            77,
            PROJECT,
            store,
            service,
            PACK,
            ["instagram", "whatsapp", "vk", "linkedin"],
            ["reel", "story", "post", "carousel"],
            "abcdef1234567890abcdef1234567890",
        )
        self.assertEqual(store.seen, (77, "project-1"))
        self.assertEqual(
            {call[3] for call in service.calls},
            {"reel", "post", "carousel"},
        )
        for call in service.calls:
            self.assertEqual(set(call[4]), {"instagram", "vk"})
            self.assertNotIn("whatsapp", call[4])
            self.assertNotIn("linkedin", call[4])
        self.assertEqual(len(jobs), 6)
        self.assertFalse(any("WhatsApp" in item or "LinkedIn" in item for item in skipped))

    def test_each_platform_gets_its_own_copy_paste_package(self):
        instagram = daily._channel_package_text(
            PACK, "instagram", ["reel", "story", "post", "carousel"]
        )
        whatsapp = daily._channel_package_text(
            PACK, "whatsapp", ["story", "post"]
        )
        threads = daily._channel_package_text(
            PACK, "threads", ["post"]
        )
        vk = daily._channel_package_text(
            PACK, "vk", ["reel", "post", "carousel"]
        )
        linkedin = daily._channel_package_text(
            PACK, "linkedin", ["post"]
        )

        self.assertIn("REELS", instagram)
        self.assertIn("STORIES", instagram)
        self.assertIn("КАРУСЕЛЬ", instagram)
        self.assertEqual(whatsapp, PACK["story"]["status"])
        self.assertNotIn("WhatsApp Status", whatsapp)
        self.assertNotIn("Готово к публикации", whatsapp)
        self.assertNotIn("ТЕКСТ", whatsapp)
        self.assertNotIn("REELS", whatsapp)
        self.assertIn("Разговорный Threads.", threads)
        self.assertNotIn("КАРУСЕЛЬ", threads)
        self.assertIn("Подробный пост ВК.", vk)
        self.assertIn("КАРУСЕЛЬ", vk)
        self.assertIn("Профессиональный LinkedIn.", linkedin)
        self.assertNotEqual(instagram, linkedin)

    def test_create_package_does_not_automatically_queue_publication(self):
        source = inspect.getsource(daily.render_daily_content_hub)
        self.assertNotIn("_queue_supported(", source)
        self.assertIn("Ничего автоматически не опубликовано", source)

    def test_pack_text_is_plain_readable_text_with_real_line_breaks(self):
        text = daily._pack_text(
            PACK,
            ["instagram", "whatsapp", "linkedin"],
            ["reel", "story", "post", "carousel"],
        )
        self.assertIn("REELS / КОРОТКОЕ ВИДЕО", text)
        self.assertIn("STORIES / STATUS", text)
        self.assertIn("WhatsApp Status", text)
        self.assertIn("LinkedIn", text)
        self.assertIn("КАРУСЕЛЬ", text)
        self.assertIn("\n", text)
        self.assertNotIn("# ", text)
        self.assertNotIn("\\n", text)


    def test_weekly_plan_is_seven_owner_editable_days_with_allowed_goals(self):
        from datetime import date, timedelta
        dates = [date(2026, 10, 29) + timedelta(days=i) for i in range(7)]
        raw = {
            "focus": "Вернуть человеку время",
            "days": [
                {"topic": f"Тема {i + 1}", "goal": goal}
                for i, goal in enumerate(
                    ["value", "engage", "zoom", "story", "value", "engage", "unknown"]
                )
            ],
        }
        plan = daily._normalise_weekly_plan(raw, dates)
        self.assertEqual(len(plan["days"]), 7)
        self.assertEqual(plan["days"][0]["date"], "2026-10-29")
        self.assertEqual(plan["days"][0]["day"], "Четверг")
        self.assertEqual(plan["days"][2]["goal"], "zoom")
        self.assertEqual(plan["days"][6]["goal"], "value")
        text = daily._weekly_plan_text(plan)
        self.assertIn("ПЛАН КОНТЕНТА НА 7 ДНЕЙ", text)
        self.assertNotIn("\\n", text)

    def test_weekly_prompt_makes_stagirite_adviser_not_final_approver(self):
        from datetime import date, timedelta
        dates = [date(2026, 10, 29) + timedelta(days=i) for i in range(7)]
        text = daily._build_weekly_plan_prompt(
            PROJECT,
            "В среду презентация; нужен живой контент",
            dates,
        )
        self.assertIn("окончательное решение", text)
        self.assertIn("Пожелание владельца", text)
        self.assertIn("Допустимые goal", text)

    def test_prompt_uses_project_facts_and_zoom_goal_without_invented_claims(self):
        text = daily._build_prompt(
            PROJECT,
            "Как вернуть время семье",
            "zoom",
            ["instagram", "linkedin"],
            ["reel", "post"],
        )
        self.assertIn("Как вернуть время семье", text)
        self.assertIn("не обещать доход", text)
        self.assertIn("Zoom", text)
        self.assertIn("LinkedIn", text)
        self.assertIn("не выдумывай клиентов, доходы, проценты", text)


if __name__ == "__main__":
    unittest.main()
