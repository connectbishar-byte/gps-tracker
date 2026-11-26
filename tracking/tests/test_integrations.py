import io
import json
import os
import urllib.error
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from tracking import integrations
from tracking.models import IntegrationSetting

GKEY = "AIzaSyTEST-key-0123456789abcdef"
OKEY = "sk-test-0123456789abcdefWXYZ"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class IntegrationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user("ops", password="pw-12345678", is_staff=True)
        self.viewer = User.objects.create_user("viewer", password="pw-12345678")
        self.c = Client()
        self.c.login(username="ops", password="pw-12345678")

    def tearDown(self):
        cache.clear()

    def test_save_encrypts_masks_and_clears(self):
        r = self.c.post("/settings/integrations/", {"google_maps_api_key": GKEY, "openai_api_key": OKEY, "openai_model": "my-model"})
        self.assertEqual(r.status_code, 302)
        row = IntegrationSetting.objects.get(key="openai_api_key")
        self.assertNotIn(OKEY, row.value)          # encrypted at rest
        self.assertEqual(row.updated_by, "ops")
        self.assertEqual(integrations.get("openai_api_key"), OKEY)
        self.assertEqual(integrations.get("openai_model"), "my-model")
        page = self.c.get("/settings/").content.decode()
        self.assertNotIn(OKEY, page)                # never shown in full
        self.assertIn("sk-t…WXYZ", page)
        # blank field keeps the value; Remove clears it
        self.c.post("/settings/integrations/", {"openai_api_key": ""})
        self.assertEqual(integrations.get("openai_api_key"), OKEY)
        self.c.post("/settings/integrations/", {"clear_openai_api_key": "on"})
        self.assertEqual(integrations.get("openai_api_key"), "")

    def test_rejects_values_with_spaces(self):
        self.c.post("/settings/integrations/", {"openai_api_key": "not a key"})
        self.assertEqual(integrations.get("openai_api_key"), "")

    def test_environment_fallback_and_override(self):
        with mock.patch.dict(os.environ, {"FLEET_GOOGLE_MAPS_API_KEY": "AIza-from-env"}):
            cache.clear()
            self.assertEqual(integrations.get("google_maps_api_key"), "AIza-from-env")
            integrations.save("google_maps_api_key", GKEY, "ops")
            self.assertEqual(integrations.get("google_maps_api_key"), GKEY)

    def test_secret_key_change_reads_as_unset(self):
        integrations.save("openai_api_key", OKEY, "ops")
        with override_settings(SECRET_KEY="a-different-secret-key-for-this-test-only-0123456789"):
            cache.clear()
            self.assertEqual(integrations.get("openai_api_key"), "")
            row = [s for s in integrations.status() if s["name"] == "openai_api_key"][0]
            self.assertTrue(row["unreadable"])

    def test_viewer_cannot_see_or_change(self):
        v = Client()
        v.login(username="viewer", password="pw-12345678")
        self.assertNotIn("id=\"integrations\"", v.get("/settings/").content.decode())
        r = v.post("/settings/integrations/", {"openai_api_key": OKEY})
        self.assertIn(r.status_code, (302, 403))
        self.assertEqual(integrations.get("openai_api_key"), "")
        r = v.post("/settings/integrations/test-openai/", {"openai_api_key": OKEY})
        self.assertIn(r.status_code, (302, 403))

    def test_maps_key_only_for_signed_in_pages(self):
        integrations.save("google_maps_api_key", GKEY, "ops")
        self.assertIn(GKEY.replace("-", "\\u002D"), self.c.get("/live/").content.decode())
        self.assertIn("Leaflet.GoogleMutant", self.c.get("/live/").content.decode())
        self.assertNotIn("AIzaSyTEST", Client().get("/login/").content.decode())

    def test_no_key_means_no_google(self):
        page = self.c.get("/live/").content.decode()
        self.assertNotIn("Leaflet.GoogleMutant", page)
        self.assertIn('gkey: ""', page)

    def test_openai_test_button(self):
        body = json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_Resp(body)) as m:
            r = self.c.post("/settings/integrations/test-openai/", {"openai_api_key": OKEY, "openai_model": "model-b"}).json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["models"], ["model-a", "model-b"])
        self.assertEqual(m.call_args[0][0].get_header("Authorization"), f"Bearer {OKEY}")
        with mock.patch("urllib.request.urlopen", return_value=_Resp(body)):
            r = self.c.post("/settings/integrations/test-openai/", {"openai_api_key": OKEY, "openai_model": "nope"}).json()
        self.assertFalse(r["ok"])
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b'{"error": {"message": "Incorrect API key"}}'))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            r = self.c.post("/settings/integrations/test-openai/", {"openai_api_key": OKEY}).json()
        self.assertFalse(r["ok"])
        self.assertIn("401", r["detail"])
        r = self.c.post("/settings/integrations/test-openai/", {}).json()
        self.assertFalse(r["ok"])


class AssetVersionTests(TestCase):
    def test_own_static_files_carry_a_version(self):
        from tracking.views import ASSET_V
        User.objects.create_user("v2", password="pw-12345678")
        c = Client()
        c.login(username="v2", password="pw-12345678")
        page = c.get("/live/").content.decode()
        self.assertIn(f"tracking/app.js?v={ASSET_V}", page)
        self.assertIn(f"tracking/vehicle-panel.js?v={ASSET_V}", page)
