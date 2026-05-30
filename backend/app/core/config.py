from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Cloud Lab Platform"
    environment: str = "development"
    database_url: str = "sqlite+aiosqlite:///./cloudlabs.db"
    jwt_secret: str = Field(default="change-me-in-production", min_length=16)
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 720
    admin_bootstrap_email: str = "admin@example.com"
    admin_bootstrap_password: str = "ChangeMe123!"

    aws_region: str = "ap-south-1"
    default_vpc_id: str | None = None
    lab_subnet_id: str | None = None
    lab_subnet_ids: str | None = None
    lab_security_group_id: str | None = None
    lab_key_name: str | None = None
    lab_iam_instance_profile: str | None = None
    lab_instance_market: str = "on-demand"
    lab_spot_enabled: bool = False
    lab_spot_max_price: str | None = None
    lab_spot_fallback_to_on_demand: bool = True
    lab_spot_instance_types: str | None = None
    lab_root_volume_size_gb: int = Field(default=64, ge=30, le=1024)
    windows_admin_user: str = "Administrator"
    claude_profile_bucket: str | None = None
    claude_profile_prefix: str = "claude-profiles/"
    claude_profile_ids: str = "siddharthyadav63_ymail_com"
    claude_profile_archive_suffix: str = ".zip"
    claude_account_email: str = "siddharthyadav63@ymail.com"
    claude_require_profile_archive: bool = True
    claude_require_fast_launch: bool = True
    claude_fast_launch_min_target_count: int = Field(default=6, ge=1, le=100)
    lab_provision_stagger_seconds: int = Field(default=15, ge=0, le=120)

    guacamole_base_url: str = "http://guacamole:8080/guacamole"
    guacamole_public_url: str = ""
    guacamole_admin_user: str = "guacadmin"
    guacamole_admin_password: str = "guacadmin"
    guacamole_datasource: str = "postgresql"

    cleanup_interval_seconds: int = 60
    pricing_cache_ttl_seconds: int = 21600


@lru_cache
def get_settings() -> Settings:
    return Settings()
