from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    import app.models.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_lab_runtime_columns)
        await conn.run_sync(_ensure_schedule_columns)
        await conn.run_sync(_ensure_spot_columns)
        await conn.run_sync(_ensure_lab_type_columns)
        if engine.dialect.name == "postgresql":
            await conn.execute(text("ALTER TYPE labstatus ADD VALUE IF NOT EXISTS 'scheduled'"))
            await conn.execute(text("ALTER TYPE labstatus ADD VALUE IF NOT EXISTS 'stopped'"))
            await conn.execute(text("ALTER TYPE labstatus ADD VALUE IF NOT EXISTS 'resuming'"))
            await conn.execute(text("ALTER TYPE labstatus ADD VALUE IF NOT EXISTS 'interrupted'"))


def _ensure_lab_runtime_columns(sync_conn) -> None:
    columns = {column["name"] for column in inspect(sync_conn).get_columns("labs")}
    dialect = sync_conn.dialect.name
    timestamp_type = "TIMESTAMP WITH TIME ZONE" if dialect == "postgresql" else "DATETIME"
    float_type = "DOUBLE PRECISION" if dialect == "postgresql" else "FLOAT"
    if "last_started_at" not in columns:
        sync_conn.execute(text(f"ALTER TABLE labs ADD COLUMN last_started_at {timestamp_type}"))
    if "accumulated_runtime_seconds" not in columns:
        sync_conn.execute(text(f"ALTER TABLE labs ADD COLUMN accumulated_runtime_seconds {float_type} DEFAULT 0"))


def _ensure_schedule_columns(sync_conn) -> None:
    dialect = sync_conn.dialect.name
    bool_type = "BOOLEAN" if dialect == "postgresql" else "BOOLEAN"
    date_type = "DATE"
    schedule_columns = [
        ("schedule_enabled", f"{bool_type} DEFAULT FALSE"),
        ("schedule_start_date", date_type),
        ("schedule_days", "INTEGER"),
        ("schedule_start_time", "VARCHAR(5)"),
        ("schedule_end_time", "VARCHAR(5)"),
        ("schedule_timezone", "VARCHAR(64) DEFAULT 'Asia/Kolkata'"),
    ]
    for table in ["batches", "labs"]:
        columns = {column["name"] for column in inspect(sync_conn).get_columns(table)}
        for name, definition in schedule_columns:
            if name not in columns:
                sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))


def _ensure_spot_columns(sync_conn) -> None:
    columns = {column["name"] for column in inspect(sync_conn).get_columns("labs")}
    dialect = sync_conn.dialect.name
    timestamp_type = "TIMESTAMP WITH TIME ZONE" if dialect == "postgresql" else "DATETIME"
    float_type = "DOUBLE PRECISION" if dialect == "postgresql" else "FLOAT"
    spot_columns = [
        ("requested_instance_market", "VARCHAR(20) DEFAULT 'on-demand'"),
        ("instance_market", "VARCHAR(20) DEFAULT 'on-demand'"),
        ("on_demand_hourly_cost", float_type),
        ("spot_hourly_cost", float_type),
        ("interrupted_at", timestamp_type),
    ]
    for name, definition in spot_columns:
        if name not in columns:
            sync_conn.execute(text(f"ALTER TABLE labs ADD COLUMN {name} {definition}"))


def _ensure_lab_type_columns(sync_conn) -> None:
    for table in ["batches", "labs"]:
        columns = {column["name"] for column in inspect(sync_conn).get_columns(table)}
        if "lab_type" not in columns:
            sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN lab_type VARCHAR(40) DEFAULT 'windows'"))
    lab_columns = {column["name"] for column in inspect(sync_conn).get_columns("labs")}
    if "claude_profile_id" not in lab_columns:
        sync_conn.execute(text("ALTER TABLE labs ADD COLUMN claude_profile_id VARCHAR(120)"))
