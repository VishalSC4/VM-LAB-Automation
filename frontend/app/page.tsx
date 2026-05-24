"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertCircle,
  CalendarDays,
  CheckCircle2,
  Clock,
  Copy,
  Cpu,
  DollarSign,
  Download,
  ExternalLink,
  KeyRound,
  LogOut,
  Play,
  RefreshCw,
  Search,
  Server,
  Shield,
  Square,
  Trash2,
  Users,
  WalletCards,
  Zap,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { api, AUTH_EXPIRED_EVENT, AuditLog, Dashboard, Lab, LabCredentialsExport, StudentLab } from "@/lib/api";

const defaults = {
  name: "dev-lab",
  user_count: 1,
  duration_hours: 4,
  budget_per_vm: 10,
  aws_region: "ap-south-1",
  instance_type: "c6a.xlarge",
  windows_ami: "ami-079ba093634ca5405",
  idle_timeout_minutes: 60,
  schedule_enabled: false,
  schedule_start_date: new Date().toISOString().slice(0, 10),
  schedule_days: 2,
  schedule_start_time: "09:00",
  schedule_end_time: "17:00",
  schedule_timezone: "Asia/Kolkata",
};

function labAccessHref(accessUrl: string): string {
  try {
    const parsed = new URL(accessUrl, window.location.origin);
    if (parsed.pathname.startsWith("/session/") || parsed.pathname.startsWith("/guacamole/")) {
      return `${parsed.pathname}${parsed.search}${parsed.hash}`;
    }
  } catch {
    return accessUrl;
  }
  return accessUrl;
}

function runtimeSeconds(lab: Lab, nowMs: number): number {
  const accumulated = lab.accumulated_runtime_seconds ?? 0;
  const liveSeconds = lab.status === "running" && lab.last_started_at ? Math.max((nowMs - new Date(lab.last_started_at).getTime()) / 1000, 0) : 0;
  return accumulated + liveSeconds;
}

function currentSpend(lab: Lab, nowMs: number): number {
  return (runtimeSeconds(lab, nowMs) / 3600) * lab.hourly_cost;
}

function runtimeLabel(seconds: number): string {
  const safeSeconds = Math.max(seconds, 0);
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const secs = Math.floor(safeSeconds % 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

function percent(value: number, total: number): number {
  if (!Number.isFinite(value) || !Number.isFinite(total) || total <= 0) return 0;
  return Math.min(Math.max((value / total) * 100, 0), 100);
}

function remainingTime(lab: Lab): string {
  const remainingMs = new Date(lab.expiry_time).getTime() - Date.now();
  if (remainingMs <= 0) return "Expired";
  const hours = Math.floor(remainingMs / 3600000);
  const minutes = Math.floor((remainingMs % 3600000) / 60000);
  if (hours >= 24) return `${Math.floor(hours / 24)}d ${hours % 24}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function elapsedPercent(lab: Lab): number {
  const created = new Date(lab.created_at).getTime();
  const expires = new Date(lab.expiry_time).getTime();
  return percent(Date.now() - created, expires - created);
}

function scheduleLabel(lab: Lab): string {
  if (!lab.schedule_enabled) return "Any time";
  return `${lab.schedule_start_time}-${lab.schedule_end_time} x ${lab.schedule_days}d`;
}

function cardTone(status: string): string {
  if (status === "running") return "lab-card lab-card-running";
  if (status === "provisioning" || status === "resuming") return "lab-card lab-card-warm";
  if (status === "failed" || status === "budget_exceeded" || status === "interrupted") return "lab-card lab-card-danger";
  if (status === "terminated" || status === "expired") return "lab-card lab-card-muted";
  return "lab-card";
}

function statusTone(status: string): string {
  if (status === "running") return "status-pill status-running";
  if (status === "provisioning" || status === "resuming") return "status-pill status-warm";
  if (status === "failed" || status === "budget_exceeded" || status === "interrupted") return "status-pill status-danger";
  if (status === "terminated") return "status-pill status-muted";
  return "status-pill";
}

function progressSteps(lab: Lab): string[] {
  const steps = [
    lab.ec2_instance_id ? "EC2 ready" : "Creating EC2",
    lab.private_ip ? "Network ready" : "Waiting network",
    lab.access_url ? "Access ready" : "Preparing access",
  ];
  if (lab.status === "running") steps.push("Running");
  if (lab.status === "stopped") steps.push("Stopped");
  if (lab.status === "failed") steps.push("Failed");
  if (lab.status === "interrupted") steps.push("Spot ended");
  return steps;
}

function canExtendLab(status: string): boolean {
  return !["terminated", "terminating"].includes(status);
}

function viewMatchesLab(view: string, lab: Lab): boolean {
  if (view === "all") return true;
  if (view === "attention") return ["failed", "budget_exceeded", "expired", "interrupted"].includes(lab.status);
  if (view === "provisioning") return ["provisioning", "resuming"].includes(lab.status);
  return lab.status === view;
}

export default function Home() {
  const [token, setToken] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [form, setForm] = useState(defaults);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [labs, setLabs] = useState<Lab[]>([]);
  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [creditInputs, setCreditInputs] = useState<Record<string, string>>({});
  const [extendInputs, setExtendInputs] = useState<Record<string, string>>({});
  const [credentialExportStatus, setCredentialExportStatus] = useState("");
  const [studentUsername, setStudentUsername] = useState("");
  const [studentPassword, setStudentPassword] = useState("");
  const [studentLab, setStudentLab] = useState<StudentLab | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [adminView, setAdminView] = useState("labs");
  const [labQuery, setLabQuery] = useState("");
  const [labFilter, setLabFilter] = useState("all");
  const [nowMs, setNowMs] = useState(Date.now());

  const activeCost = useMemo(
    () => labs.filter((lab) => lab.status === "running").reduce((sum, lab) => sum + lab.hourly_cost, 0),
    [labs]
  );

  const liveTotalSpend = useMemo(
    () => labs.reduce((sum, lab) => sum + currentSpend(lab, nowMs), 0),
    [labs, nowMs]
  );

  const filteredLabs = useMemo(() => {
    const normalizedQuery = labQuery.trim().toLowerCase();
    return labs.filter((lab) => {
      const matchesFilter = viewMatchesLab(labFilter, lab);
      const matchesQuery =
        !normalizedQuery ||
        [lab.owner_label, lab.username, lab.instance_type, lab.instance_market, lab.aws_region, lab.ec2_instance_id ?? "", lab.private_ip ?? ""]
          .join(" ")
          .toLowerCase()
          .includes(normalizedQuery);
      return matchesFilter && matchesQuery;
    });
  }, [labs, labFilter, labQuery]);

  async function login() {
    setBusy(true);
    setError("");
    try {
      const result = await api<{ access_token: string }>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      localStorage.setItem("cloudlab_token", result.access_token);
      setToken(result.access_token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }

  async function refresh() {
    setRefreshing(true);
    setError("");
    try {
      const [nextDashboard, nextLabs, nextLogs] = await Promise.all([
        api<Dashboard>("/dashboard"),
        api<Lab[]>("/labs"),
        api<AuditLog[]>("/logs"),
      ]);
      setDashboard(nextDashboard);
      setLabs(nextLabs);
      setLogs(nextLogs);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Refresh failed");
    } finally {
      setRefreshing(false);
    }
  }

  async function createBatch() {
    setBusy(true);
    setError("");
    try {
      const payload = {
        ...form,
        schedule_start_date: form.schedule_enabled ? form.schedule_start_date : null,
        schedule_days: form.schedule_enabled ? form.schedule_days : null,
        schedule_start_time: form.schedule_enabled ? form.schedule_start_time : null,
        schedule_end_time: form.schedule_enabled ? form.schedule_end_time : null,
      };
      await api("/batches", { method: "POST", body: JSON.stringify(payload) });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Batch creation failed");
    } finally {
      setBusy(false);
    }
  }

  async function terminate(id: string) {
    await api(`/labs/${id}/terminate`, { method: "POST" });
    await refresh();
  }

  async function startLab(id: string) {
    setBusy(true);
    setError("");
    try {
      await api(`/labs/${id}/resume`, { method: "POST" });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Start failed");
    } finally {
      setBusy(false);
    }
  }

  async function stopLab(id: string) {
    setBusy(true);
    setError("");
    try {
      await api(`/labs/${id}/stop`, { method: "POST" });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Stop failed");
    } finally {
      setBusy(false);
    }
  }

  async function extendLab(id: string) {
    const hours = Number(extendInputs[id] || 1);
    if (!Number.isFinite(hours) || hours <= 0) {
      setError("Enter valid extension hours");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api(`/labs/${id}/extend`, { method: "POST", body: JSON.stringify({ hours }) });
      setExtendInputs({ ...extendInputs, [id]: "" });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Extend failed");
    } finally {
      setBusy(false);
    }
  }

  async function addBudgetCredit(id: string) {
    const amount = Number(creditInputs[id]);
    if (!Number.isFinite(amount) || amount <= 0) {
      setError("Enter a valid credit amount");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api(`/labs/${id}/budget-credit`, {
        method: "POST",
        body: JSON.stringify({ amount }),
      });
      setCreditInputs({ ...creditInputs, [id]: "" });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Credit update failed");
    } finally {
      setBusy(false);
    }
  }

  async function copyCredentials(id: string) {
    const result = await api<{ url: string | null; username: string; password: string; expires_at: string }>(`/labs/${id}/credentials`);
    const url = result.url ? labAccessHref(result.url) : null;
    const text = `URL: ${url}\nUsername: ${result.username}\nPassword: ${result.password}\nExpires: ${result.expires_at}`;
    await navigator.clipboard.writeText(text);
    setCredentials({ ...credentials, [id]: "Copied" });
  }

  async function recentCredentialsText(): Promise<LabCredentialsExport> {
    return api<LabCredentialsExport>("/labs/recent-credentials?limit=100");
  }

  function excelCell(value: string | null | undefined): string {
    const normalized = value ?? "";
    return `"${normalized.replaceAll('"', '""')}"`;
  }

  function credentialsCsv(result: LabCredentialsExport): string {
    const rows = [
      ["Lab", "Status", "URL", "Username", "Password", "Expires"],
      ...result.credentials.map((credential) => [
        credential.owner_label,
        credential.status,
        credential.url ?? "Access pending",
        credential.username,
        credential.password,
        credential.expires,
      ]),
    ];
    return rows.map((row) => row.map(excelCell).join(",")).join("\r\n");
  }

  async function copyRecentCredentials() {
    setBusy(true);
    setError("");
    setCredentialExportStatus("");
    try {
      const result = await recentCredentialsText();
      if (!result.count) {
        setCredentialExportStatus("No recent lab credentials found");
        return;
      }
      await navigator.clipboard.writeText(result.share_text);
      setCredentialExportStatus(`Copied ${result.count} lab credential set${result.count === 1 ? "" : "s"}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Credential copy failed");
    } finally {
      setBusy(false);
    }
  }

  async function downloadRecentCredentialsExcel() {
    setBusy(true);
    setError("");
    setCredentialExportStatus("");
    try {
      const result = await recentCredentialsText();
      if (!result.count) {
        setCredentialExportStatus("No recent lab credentials found");
        return;
      }
      const blob = new Blob(["\ufeff", credentialsCsv(result)], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `cloud-lab-credentials-${new Date().toISOString().slice(0, 10)}.csv`;
      link.click();
      URL.revokeObjectURL(url);
      setCredentialExportStatus(`Downloaded ${result.count} lab credential set${result.count === 1 ? "" : "s"} for Excel`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Excel download failed");
    } finally {
      setBusy(false);
    }
  }

  async function loginStudent() {
    setBusy(true);
    setError("");
    try {
      const result = await api<StudentLab>("/student/login", {
        method: "POST",
        body: JSON.stringify({ username: studentUsername, password: studentPassword }),
      });
      setStudentLab(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Student login failed");
    } finally {
      setBusy(false);
    }
  }

  async function copyStudentCredentials() {
    if (!studentLab) return;
    const text = `URL: ${studentLab.access_url ? labAccessHref(studentLab.access_url) : ""}\nUsername: ${studentLab.username}\nPassword: ${studentLab.password}`;
    await navigator.clipboard.writeText(text);
  }

  useEffect(() => {
    const saved = localStorage.getItem("cloudlab_token");
    if (saved) setToken(saved);
  }, []);

  useEffect(() => {
    function expireSession() {
      setToken(null);
      setDashboard(null);
      setLabs([]);
      setLogs([]);
      setError("Session expired. Please sign in again.");
    }

    window.addEventListener(AUTH_EXPIRED_EVENT, expireSession);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, expireSession);
  }, []);

  useEffect(() => {
    if (!token) return;
    refresh();
    const timer = setInterval(refresh, 15000);
    return () => clearInterval(timer);
  }, [token]);

  useEffect(() => {
    const timer = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const metricItems: Array<{ label: string; value: number; Icon: LucideIcon }> = [
    { label: "Batches", value: dashboard?.batches ?? 0, Icon: Users },
    { label: "Running", value: dashboard?.active_labs ?? 0, Icon: Activity },
    { label: "Provisioning", value: dashboard?.provisioning_labs ?? 0, Icon: Server },
    { label: "Terminated", value: dashboard?.terminated_labs ?? 0, Icon: Square },
    { label: "Failed", value: dashboard?.failed_labs ?? 0, Icon: AlertCircle },
  ];

  const labViews = [
    { id: "all", label: "All", count: labs.length },
    { id: "running", label: "Running", count: labs.filter((lab) => lab.status === "running").length },
    { id: "stopped", label: "Stopped", count: labs.filter((lab) => lab.status === "stopped").length },
    { id: "provisioning", label: "Provisioning", count: labs.filter((lab) => ["provisioning", "resuming"].includes(lab.status)).length },
    { id: "attention", label: "Attention", count: labs.filter((lab) => ["failed", "budget_exceeded", "expired", "interrupted"].includes(lab.status)).length },
    { id: "terminated", label: "Terminated", count: labs.filter((lab) => lab.status === "terminated").length },
  ];

  const workspaceViews = [
    { id: "labs", label: "Labs", Icon: Activity },
    { id: "launch", label: "Launch", Icon: Play },
    { id: "logs", label: "Logs", Icon: Server },
  ];

  if (!token) {
    return (
      <main className="login-shell min-h-screen px-4 py-10">
        <div className="mx-auto flex min-h-[calc(100vh-5rem)] w-full max-w-6xl items-center">
          <div className="grid w-full gap-5 lg:grid-cols-[0.95fr_1.05fr]">
            <section className="auth-panel p-6 sm:p-8">
              <div className="mb-8 flex items-center justify-between gap-4">
                <div className="brand-tile h-14 w-32">
                  <img src="/unext-logo.jpeg" alt="UNext" className="max-h-10 w-auto object-contain" />
                </div>
                <div className="icon-badge">
                  <Shield className="h-5 w-5" />
                </div>
              </div>
              <p className="eyebrow mb-3">Admin Console</p>
              <h1 className="text-3xl font-black leading-tight text-ink sm:text-4xl">UNext Cloud Lab</h1>
              <p className="mt-2 max-w-md text-sm leading-6 text-slate-600">
                Provision and manage browser-accessible Windows labs from one clean operations console.
              </p>

              <div className="mt-8 space-y-4">
                <label className="form-label">
                  Email
                  <input className="field-3d mt-1.5 w-full" value={email} onChange={(e) => setEmail(e.target.value)} />
                </label>
                <label className="form-label">
                  Password
                  <input className="field-3d mt-1.5 w-full" type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
                </label>
                {error && <p className="alert alert-danger"><AlertCircle className="h-4 w-4" /> {error}</p>}
                <button className="button-3d button-primary w-full justify-center disabled:opacity-60" onClick={login} disabled={busy}>
                  Sign in
                </button>
              </div>
            </section>

            <section className="auth-panel p-6 sm:p-8">
              <div className="mb-8 flex items-center justify-between gap-4">
                <div>
                  <p className="eyebrow mb-3">Student Portal</p>
                  <h2 className="text-2xl font-black text-ink sm:text-3xl">Open Your Lab</h2>
                </div>
                <div className="icon-badge icon-badge-soft">
                  <KeyRound className="h-5 w-5" />
                </div>
              </div>
              <p className="max-w-md text-sm leading-6 text-slate-600">
                Students can check status, launch access, and copy credentials using the assigned lab login.
              </p>

              <div className="mt-7 space-y-4">
                <label className="form-label">
                  Lab username
                  <input className="field-3d mt-1.5 w-full" value={studentUsername} onChange={(e) => setStudentUsername(e.target.value)} />
                </label>
                <label className="form-label">
                  Lab password
                  <input className="field-3d mt-1.5 w-full" type="password" value={studentPassword} onChange={(e) => setStudentPassword(e.target.value)} />
                </label>
                <button className="button-3d button-primary w-full justify-center disabled:opacity-60" onClick={loginStudent} disabled={busy || !studentUsername || !studentPassword}>
                  <ExternalLink className="h-4 w-4" /> View lab
                </button>
              </div>

              {studentLab && (
                <div className="student-result mt-5">
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <p className="truncate font-black text-ink">{studentLab.lab.owner_label}</p>
                      <span className={statusTone(studentLab.lab.status)}>{studentLab.lab.status}</span>
                    </div>
                    {studentLab.access_url && (
                      <a className="open-link" href={labAccessHref(studentLab.access_url)} target="_blank" rel="noreferrer">
                        <ExternalLink className="h-4 w-4" /> Open
                      </a>
                    )}
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {studentLab.progress.map((step) => <span key={step} className="progress-chip">{step}</span>)}
                  </div>
                  <button className="mini-button-3d mt-4" onClick={copyStudentCredentials}><Copy className="h-4 w-4" /> Copy credentials</button>
                </div>
              )}
            </section>
          </div>
        </div>
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-console">
      <header className="sticky top-0 z-20 border-b border-orange-100 bg-white/95 shadow-sm backdrop-blur">
        <div className="mx-auto flex max-w-[1480px] items-center justify-between px-4 py-3 sm:px-5">
          <div className="flex items-center gap-3">
            <div className="brand-tile h-11 w-24">
              <img src="/unext-logo.jpeg" alt="UNext" className="max-h-9 w-auto object-contain" />
            </div>
            <div>
              <h1 className="text-base font-black text-ink sm:text-xl">Cloud Lab Platform</h1>
              <p className="hidden text-xs font-medium text-slate-500 sm:block">Windows EC2 labs through Apache Guacamole</p>
            </div>
          </div>
          <nav className="workspace-nav">
            {workspaceViews.map(({ id, label, Icon }) => (
              <button key={id} className={adminView === id ? "workspace-tab workspace-tab-active" : "workspace-tab"} onClick={() => setAdminView(id)} type="button">
                <Icon className="h-4 w-4" /> {label}
              </button>
            ))}
          </nav>
          <div className="flex gap-2">
            <button className="icon-button-3d" title="Refresh" onClick={refresh} disabled={refreshing}><RefreshCw className={`h-5 w-5 ${refreshing ? "animate-spin" : ""}`} /></button>
            <button className="icon-button-3d" title="Sign out" onClick={() => { localStorage.removeItem("cloudlab_token"); setToken(null); }}><LogOut className="h-5 w-5" /></button>
          </div>
        </div>
      </header>

      <div className="workspace-shell">
        {adminView === "labs" && (
        <section className="space-y-4">
          <div className="command-center p-4">
            <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
              <div className="max-w-3xl">
                <p className="eyebrow mb-2 w-fit">Lab Desk</p>
                <h2 className="text-xl font-black leading-tight text-ink sm:text-2xl">Open, pause, copy, and clean up labs from one focused list.</h2>
              </div>
              <div className="hero-actions">
                <button className="mini-button-3d" onClick={copyRecentCredentials} disabled={busy || !labs.length}><Copy className="h-4 w-4" /> Copy credentials</button>
                <button className="mini-button-3d" title="Download Excel file" onClick={downloadRecentCredentialsExcel} disabled={busy || !labs.length}><Download className="h-4 w-4" /> Excel</button>
              </div>
            </div>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
            {metricItems.map(({ label, value, Icon }) => (
              <div key={label} className="metric-card metric-card-compact">
                <div className="metric-icon"><Icon className="h-4 w-4" /></div>
                <p className="text-xs font-black uppercase tracking-wide text-slate-500">{label}</p>
                <p className="mt-2 text-3xl font-black text-ink">{value}</p>
              </div>
            ))}
          </div>
          <div className="grid gap-3 md:grid-cols-4">
            <div className="insight-card"><DollarSign className="h-4 w-4" /><p>Running Cost</p><strong>${(dashboard?.estimated_running_hourly_cost ?? activeCost).toFixed(3)}/h</strong></div>
            <div className="insight-card"><WalletCards className="h-4 w-4" /><p>Live Total Spend</p><strong>${liveTotalSpend.toFixed(2)}</strong></div>
            <div className="insight-card insight-good"><CheckCircle2 className="h-4 w-4" /><p>Healthy</p><strong>{dashboard?.healthy_labs ?? 0}</strong></div>
            <div className="insight-card insight-danger"><AlertCircle className="h-4 w-4" /><p>Needs Attention</p><strong>{dashboard?.attention_labs ?? 0}</strong></div>
          </div>
          <div className="lab-stage">
            <div className="space-y-3 border-b border-orange-100 px-4 py-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <Activity className="h-5 w-5 text-unext-orange" />
                  <div>
                    <h2 className="font-black text-ink">Labs</h2>
                    <p className="text-xs font-medium text-slate-500">{filteredLabs.length} of {labs.length} visible</p>
                  </div>
                </div>
                <div className="flex flex-wrap items-center justify-end gap-2">
                  {credentialExportStatus && <span className="status-message">{credentialExportStatus}</span>}
                  <span className="cost-chip">${activeCost.toFixed(3)}/hour running</span>
                </div>
              </div>
              <div className="page-tabs">
                {labViews.map((view) => (
                  <button
                    key={view.id}
                    className={labFilter === view.id ? "page-tab page-tab-active" : "page-tab"}
                    onClick={() => setLabFilter(view.id)}
                    type="button"
                  >
                    <span>{view.label}</span>
                    <strong>{view.count}</strong>
                  </button>
                ))}
              </div>
              <div className="grid gap-2">
                <label className="search-box">
                  <Search className="h-4 w-4" />
                  <input value={labQuery} onChange={(e) => setLabQuery(e.target.value)} placeholder="Search lab, user, instance, IP" />
                </label>
              </div>
            </div>
            <div className="lab-list">
              {filteredLabs.map((lab) => {
                const spend = currentSpend(lab, nowMs);
                const runtime = runtimeSeconds(lab, nowMs);
                const budgetPct = percent(spend, lab.budget_limit);
                const timePct = elapsedPercent(lab);
                return (
                  <article key={lab.id} className={`lab-row ${cardTone(lab.status)}`}>
                    <div className="lab-row-main">
                      <div className="lab-row-name">
                        <p className="truncate font-black text-ink">{lab.owner_label}</p>
                        <span className="truncate">{lab.username}</span>
                      </div>
                      <span className={statusTone(lab.status)}>{lab.status}</span>
                      <div className="lab-row-facts">
                        <span><Cpu className="h-3.5 w-3.5" /> {lab.instance_type}</span>
                        <span><Zap className="h-3.5 w-3.5" /> {lab.instance_market === "spot" ? "Spot" : "On-Demand"}</span>
                        <span><Clock className="h-3.5 w-3.5" /> {remainingTime(lab)}</span>
                        <span className={lab.status === "running" ? "live-cost-chip" : ""}><DollarSign className="h-3.5 w-3.5" /> ${spend.toFixed(3)} spent</span>
                      </div>
                      <div className="lab-row-actions">
                        {lab.access_url && ["running", "stopped"].includes(lab.status) ? (
                          <a className="button-3d button-primary min-h-0 px-3 py-2 text-sm" href={labAccessHref(lab.access_url)} target="_blank" rel="noreferrer">
                            <ExternalLink className="h-4 w-4" /> Open
                          </a>
                        ) : (
                          <span className="rounded-xl bg-slate-100 px-3 py-2 text-sm font-bold text-slate-500">Access pending</span>
                        )}
                        <button className="mini-button-3d" onClick={() => startLab(lab.id)} disabled={busy || lab.status !== "stopped"}><Play className="h-4 w-4" /> Start</button>
                        <button className="mini-button-3d" onClick={() => stopLab(lab.id)} disabled={busy || lab.status !== "running"}><Square className="h-4 w-4" /> Stop</button>
                        <button className="mini-button-3d" onClick={() => copyCredentials(lab.id)}><Copy className="h-4 w-4" /> {credentials[lab.id] ?? "Copy"}</button>
                      </div>
                      <button className="icon-button-3d h-10 w-10 text-red-600" title="Force terminate" onClick={() => terminate(lab.id)}><Trash2 className="h-4 w-4" /></button>
                      <details className="lab-details">
                        <summary>Details</summary>
                        <div className="lab-details-body">
                          <div className="grid gap-3 sm:grid-cols-3">
                            <div className="lab-stat"><Cpu className="h-4 w-4" /><span>Instance</span><strong>{lab.instance_type}</strong></div>
                            <div className="lab-stat"><Zap className="h-4 w-4" /><span>Market</span><strong>{lab.instance_market === "spot" ? "Spot" : "On-Demand"}</strong></div>
                            <div className="lab-stat"><Clock className="h-4 w-4" /><span>Runtime</span><strong>{runtimeLabel(runtime)}</strong></div>
                            <div className="lab-stat"><DollarSign className="h-4 w-4" /><span>Cost/hr</span><strong>${lab.hourly_cost.toFixed(3)}</strong></div>
                            {lab.spot_hourly_cost !== null && lab.on_demand_hourly_cost !== null && (
                              <div className="lab-stat"><DollarSign className="h-4 w-4" /><span>Spot estimate</span><strong>${lab.spot_hourly_cost.toFixed(3)} vs ${lab.on_demand_hourly_cost.toFixed(3)}/h</strong></div>
                            )}
                            <div className="lab-stat"><WalletCards className="h-4 w-4" /><span>Live spend</span><strong>${spend.toFixed(4)}</strong></div>
                            {lab.schedule_enabled && <div className="lab-stat sm:col-span-3"><CalendarDays className="h-4 w-4" /><span>Schedule</span><strong>{scheduleLabel(lab)}</strong></div>}
                          </div>
                          <div className="mt-3 grid gap-3 lg:grid-cols-2">
                            <div>
                              <div className="mb-1 flex justify-between text-xs font-bold text-slate-600">
                                <span>Budget used</span><span>${spend.toFixed(2)} / ${lab.budget_limit.toFixed(2)}</span>
                              </div>
                              <div className="bar-track"><div className={budgetPct >= 90 ? "bar-fill bar-danger" : "bar-fill"} style={{ width: `${budgetPct}%` }} /></div>
                            </div>
                            <div>
                              <div className="mb-1 flex justify-between text-xs font-bold text-slate-600">
                                <span>Time elapsed</span><span>{remainingTime(lab)}</span>
                              </div>
                              <div className="bar-track"><div className={timePct >= 90 ? "bar-fill bar-danger" : "bar-fill bar-time"} style={{ width: `${timePct}%` }} /></div>
                            </div>
                          </div>
                          <div className="mt-3 flex flex-wrap gap-1.5">
                            {progressSteps(lab).map((step) => <span key={step} className="progress-chip">{step}</span>)}
                          </div>
                          <div className="mt-3 grid gap-2 sm:grid-cols-2">
                            <div className="quick-input">
                              <input type="number" min="0.01" step="0.01" placeholder="Budget credit" value={creditInputs[lab.id] ?? ""} onChange={(e) => setCreditInputs({ ...creditInputs, [lab.id]: e.target.value })} />
                              <button onClick={() => addBudgetCredit(lab.id)} disabled={busy || !["budget_exceeded", "stopped", "running"].includes(lab.status)}>Add</button>
                            </div>
                            <div className="quick-input">
                              <input type="number" min="0.25" step="0.25" placeholder="Extend hours" value={extendInputs[lab.id] ?? ""} onChange={(e) => setExtendInputs({ ...extendInputs, [lab.id]: e.target.value })} />
                              <button onClick={() => extendLab(lab.id)} disabled={busy || !canExtendLab(lab.status)}>Extend</button>
                            </div>
                          </div>
                        </div>
                      </details>
                    </div>
                  </article>
                );
              })}
              {!filteredLabs.length && (
                <div className="col-span-full p-10 text-center">
                  <p className="text-lg font-black text-ink">{labs.length ? "No labs match your filters" : "No labs yet"}</p>
                  <p className="mt-1 text-sm text-slate-500">{labs.length ? "Adjust search or status to bring labs back into view." : "Create a batch to see lab cards with access, budget, time, and quick controls."}</p>
                </div>
              )}
            </div>
          </div>
        </section>
        )}

        {adminView === "launch" && (
          <section className="launch-board">
            <div className="command-center p-4">
              <p className="eyebrow mb-2 w-fit">Launch Wizard</p>
              <h2 className="text-xl font-black leading-tight text-ink sm:text-2xl">Create a batch without mixing it with live operations.</h2>
            </div>
            <div className="launch-grid">
              <div className="launch-card">
                <h2>Batch</h2>
                <div className="grid gap-3 sm:grid-cols-2">
                  {[
                    ["name", "Batch name"],
                    ["user_count", "Users"],
                    ["duration_hours", "Duration hours"],
                    ["budget_per_vm", "Budget per VM"],
                  ].map(([key, label]) => (
                    <label key={key} className="form-label">
                      {label}
                      <input
                        className="field-3d mt-1.5 w-full"
                        value={(form as unknown as Record<string, string | number>)[key]}
                        onChange={(e) => setForm({ ...form, [key]: ["user_count", "duration_hours", "budget_per_vm"].includes(key) ? Number(e.target.value) : e.target.value })}
                      />
                    </label>
                  ))}
                </div>
              </div>
              <div className="launch-card">
                <h2>Infrastructure</h2>
                <p className="mb-3 text-xs font-bold text-slate-500">New labs use the server market policy; Spot labs may end early if AWS reclaims capacity.</p>
                <div className="grid gap-3 sm:grid-cols-2">
                  {[
                    ["aws_region", "AWS region"],
                    ["instance_type", "Instance type"],
                    ["windows_ami", "Golden Windows AMI"],
                    ["idle_timeout_minutes", "Idle timeout minutes"],
                  ].map(([key, label]) => (
                    <label key={key} className="form-label">
                      {label}
                      <input
                        className="field-3d mt-1.5 w-full"
                        value={(form as unknown as Record<string, string | number>)[key]}
                        onChange={(e) => setForm({ ...form, [key]: key === "idle_timeout_minutes" ? Number(e.target.value) : e.target.value })}
                      />
                    </label>
                  ))}
                </div>
              </div>
              <div className="launch-card">
                <div className="flex items-center justify-between gap-3">
                  <h2>Schedule</h2>
                  <label className="toggle-row compact-toggle">
                    Daily schedule
                    <input type="checkbox" checked={form.schedule_enabled} onChange={(e) => setForm({ ...form, schedule_enabled: e.target.checked })} />
                  </label>
                </div>
                {form.schedule_enabled ? (
                  <div className="mt-3 grid gap-3 sm:grid-cols-4">
                    <label className="form-label">
                      Start date
                      <input className="field-3d mt-1.5 w-full" type="date" value={form.schedule_start_date} onChange={(e) => setForm({ ...form, schedule_start_date: e.target.value })} />
                    </label>
                    <label className="form-label">
                      Days
                      <input className="field-3d mt-1.5 w-full" type="number" min="1" value={form.schedule_days} onChange={(e) => setForm({ ...form, schedule_days: Number(e.target.value) })} />
                    </label>
                    <label className="form-label">
                      Start time
                      <input className="field-3d mt-1.5 w-full" type="time" value={form.schedule_start_time} onChange={(e) => setForm({ ...form, schedule_start_time: e.target.value })} />
                    </label>
                    <label className="form-label">
                      End time
                      <input className="field-3d mt-1.5 w-full" type="time" value={form.schedule_end_time} onChange={(e) => setForm({ ...form, schedule_end_time: e.target.value })} />
                    </label>
                  </div>
                ) : (
                  <p className="mt-3 text-sm font-medium text-slate-500">Labs can be used any time until their duration or budget is exhausted.</p>
                )}
              </div>
            </div>
            {error && <p className="alert alert-danger"><AlertCircle className="h-4 w-4" /> {error}</p>}
            <div className="launch-footer">
              <button className="button-3d button-primary disabled:opacity-60" onClick={createBatch} disabled={busy || !form.windows_ami}>
                <Play className="h-4 w-4" /> Launch Labs
              </button>
            </div>
          </section>
        )}

        {adminView === "logs" && (
          <section className="panel-3d overflow-hidden">
            <div className="flex items-center justify-between gap-3 border-b border-orange-100 px-4 py-3">
              <div>
                <h2 className="font-black text-ink">Provisioning and Cleanup Logs</h2>
                <p className="text-xs font-medium text-slate-500">{logs.length} recent events</p>
              </div>
              <button className="mini-button-3d" onClick={refresh} disabled={refreshing}><RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} /> Refresh</button>
            </div>
            <div className="log-timeline">
              {logs.map((log) => (
                <div key={log.id} className="log-entry">
                  <div className="log-dot" />
                  <div>
                    <div className="font-bold text-ink">{log.action}</div>
                    <div className="text-sm text-slate-600">{log.message}</div>
                    <div className="text-xs text-slate-500">{new Date(log.created_at).toLocaleString()}</div>
                  </div>
                </div>
              ))}
              {!logs.length && (
                <div className="p-10 text-center">
                  <p className="text-lg font-black text-ink">No logs yet</p>
                  <p className="mt-1 text-sm text-slate-500">Provisioning and cleanup activity will appear here.</p>
                </div>
              )}
            </div>
          </section>
        )}
      </div>
    </main>
  );
}
