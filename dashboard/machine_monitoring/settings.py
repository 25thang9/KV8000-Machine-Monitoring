"""Django settings for the production machine-monitoring dashboard."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent
load_dotenv(PROJECT_ROOT / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {
        "1", "true", "yes", "on",
    }


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = env_bool("DJANGO_DEBUG", APP_ENV != "production")

if APP_ENV == "production":
    if len(SECRET_KEY) < 32 or SECRET_KEY in {
        "dev-only-change-me",
        "replace_with_a_long_random_secret",
    }:
        raise RuntimeError(
            "Production yêu cầu DJANGO_SECRET_KEY thật (>= 32 ký tự), "
            "không được dùng giá trị mẫu."
        )
    if DEBUG:
        raise RuntimeError("Production yêu cầu DJANGO_DEBUG=False.")

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv(
        "DJANGO_ALLOWED_HOSTS",
        "127.0.0.1,localhost",
    ).split(",")
    if host.strip()
]

CSRF_TRUSTED_ORIGINS = [
    item.strip()
    for item in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if item.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "monitoring.apps.MonitoringConfig",
]

try:
    import whitenoise  # noqa: F401
    WHITENOISE_AVAILABLE = True
except ImportError:
    WHITENOISE_AVAILABLE = False

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    *(
        ["whitenoise.middleware.WhiteNoiseMiddleware"]
        if WHITENOISE_AVAILABLE else []
    ),
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "monitoring.middleware.OptionalMonitoringLoginMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "machine_monitoring.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "monitoring.context_processors.runtime_info",
            ],
        },
    },
]

WSGI_APPLICATION = "machine_monitoring.wsgi.application"


# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
DB_ENGINE = os.getenv("DB_ENGINE", "postgresql").strip().lower()
DB_CONN_MAX_AGE = env_int("DB_CONN_MAX_AGE", 60)

if DB_ENGINE == "postgresql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["DB_NAME"],
            "USER": os.environ["DB_USER"],
            "PASSWORD": os.environ["DB_PASSWORD"],
            "HOST": os.getenv("DB_HOST", "127.0.0.1"),
            "PORT": os.getenv("DB_PORT", "5432"),
            "CONN_MAX_AGE": DB_CONN_MAX_AGE,
            "CONN_HEALTH_CHECKS": True,
        }
    }
elif DB_ENGINE == "sqlserver":
    sqlserver_options = {
        "driver": os.getenv("DB_DRIVER", "ODBC Driver 18 for SQL Server"),
    }
    extra_params = os.getenv("DB_EXTRA_PARAMS", "").strip()
    if extra_params:
        sqlserver_options["extra_params"] = extra_params

    DATABASES = {
        "default": {
            "ENGINE": "mssql",
            "NAME": os.environ["DB_NAME"],
            "USER": os.environ["DB_USER"],
            "PASSWORD": os.environ["DB_PASSWORD"],
            "HOST": os.environ["DB_HOST"],
            "PORT": os.getenv("DB_PORT", "1433"),
            "OPTIONS": sqlserver_options,
            "CONN_MAX_AGE": DB_CONN_MAX_AGE,
            "CONN_HEALTH_CHECKS": True,
        }
    }
elif DB_ENGINE == "sqlite":
    # Chỉ dành cho test/CI hoặc demo cục bộ. Production dùng PostgreSQL/SQL Server.
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.getenv("DB_NAME", str(BASE_DIR / "db.sqlite3")),
        }
    }
else:
    raise ValueError("DB_ENGINE chỉ được phép là postgresql, sqlserver hoặc sqlite.")


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "vi"
TIME_ZONE = "Asia/Ho_Chi_Minh"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
if WHITENOISE_AVAILABLE:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
        },
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# -----------------------------------------------------------------------------
# Monitoring / collector
# -----------------------------------------------------------------------------
MONITOR_STALE_AFTER_SECONDS = env_int("MONITOR_STALE_AFTER_SECONDS", 15)
COLLECTOR_CONFIG_REFRESH_SECONDS = env_int("COLLECTOR_CONFIG_REFRESH_SECONDS", 5)
COLLECTOR_MAX_WORKERS = env_int("COLLECTOR_MAX_WORKERS", 16)
COLLECTOR_RETENTION_DAYS = env_int("COLLECTOR_RETENTION_DAYS", 90)
MONITOR_REQUIRE_LOGIN = env_bool("MONITOR_REQUIRE_LOGIN", APP_ENV == "production")

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"


# -----------------------------------------------------------------------------
# Optional web security switches. Enable HTTPS flags only when TLS is deployed.
# -----------------------------------------------------------------------------
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", False)
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", False)
SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool(
    "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", False
)
X_FRAME_OPTIONS = "DENY"
