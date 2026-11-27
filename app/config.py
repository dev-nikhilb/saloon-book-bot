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
    meta_config_id: str = ""  # Tech Provider Embedded Signup configuration ID
    meta_access_token: str = ""  # optional fallback WABA token
    meta_webhook_verify_token: str = "changeme"
    webhook_async: bool = False  # set True in production to offload to Celery

    # Google OAuth
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    # Razorpay (booking deposits + SaaS subscriptions)
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    razorpay_plan_solo_monthly: str = ""
    razorpay_plan_solo_annual: str = ""
    razorpay_plan_growth_monthly: str = ""
    razorpay_plan_growth_annual: str = ""
    razorpay_plan_pro_monthly: str = ""
    razorpay_plan_pro_annual: str = ""
    subscription_grace_days: int = 3

    # Gemini (conversation AI)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # Dashboard
    dashboard_url: str = "http://localhost:8000/dashboard/"
    jwt_secret: str = ""  # falls back to encryption_key if empty
    base_domain: str = "localhost:8000"  # tivoria.site in production

    # Free Email OTP (Resend API 3,000/mo free OR Gmail/Brevo SMTP 500/day free)
    resend_api_key: str = ""
    email_from: str = "Tivoria <onboarding@resend.dev>"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""

    # Observability
    sentry_dsn: str = ""
    log_level: str = "INFO"
    environment: str = "development"


settings = Settings()
