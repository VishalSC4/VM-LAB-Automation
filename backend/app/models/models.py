import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def uuid_str() -> str:
    return str(uuid.uuid4())


class LabStatus(str, enum.Enum):
    scheduled = "scheduled"
    provisioning = "provisioning"
    running = "running"
    stopped = "stopped"
    resuming = "resuming"
    failed = "failed"
    expired = "expired"
    budget_exceeded = "budget_exceeded"
    interrupted = "interrupted"
    terminating = "terminating"
    terminated = "terminated"


class CleanupReason(str, enum.Enum):
    expiry = "expiry"
    budget = "budget"
    idle = "idle"
    force = "force"
    orphan = "orphan"


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(160), index=True)
    user_count: Mapped[int] = mapped_column(Integer)
    duration_hours: Mapped[float] = mapped_column(Float)
    budget_per_vm: Mapped[float] = mapped_column(Float)
    aws_region: Mapped[str] = mapped_column(String(64))
    instance_type: Mapped[str] = mapped_column(String(64))
    windows_ami: Mapped[str] = mapped_column(String(128))
    lab_type: Mapped[str] = mapped_column(String(40), default="windows", index=True)
    idle_timeout_minutes: Mapped[int] = mapped_column(Integer, default=60)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    schedule_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    schedule_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule_start_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    schedule_end_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    schedule_timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    created_by: Mapped[str | None] = mapped_column(ForeignKey("admins.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    labs: Mapped[list["Lab"]] = relationship(back_populates="batch")


class Lab(Base):
    __tablename__ = "labs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"), index=True)
    owner_label: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[LabStatus] = mapped_column(Enum(LabStatus), default=LabStatus.provisioning, index=True)
    aws_region: Mapped[str] = mapped_column(String(64))
    instance_type: Mapped[str] = mapped_column(String(64))
    windows_ami: Mapped[str] = mapped_column(String(128))
    lab_type: Mapped[str] = mapped_column(String(40), default="windows", index=True)
    claude_profile_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    requested_instance_market: Mapped[str] = mapped_column(String(20), default="on-demand")
    instance_market: Mapped[str] = mapped_column(String(20), default="on-demand")
    on_demand_hourly_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    spot_hourly_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    ec2_instance_id: Mapped[str | None] = mapped_column(String(80), index=True)
    private_ip: Mapped[str | None] = mapped_column(String(64))
    public_ip: Mapped[str | None] = mapped_column(String(64))
    guacamole_connection_id: Mapped[str | None] = mapped_column(String(80))
    access_url: Mapped[str | None] = mapped_column(String(512))
    username: Mapped[str] = mapped_column(String(128))
    password_secret_ref: Mapped[str] = mapped_column(String(512))
    password_ciphertext: Mapped[str] = mapped_column(Text)
    budget_limit: Mapped[float] = mapped_column(Float)
    hourly_cost: Mapped[float] = mapped_column(Float)
    expiry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    idle_timeout_minutes: Mapped[int] = mapped_column(Integer)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    schedule_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    schedule_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule_start_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    schedule_end_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    schedule_timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accumulated_runtime_seconds: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    interrupted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    batch: Mapped[Batch] = relationship(back_populates="labs")


class CleanupJob(Base):
    __tablename__ = "cleanup_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    lab_id: Mapped[str] = mapped_column(ForeignKey("labs.id"), index=True)
    reason: Mapped[CleanupReason] = mapped_column(Enum(CleanupReason))
    status: Mapped[str] = mapped_column(String(40), default="queued")
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uuid_str)
    actor: Mapped[str] = mapped_column(String(160))
    action: Mapped[str] = mapped_column(String(160), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(160), index=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
