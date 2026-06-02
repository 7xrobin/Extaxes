from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-secret-key")
DEBUG = os.getenv("DEBUG", "True") == "True"
ALLOWED_HOSTS = ["*"]
CSRF_TRUSTED_ORIGINS = ["https://*.ngrok-free.app", "https://*.ngrok.io"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "whitenoise.runserver_nostatic",
    "django.contrib.staticfiles",
    "accounts",
    "portfolio",
    "chat",
    "digest",
    "rag",
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

ROOT_URLCONF = "kyron.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "chat.context_processors.chat_sessions",
            ],
        },
    },
]

WSGI_APPLICATION = "kyron.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Europe/Berlin"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# Django 5 / 4.2+ way to configure WhiteNoise storage
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Embedding model for the tax-source RAG index (rag app)
RAG_EMBEDDING_MODEL = "text-embedding-3-small"

# ── LangSmith observability ───────────────────────────────────────────────────
# Tracing is opt-in. Set LANGSMITH_TRACING=true and LANGSMITH_API_KEY in .env to
# stream graph runs, tool spans and LLM calls to LangSmith. We accept either the
# new LANGSMITH_* names or the legacy LANGCHAIN_* names, and normalise them into
# the LANGCHAIN_* env vars that LangGraph / LangSmith actually read at runtime.
# This must happen before agent.graph is imported (Django loads settings first).
LANGSMITH_API_KEY = os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")
LANGSMITH_PROJECT = os.getenv("LANGSMITH_PROJECT", "kyron-investbuddy")
LANGSMITH_ENDPOINT = os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
_LANGSMITH_TRACING = (
    os.getenv("LANGSMITH_TRACING") or os.getenv("LANGCHAIN_TRACING_V2") or "false"
).strip().lower() == "true"

if _LANGSMITH_TRACING and LANGSMITH_API_KEY:
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = LANGSMITH_API_KEY
    os.environ["LANGCHAIN_PROJECT"] = LANGSMITH_PROJECT
    os.environ["LANGCHAIN_ENDPOINT"] = LANGSMITH_ENDPOINT
    LANGSMITH_TRACING = True
else:
    # Make sure a stale env var can't silently enable tracing without a key.
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    LANGSMITH_TRACING = False

# ── Logging ───────────────────────────────────────────────────────────────────
# Structured, level-controllable logging. Console for dev; a rotating file so the
# agent/graph trail survives across restarts. Tune with DJANGO_LOG_LEVEL.
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

DJANGO_LOG_LEVEL = os.getenv("DJANGO_LOG_LEVEL", "INFO").upper()
APP_LOG_LEVEL = os.getenv("APP_LOG_LEVEL", "DEBUG" if DEBUG else "INFO").upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname:<8} {name}:{lineno} {funcName}() | {message}",
            "style": "{",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "concise": {
            "format": "{levelname:<8} {name} | {message}",
            "style": "{",
        },
    },
    "filters": {
        "require_debug_true": {"()": "django.utils.log.RequireDebugTrue"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "concise",
            "level": "DEBUG",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOGS_DIR / "kyron.log"),
            "maxBytes": 5 * 1024 * 1024,   # 5 MB
            "backupCount": 5,
            "formatter": "verbose",
            "level": "DEBUG",
            "encoding": "utf-8",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "WARNING",
    },
    "loggers": {
        "django": {
            "handlers": ["console", "file"],
            "level": DJANGO_LOG_LEVEL,
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console", "file"],
            "level": "WARNING",
            "propagate": False,
        },
        # Application loggers — the agent graph, tools and Django apps.
        **{
            name: {
                "handlers": ["console", "file"],
                "level": APP_LOG_LEVEL,
                "propagate": False,
            }
            for name in ("agent", "rag", "portfolio", "chat", "digest", "accounts")
        },
    },
}

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/chat/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

# LangGraph memory database — separate from Django DB
LANGGRAPH_DB_PATH = str(BASE_DIR / "langgraph_memory.sqlite3")
