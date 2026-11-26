"""
Meltice GPS Tracker prototype settings. Everything deployment-specific comes from environment
variables (see .env.example). Defaults are safe for a local trial with SQLite.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return str(env(name, default)).lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [x.strip() for x in str(env(name, default)).split(",") if x.strip()]


# Optional .env file next to manage.py (simple KEY=VALUE lines)
_envfile = Path(env("FLEET_ENV_FILE", BASE_DIR / ".env"))
if _envfile.exists():
    for line in _envfile.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SECRET_KEY = env("FLEET_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env_bool("FLEET_DEBUG", True)
ALLOWED_HOSTS = env_list("FLEET_ALLOWED_HOSTS", "127.0.0.1,localhost")
CSRF_TRUSTED_ORIGINS = env_list("FLEET_CSRF_TRUSTED_ORIGINS", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "tracking",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "fleet_project.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "tracking.views.fleet_context",
    ]},
}]
WSGI_APPLICATION = "fleet_project.wsgi.application"

# ---------------------------------------------------------------------------
# Database: sqlite (default) | mssql | postgres | mysql
# ---------------------------------------------------------------------------
DB_ENGINE = env("FLEET_DB_ENGINE", "sqlite").lower()
if DB_ENGINE == "mssql":
    DATABASES = {"default": {
        "ENGINE": "mssql",
        "NAME": env("FLEET_DB_NAME", "meltice_gps_tracker"),
        "USER": env("FLEET_DB_USER", ""),
        "PASSWORD": env("FLEET_DB_PASSWORD", ""),
        "HOST": env("FLEET_DB_HOST", "127.0.0.1"),
        "PORT": env("FLEET_DB_PORT", "1433"),
        "OPTIONS": {"driver": env("FLEET_DB_ODBC_DRIVER", "ODBC Driver 18 for SQL Server"),
                    "extra_params": env("FLEET_DB_EXTRA", "TrustServerCertificate=yes")},
    }}
elif DB_ENGINE in ("postgres", "postgresql"):
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("FLEET_DB_NAME", "meltice_gps_tracker"),
        "USER": env("FLEET_DB_USER", ""),
        "PASSWORD": env("FLEET_DB_PASSWORD", ""),
        "HOST": env("FLEET_DB_HOST", "127.0.0.1"),
        "PORT": env("FLEET_DB_PORT", "5432"),
    }}
elif DB_ENGINE == "mysql":
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": env("FLEET_DB_NAME", "meltice_gps_tracker"),
        "USER": env("FLEET_DB_USER", ""),
        "PASSWORD": env("FLEET_DB_PASSWORD", ""),
        "HOST": env("FLEET_DB_HOST", "127.0.0.1"),
        "PORT": env("FLEET_DB_PORT", "3306"),
        "OPTIONS": {"charset": "utf8mb4"},
    }}
else:
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": env("FLEET_SQLITE_PATH", str(BASE_DIR / "data" / "fleet.sqlite3")),
        # web + receiver are separate processes: WAL + busy timeout avoid "database is locked"
        "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE",
                    "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;"},
    }}
    Path(DATABASES["default"]["NAME"]).parent.mkdir(parents=True, exist_ok=True)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("FLEET_DISPLAY_TZ", "Asia/Dubai")   # UTC stored, UAE time displayed
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Behind Nginx with HTTPS (Hostinger)
if env_bool("FLEET_BEHIND_HTTPS_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 3600
SESSION_COOKIE_AGE = 60 * 60 * 12

# ---------------------------------------------------------------------------
# Fleet / receiver
# ---------------------------------------------------------------------------
FLEET = {
    "SITE_NAME": env("FLEET_SITE_NAME", "Meltice GPS Tracker"),
    "ENVIRONMENT": env("FLEET_ENVIRONMENT_LABEL", "Local"),
    "RECEIVER_BIND_HOST": env("FLEET_RECEIVER_HOST", "0.0.0.0"),
    "RECEIVER_PORT": int(env("FLEET_RECEIVER_PORT", "5027")),
    # host/port the web self-test connects to (inside docker this is the receiver service name)
    "SELFTEST_HOST": env("FLEET_SELFTEST_HOST", "127.0.0.1"),
    "SELFTEST_PORT": int(env("FLEET_SELFTEST_PORT", env("FLEET_RECEIVER_PORT", "5027"))),
    "SELFTEST_IMEI": env("FLEET_SELFTEST_IMEI", "356307042441013"),
    # what the device must be configured with (shown on Settings page)
    "PUBLIC_GPS_HOST": env("FLEET_PUBLIC_GPS_HOST", "your-public-ip-or-tunnel"),
    "PUBLIC_GPS_PORT": int(env("FLEET_PUBLIC_GPS_PORT", env("FLEET_RECEIVER_PORT", "5027"))),
    "MAX_FRAME_BYTES": int(env("FLEET_MAX_FRAME_BYTES", "65536")),
    "RAW_HEX_STORE_LIMIT": int(env("FLEET_RAW_HEX_STORE_LIMIT", "16384")),
    "MAX_CONNECTIONS": int(env("FLEET_MAX_CONNECTIONS", "50")),
    "CONNECTS_PER_MINUTE_PER_IP": int(env("FLEET_CONNECTS_PER_MINUTE_PER_IP", "30")),
    "HANDSHAKE_TIMEOUT": float(env("FLEET_HANDSHAKE_TIMEOUT", "30")),
    "READ_TIMEOUT": float(env("FLEET_READ_TIMEOUT", "360")),
    "STALE_AFTER_SECONDS": int(env("FLEET_STALE_AFTER_SECONDS", "900")),
    "OFFLINE_AFTER_SECONDS": int(env("FLEET_OFFLINE_AFTER_SECONDS", "3600")),
    "POLL_SECONDS": int(env("FLEET_POLL_SECONDS", "5")),
    "FAULT_INJECT_IMEIS": set(env_list("FLEET_FAULT_INJECT_IMEIS", "")),
}

LOG_DIR = Path(env("FLEET_LOG_DIR", BASE_DIR / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"std": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "std"},
        # one file per process role (web / receiver) - rotating handlers must not be shared between processes
        "file": {"class": "logging.handlers.RotatingFileHandler",
                 "filename": str(LOG_DIR / f"{env('FLEET_PROCESS', 'fleet')}.log"),
                 "maxBytes": 5 * 1024 * 1024, "backupCount": 5, "formatter": "std"},
    },
    "loggers": {"fleet": {"handlers": ["console", "file"], "level": env("FLEET_LOG_LEVEL", "INFO")}},
}
