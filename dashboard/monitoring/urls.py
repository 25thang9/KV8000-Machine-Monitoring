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
    path("configuration/", views.configuration, name="configuration"),
    path(
        "configuration/import-machines/",
        views.import_machine_csv,
        name="import_machine_csv",
    ),
    path(
        "configuration/machine-template.csv",
        views.machine_import_template,
        name="machine_import_template",
    ),
    path("configuration/machines/<int:pk>/toggle/", views.toggle_machine, name="toggle_machine"),
    path("configuration/plcs/<int:pk>/toggle/", views.toggle_controller, name="toggle_controller"),
    path("health/", views.health, name="health"),
    path("api/dashboard-state/", views.dashboard_state_api, name="dashboard_state_api"),
    path("api/dashboard-stream/", views.dashboard_state_stream, name="dashboard_state_stream"),

    # Giữ tương thích với URL cũ sau khi thay giao diện.
    path("live-data/", views.legacy_live_data, name="live_data"),
    path("plc-signals/", views.legacy_plc_signals, name="plc_signals"),
]
