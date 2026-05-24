from datetime import date, datetime, timezone
from pydantic import BaseModel, Field, model_validator

from app.models.models import LabStatus


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginIn(BaseModel):
    email: str
    password: str


class BatchCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    user_count: int = Field(ge=1, le=500)
    duration_hours: float = Field(gt=0, le=720)
    budget_per_vm: float = Field(gt=0)
    aws_region: str
    instance_type: str
    windows_ami: str
    idle_timeout_minutes: int = Field(default=60, ge=5, le=1440)
    schedule_enabled: bool = False
    schedule_start_date: date | None = None
    schedule_days: int | None = Field(default=None, ge=1, le=365)
    schedule_start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    schedule_end_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    schedule_timezone: str = "Asia/Kolkata"

    @model_validator(mode="after")
    def validate_schedule(self):
        if not self.schedule_enabled:
            return self
        required = [
            self.schedule_start_date,
            self.schedule_days,
            self.schedule_start_time,
            self.schedule_end_time,
        ]
        if any(value is None for value in required):
            raise ValueError("Schedule date, days, start time, and end time are required")
        if self.schedule_start_time >= self.schedule_end_time:
            raise ValueError("Schedule end time must be after start time")
        return self


class LabBudgetCreditIn(BaseModel):
    amount: float = Field(gt=0, le=10000)


class BatchOut(BaseModel):
    id: str
    name: str
    user_count: int
    duration_hours: float
    budget_per_vm: float
    aws_region: str
    instance_type: str
    windows_ami: str
    idle_timeout_minutes: int
    schedule_enabled: bool = False
    schedule_start_date: date | None = None
    schedule_days: int | None = None
    schedule_start_time: str | None = None
    schedule_end_time: str | None = None
    schedule_timezone: str = "Asia/Kolkata"
    created_at: datetime

    class Config:
        from_attributes = True


class LabOut(BaseModel):
    id: str
    batch_id: str
    owner_label: str
    status: LabStatus
    aws_region: str
    instance_type: str
    requested_instance_market: str = "on-demand"
    instance_market: str = "on-demand"
    ec2_instance_id: str | None
    private_ip: str | None
    access_url: str | None
    username: str
    budget_limit: float
    hourly_cost: float
    on_demand_hourly_cost: float | None = None
    spot_hourly_cost: float | None = None
    expiry_time: datetime
    idle_timeout_minutes: int
    schedule_enabled: bool = False
    schedule_start_date: date | None = None
    schedule_days: int | None = None
    schedule_start_time: str | None = None
    schedule_end_time: str | None = None
    schedule_timezone: str = "Asia/Kolkata"
    last_seen_at: datetime | None
    last_started_at: datetime | None
    accumulated_runtime_seconds: float
    current_runtime_seconds: float = 0
    current_spend: float = 0
    budget_percent: float = 0
    created_at: datetime
    terminated_at: datetime | None
    interrupted_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def attach_live_cost_fields(cls, value):
        if isinstance(value, dict):
            return value

        accumulated = getattr(value, "accumulated_runtime_seconds", 0) or 0
        last_started_at = getattr(value, "last_started_at", None)
        status = getattr(value, "status", None)
        hourly_cost = getattr(value, "hourly_cost", 0) or 0
        budget_limit = getattr(value, "budget_limit", 0) or 0
        runtime_seconds = accumulated

        if status == LabStatus.running and last_started_at:
            if last_started_at.tzinfo is None:
                last_started_at = last_started_at.replace(tzinfo=timezone.utc)
            runtime_seconds += max((datetime.now(timezone.utc) - last_started_at).total_seconds(), 0)

        current_spend = (runtime_seconds / 3600) * hourly_cost
        budget_percent = min(max((current_spend / budget_limit) * 100, 0), 100) if budget_limit > 0 else 0

        data = {
            "id": value.id,
            "batch_id": value.batch_id,
            "owner_label": value.owner_label,
            "status": value.status,
            "aws_region": value.aws_region,
            "instance_type": value.instance_type,
            "requested_instance_market": value.requested_instance_market,
            "instance_market": value.instance_market,
            "ec2_instance_id": value.ec2_instance_id,
            "private_ip": value.private_ip,
            "access_url": value.access_url,
            "username": value.username,
            "budget_limit": value.budget_limit,
            "hourly_cost": value.hourly_cost,
            "on_demand_hourly_cost": value.on_demand_hourly_cost,
            "spot_hourly_cost": value.spot_hourly_cost,
            "expiry_time": value.expiry_time,
            "idle_timeout_minutes": value.idle_timeout_minutes,
            "schedule_enabled": value.schedule_enabled,
            "schedule_start_date": value.schedule_start_date,
            "schedule_days": value.schedule_days,
            "schedule_start_time": value.schedule_start_time,
            "schedule_end_time": value.schedule_end_time,
            "schedule_timezone": value.schedule_timezone,
            "last_seen_at": value.last_seen_at,
            "last_started_at": value.last_started_at,
            "accumulated_runtime_seconds": value.accumulated_runtime_seconds,
            "current_runtime_seconds": runtime_seconds,
            "current_spend": current_spend,
            "budget_percent": budget_percent,
            "created_at": value.created_at,
            "terminated_at": value.terminated_at,
            "interrupted_at": value.interrupted_at,
        }
        return data

    class Config:
        from_attributes = True


class DashboardOut(BaseModel):
    batches: int
    active_labs: int
    provisioning_labs: int
    terminated_labs: int
    failed_labs: int
    stopped_labs: int = 0
    budget_exceeded_labs: int = 0
    estimated_running_hourly_cost: float = 0
    estimated_total_spend: float = 0
    healthy_labs: int = 0
    attention_labs: int = 0


class LogOut(BaseModel):
    id: str
    actor: str
    action: str
    resource_id: str | None
    message: str
    created_at: datetime

    class Config:
        from_attributes = True


class LabExtendIn(BaseModel):
    hours: float = Field(gt=0, le=168)


class LabCredentialOut(BaseModel):
    lab_id: str
    owner_label: str
    status: LabStatus
    url: str | None
    username: str
    password: str
    expires: datetime


class LabCredentialsExportOut(BaseModel):
    count: int
    generated_at: datetime
    credentials: list[LabCredentialOut]
    share_text: str


class StudentLoginIn(BaseModel):
    username: str
    password: str


class StudentLabOut(BaseModel):
    lab: LabOut
    access_url: str | None
    username: str
    password: str
    progress: list[str]
