from django.urls import path

from . import views


app_name = "monitoring"


urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path(
        "machines/<str:code>/",
        views.machine_detail,
        name="machine_detail",
    ),
    path("alarms/", views.alarms, name="alarms"),
    path("history/", views.history, name="history"),
    path("system/", views.system_status, name="system"),

    # Giữ tương thích với URL cũ sau khi thay giao diện.
    path("live-data/", views.legacy_live_data, name="live_data"),
    path("plc-signals/", views.legacy_plc_signals, name="plc_signals"),
]
