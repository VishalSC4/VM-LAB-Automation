export type Dashboard = {
  batches: number;
  active_labs: number;
  provisioning_labs: number;
  terminated_labs: number;
  failed_labs: number;
  stopped_labs: number;
  budget_exceeded_labs: number;
  estimated_running_hourly_cost: number;
  estimated_total_spend: number;
  healthy_labs: number;
  attention_labs: number;
};

export type Lab = {
  id: string;
  batch_id: string;
  owner_label: string;
  status: string;
  aws_region: string;
  instance_type: string;
  windows_ami: string;
  lab_type: string;
  claude_profile_id: string | null;
  requested_instance_market: string;
  instance_market: string;
  ec2_instance_id: string | null;
  private_ip: string | null;
  access_url: string | null;
  username: string;
  budget_limit: number;
  hourly_cost: number;
  on_demand_hourly_cost: number | null;
  spot_hourly_cost: number | null;
  expiry_time: string;
  idle_timeout_minutes: number;
  schedule_enabled: boolean;
  schedule_start_date: string | null;
  schedule_days: number | null;
  schedule_start_time: string | null;
  schedule_end_time: string | null;
  schedule_timezone: string;
  last_seen_at: string | null;
  last_started_at: string | null;
  accumulated_runtime_seconds: number;
  current_runtime_seconds: number;
  current_spend: number;
  budget_percent: number;
  created_at: string;
  terminated_at: string | null;
  interrupted_at: string | null;
};

export type AuditLog = {
  id: string;
  actor: string;
  action: string;
  resource_id: string | null;
  message: string;
  created_at: string;
};

export type StudentLab = {
  lab: Lab;
  access_url: string | null;
  username: string;
  password: string;
  progress: string[];
};

export type LabCredential = {
  lab_id: string;
  owner_label: string;
  status: string;
  url: string | null;
  username: string;
  password: string;
  expires: string;
};

export type LabCredentialsExport = {
  count: number;
  generated_at: string;
  credentials: LabCredential[];
  share_text: string;
};

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api";

export const AUTH_EXPIRED_EVENT = "cloudlab:auth-expired";

function errorMessageFromBody(body: string, fallback: string): string {
  if (!body) return fallback;
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
    if (Array.isArray(parsed.detail)) return parsed.detail.map((item) => item?.msg ?? String(item)).join("; ");
  } catch {
    // The API can still return plain text for proxy/runtime errors.
  }
  return body;
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = typeof window !== "undefined" ? localStorage.getItem("cloudlab_token") : null;
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const body = await response.text();
    const message = errorMessageFromBody(body, response.statusText);
    if (response.status === 401 && typeof window !== "undefined") {
      localStorage.removeItem("cloudlab_token");
      window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
      throw new Error(message === "Invalid email or password" ? message : "Session expired. Please sign in again.");
    }
    throw new Error(message);
  }
  return response.json();
}
