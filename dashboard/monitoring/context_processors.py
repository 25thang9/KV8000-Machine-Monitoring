import os

from django.conf import settings


def runtime_info(_request):
    return {
        "runtime_app_env": settings.APP_ENV,
        "runtime_db_engine": settings.DB_ENGINE,
        "runtime_web_port": os.getenv("WEB_PORT", "8001"),
    }
