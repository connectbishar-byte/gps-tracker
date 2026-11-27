from django.urls import path
from rest_framework.routers import DefaultRouter

from . import alerts_views, api, driverscore, places, vehicledash

router = DefaultRouter()
router.register("devices", api.DeviceViewSet, basename="device")
router.register("records", api.RecordViewSet, basename="record")
router.register("packets", api.PacketViewSet, basename="packet")
router.register("connections", api.ConnectionViewSet, basename="connection")
router.register("tests", api.TestRunViewSet, basename="testrun")
router.register("places", places.SavedPlaceViewSet, basename="place")

urlpatterns = [
    path("auth/token/", api.TokenView.as_view(), name="api_token"),
    path("auth/me/", api.me, name="api_me"),
    path("port-events/", api.port_events, name="api_port_events"),
    path("receiver/", api.receiver, name="api_v1_receiver"),
    path("whatsapp/send-text/", alerts_views.api_send_text, name="api_whatsapp_send_text"),
    path("whatsapp/send-media/", alerts_views.api_send_media, name="api_whatsapp_send_media"),
    path("whatsapp/send-certificate/", alerts_views.api_send_media, name="api_whatsapp_send_certificate"),
    path("vehicles/<int:pk>/dashboard/", vehicledash.api_dashboard, name="api_vehicle_dashboard"),
    path("driver-scores/", driverscore.api_scores, name="api_driver_scores"),
    path("driver-scores/<str:kind>/<int:pk>/", driverscore.api_sheet, name="api_driver_sheet"),
] + router.urls
