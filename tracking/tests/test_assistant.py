import json
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from tracking import assistant, integrations
from tracking.models import Connection, Device

AKEY = "sk-ant-test-0123456789abcdefWXYZ"


def _post(c, url, body):
    return c.post(url, json.dumps(body), content_type="application/json")


class AssistantTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user("ops", password="pw-12345678", is_staff=True)
        self.viewer = User.objects.create_user("viewer", password="pw-12345678")
        self.c = Client()
        self.c.login(username="ops", password="pw-12345678")
        now = timezone.now()
        Device.objects.create(imei="356307042441001", name="Pickup 77810", plate="AD 77810",
                              last_packet_utc=now - timedelta(hours=13), last_connection_utc=now - timedelta(hours=13))
        Device.objects.create(imei="356307042441002", name="Hiace 50452")
        Connection.objects.create(claimed_imei="356307042449999", remote="1.2.3.4:5000", handshake="rejected")

    def tearDown(self):
        cache.clear()

    def test_robot_on_every_page_and_can_be_turned_off(self):
        page = self.c.get("/").content.decode()
        self.assertIn('id="mgtBot"', page)
        self.assertIn('data-name="Melo"', page)
        cfg, errors = assistant.clean_config({"name": "Robo", "position": "top-right", "provider": "off",
                                              "audience": "all", "detail": "short", "greeting": "", "instructions": ""})
        self.assertEqual(errors, [])
        assistant.save_config(cfg)  # enabled box not ticked -> off
        self.assertNotIn('id="mgtBot"', self.c.get("/").content.decode())
        self.assertEqual(_post(self.c, "/api/assistant/chat/", {"message": "hi"}).status_code, 404)

    def test_staff_only_audience(self):
        cfg = dict(assistant.DEFAULTS, audience="staff")
        assistant.save_config(cfg)
        v = Client()
        v.login(username="viewer", password="pw-12345678")
        self.assertNotIn('id="mgtBot"', v.get("/").content.decode())
        self.assertIn('id="mgtBot"', self.c.get("/").content.decode())

    def test_context_has_page_tips_and_findings(self):
        d = _post(self.c, "/api/assistant/context/", {"page": "live", "client": {"google_error": "RefererNotAllowedMapError"}}).json()
        self.assertEqual(d["page"]["title"], "Live map")
        titles = " | ".join(f["title"] for f in d["findings"])
        self.assertIn("receiver is offline", titles)
        self.assertIn("no signal", titles)
        self.assertIn("never connected", titles)
        self.assertIn("rejected connection", titles)
        self.assertIn("Google Maps was rejected", titles)
        self.assertNotIn("356307042449999", json.dumps(d))  # IMEIs are masked
        self.assertEqual(d["findings"][0]["level"], "error")
        self.assertIn("ops", d["greeting"])

    def test_chat_without_key_uses_builtin_guide(self):
        d = _post(self.c, "/api/assistant/chat/", {"message": "Why do my vehicles show No signal?", "page": "dashboard"}).json()
        self.assertEqual(d["source"], "guide")
        self.assertIn("No signal", d["reply"])
        d = _post(self.c, "/api/assistant/chat/", {"message": "check for problems"}).json()
        self.assertIn("receiver is offline", d["reply"])

    def test_chat_with_claude(self):
        integrations.save("anthropic_api_key", AKEY, "ops")
        fake = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Pickup 77810 is offline.")])
        with mock.patch("anthropic.Anthropic") as A:
            A.return_value.beta.messages.create.return_value = fake
            d = _post(self.c, "/api/assistant/chat/", {"message": "What is wrong?", "page": "live",
                                                        "history": [{"role": "assistant", "text": "hello"},
                                                                    {"role": "user", "text": "hi"},
                                                                    {"role": "assistant", "text": "Hi!"}]}).json()
        self.assertEqual(d, {"reply": "Pickup 77810 is offline.", "source": "claude"})
        kw = A.return_value.beta.messages.create.call_args.kwargs
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["fallbacks"], "default")
        self.assertEqual(kw["messages"][0]["role"], "user")  # leading assistant turn dropped
        self.assertEqual(kw["messages"][-1], {"role": "user", "content": "What is wrong?"})
        self.assertIn("Pickup 77810", kw["system"][1]["text"])   # live snapshot is sent
        self.assertNotIn(AKEY, json.dumps(kw["system"]))

    def test_claude_failure_falls_back_to_guide(self):
        import anthropic
        integrations.save("anthropic_api_key", AKEY, "ops")
        integrations.save("anthropic_model", "claude-haiku-4-5", "ops")
        with mock.patch("anthropic.Anthropic") as A:
            A.return_value.messages.create.side_effect = anthropic.APIConnectionError(request=mock.Mock())
            d = _post(self.c, "/api/assistant/chat/", {"message": "how do I add a new tracker"}).json()
        self.assertEqual(d["source"], "guide")
        self.assertIn("could not reach Anthropic", d["reply"])
        self.assertIn("Devices", d["reply"])

    def test_settings_save_and_permissions(self):
        r = self.c.post("/settings/assistant/", {"enabled": "on", "name": "Robo", "position": "bottom-left",
                                                 "provider": "claude", "audience": "all", "detail": "detailed",
                                                 "greeting": "Hello {user}", "instructions": "Answer in Arabic too."})
        self.assertEqual(r.status_code, 302)
        cfg = assistant.get_config()
        self.assertEqual((cfg["name"], cfg["position"], cfg["detail"]), ("Robo", "bottom-left", "detailed"))
        self.assertIn("AI assistant", self.c.get("/settings/").content.decode())
        v = Client()
        v.login(username="viewer", password="pw-12345678")
        self.assertNotEqual(v.post("/settings/assistant/", {"name": "x"}).status_code, 200)
        self.assertEqual(assistant.get_config()["name"], "Robo")
        bad = self.c.post("/settings/assistant/", {"enabled": "on", "name": "A", "position": "middle", "provider": "claude",
                                                   "audience": "all", "detail": "short"})
        self.assertEqual(bad.status_code, 302)
        self.assertEqual(assistant.get_config()["position"], "bottom-left")  # invalid form not saved

    def test_rate_limit(self):
        with mock.patch.object(assistant, "CHAT_LIMIT", 2):
            codes = [_post(self.c, "/api/assistant/chat/", {"message": "hi"}).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_anthropic_key_test_button(self):
        with mock.patch("anthropic.Anthropic") as A:
            A.return_value.models.list.return_value = [SimpleNamespace(id="claude-opus-5-5"), SimpleNamespace(id="claude-sonnet-5-5")]
            r = self.c.post("/settings/integrations/test-anthropic/", {"anthropic_api_key": AKEY, "anthropic_model": "claude-opus-5-5"}).json()
        self.assertTrue(r["ok"])
        self.assertIn("claude-sonnet-5-5", r["models"])
