from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from tracking import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("live/", views.live, name="live"),
    path("history/", views.history, name="history"),
    path("records/", views.records, name="records"),
    path("records/<int:pk>/", views.record_detail, name="record_detail"),
    path("io/", views.io_inspector, name="io_inspector"),
    path("raw/", views.raw_packets, name="raw"),
    path("raw/<int:pk>/", views.raw_detail, name="raw_detail"),
    path("devices/", views.devices, name="devices"),
    path("devices/<int:pk>/", views.device_detail, name="device_detail"),
    path("connectivity/", views.connectivity, name="connectivity"),
    path("connectivity/selftest/", views.run_selftest, name="run_selftest"),
    path("settings/", views.settings_page, name="settings"),
    path("api/fleet/", views.api_fleet, name="api_fleet"),
    path("api/history/", views.api_history, name="api_history"),
    path("api/receiver/", views.api_receiver, name="api_receiver"),
    path("login/", auth_views.LoginView.as_view(template_name="tracking/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("admin/", admin.site.urls),
]
