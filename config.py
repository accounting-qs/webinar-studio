from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str

    # Auth
    API_BEARER_TOKEN: str

    # Public contact-counts endpoint key (optional)
    STATS_API_KEY: Optional[str] = None

    # Apify
    APIFY_API_TOKEN: str
    APIFY_ACTOR_ID: Optional[str] = None

    # Cloudflare R2
    R2_ACCOUNT_ID: str
    R2_ACCESS_KEY_ID: str
    R2_SECRET_ACCESS_KEY: str
    R2_BUCKET_NAME: str
    R2_ENDPOINT_URL: str

    # Deepgram
    DEEPGRAM_API_KEY: str

    # Anthropic (Claude)
    ANTHROPIC_API_KEY: str
    CLAUDE_MODEL: str = "claude-sonnet-4-6"

    # Twilio WhatsApp
    TWILIO_ACCOUNT_SID: str
    TWILIO_AUTH_TOKEN: str
    TWILIO_WHATSAPP_FROM: str
    LLOYD_WHATSAPP_NUMBER: str

    # Scheduler
    MONITORING_HOUR: int = 6          # 6 AM UTC daily
    PATTERN_ANALYSIS_DAY: str = "sun" # weekly Sunday

    # Processing limits
    MAX_CONCURRENT_DOWNLOADS: int = 3
    MAX_VIDEO_SIZE_MB: int = 500
    CDN_URL_MAX_AGE_HOURS: int = 6

    # Brain gate
    BRAIN_MINIMUM_PRINCIPLES: int = 5

    # Vision extraction
    ENABLE_VISION_EXTRACTION: bool = True
    VISION_FRAME_SECONDS_DENSE: int = 5   # 1fps for first N seconds (hook)
    VISION_FRAME_INTERVAL_SPARSE: int = 15 # 1 frame per N seconds thereafter

    # Phase 3 (optional)
    META_APP_ID: Optional[str] = None
    META_APP_SECRET: Optional[str] = None
    GHL_API_KEY: Optional[str] = None
    GHL_LOCATION_ID: Optional[str] = None
    GHL_PIPELINE_ID: Optional[str] = None
    GHL_API_BASE_URL: str = "https://services.leadconnectorhq.com"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
