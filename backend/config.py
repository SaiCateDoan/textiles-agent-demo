from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_vision_model: str = ""
    max_multimodal_images: int = 60
    textile_data_dir: Path = ROOT / "data"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_cache_dir: Path | None = None
    embedding_cache_version: str = "1"
    onnx_provider: str = "CPUExecutionProvider"
    max_upload_mb: int = 30
    chat_max_rounds: int = Field(default=8, ge=1, le=24)
    chat_timeout_seconds: float = Field(default=300, gt=0)
    ingestion_enabled: bool = True
    ingestion_workers: int = Field(default=2, ge=1, le=8)
    ingestion_queue_limit: int = Field(default=10000, ge=1)
    ingestion_max_attempts: int = Field(default=4, ge=1, le=20)
    ingestion_lease_seconds: float = Field(default=90, ge=3)
    ingestion_poll_seconds: float = Field(default=.25, ge=.02)
    parse_timeout_seconds: float = Field(default=300, gt=0)
    vision_timeout_seconds: float = Field(default=100, gt=0)
    vision_concurrency: int = Field(default=2, ge=1, le=8)
    vision_requests_per_minute: int = Field(default=30, ge=1)
    embedding_batch_size: int = Field(default=32, ge=1, le=128)
    vector_write_batch_size: int = Field(default=256, ge=1, le=2048)
    upload_concurrency: int = Field(default=4, ge=1, le=32)
    minimum_free_disk_mb: int = Field(default=512, ge=0)
    cache_max_mb: int = Field(default=512, ge=1)
    checkpoint_retention_hours: int = Field(default=24, ge=1)
    failed_checkpoint_retention_hours: int = Field(default=168, ge=1)

    @field_validator("embedding_cache_dir",mode="before")
    @classmethod
    def optional_model_path(cls, value):
        return None if value == "" else value


settings = Settings()
