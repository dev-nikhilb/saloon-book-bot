from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Database
    database_url: str = "postgresql+psycopg://booking:booking@localhost:5432/booking"

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"

    # Encryption (for WABA tokens, OAuth tokens)
    encryption_key: str = ""

    # Meta WhatsApp Business API
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_access_token: str = ""  # optional fallback WABA token
    meta_webhook_verify_token: str = "changeme"
    webhook_async: bool = False  # set True in production to offload to Celery

    # Google OAuth
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    # Razorpay
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # Gemini (conversation AI)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # Dashboard
    dashboard_url: str = "http://localhost:8000/dashboard/"
    jwt_secret: str = ""  # falls back to encryption_key if empty
    base_domain: str = "localhost:8000"  # bookbot.com in production

    # Observability
    sentry_dsn: str = ""
    log_level: str = "INFO"
    environment: str = "development"


settings = Settings()
