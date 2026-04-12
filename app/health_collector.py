#!/usr/bin/env python3
"""
health_collector.py — Collecte les métriques système depuis /proc (container).
Les données hôte (Docker, Watchtower, APT, doctor/audit, services) viennent du
sidecar JSON écrit par daily-health-check.py sur le host via le volume ./data:/data.
"""

import subprocess, json, os, time as _time
from datetime import datetime, timedelta

HEALTH_CACHE  = os.environ.get("HEALTH_CACHE",   "/data/health-cache.json")
HEALTH_SIDECAR = os.environ.get("HEALTH_SIDECAR", "/data/host-health.json")


def run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"


# ── /proc helpers (aucune dépendance sur procps) ──────────────────────────────

def _proc_uptime():
    try:
        secs = float(open("/proc/uptime").read().split()[0])
        d = int(secs // 86400)
        h = int((secs % 86400) // 3600)
        m = int((secs % 3600) // 60)
        parts = (([f"{d}j"] if d else []) +
                 ([f"{h}h"] if h else []) +
                 [f"{m}min"])
        human    = " ".join(parts)
        boot_dt  = datetime.utcfromtimestamp(_time.time() - secs)
        return human, boot_dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception as e:
        return f"ERROR: {e}", ""


def _proc_memory():
    try:
        info = {}
        for line in open("/proc/meminfo"):
            p = line.split()
            if len(p) >= 2:
                info[p[0].rstrip(":")] = int(p[1])
        def fmt(kb):
            if kb >= 1024 * 1024: return f"{kb/1024/1024:.1f}G"
            return f"{kb/1024:.0f}M"
        total  = info.get("MemTotal", 0)
        avail  = info.get("MemAvailable", 0)
        used   = total - avail
        swap_t = info.get("SwapTotal", 0)
        swap_u = swap_t - info.get("SwapFree", 0)
        s = f"{fmt(total)} total / {fmt(used)} used / {fmt(avail)} free"
        if swap_t:
            s += f" · swap {fmt(swap_u)}/{fmt(swap_t)}"
        return s
    except Exception as e:
        return f"ERROR: {e}"


def _proc_memory_detail():
    try:
        info = {}
        for line in open("/proc/meminfo"):
            p = line.split()
            if len(p) >= 2:
                info[p[0].rstrip(":")] = int(p[1])
        def fmt(kb):
            if kb >= 1024 * 1024: return f"{kb/1024/1024:.1f}G"
            return f"{kb/1024:.0f}M"
        total  = info.get("MemTotal", 0)
        avail  = info.get("MemAvailable", 0)
        used   = total - avail
        swap_t = info.get("SwapTotal", 0)
        swap_u = swap_t - info.get("SwapFree", 0)
        return {
            "ram_total": fmt(total), "ram_used": fmt(used), "ram_free": fmt(avail),
            "ram_pct": round(used / total * 100) if total else 0,
            "swap_used": fmt(swap_u) if swap_t else None,
            "swap_total": fmt(swap_t) if swap_t else None,
        }
    except Exception:
        return {}


def _proc_disk():
    try:
        out = run("df -h /")
        lines = [l for l in out.splitlines() if l and not l.startswith("Filesystem")]
        if not lines: return "N/A"
        p = lines[0].split()
        return f"{p[1]} total / {p[2]} used / {p[3]} free / {p[4]} use%"
    except Exception as e:
        return f"ERROR: {e}"


def _proc_cpu_percent():
    def _stat():
        vals = list(map(int, open("/proc/stat").readline().split()[1:]))
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return idle, sum(vals)
    try:
        i1, t1 = _stat(); _time.sleep(1); i2, t2 = _stat()
        dt = t2 - t1
        return f"{100*(dt-(i2-i1))/dt:.1f}%" if dt else "0.0%"
    except Exception as e:
        return f"ERROR: {e}"


def _is_stale(iso_ts, hours=4):
    if not iso_ts: return True
    try:
        return (datetime.now() - datetime.fromisoformat(iso_ts.replace("Z", "+00:00").replace("+00:00", ""))).total_seconds() > hours * 3600
    except Exception:
        return True


# ── Watchtower HTTP API (opt-in via WATCHTOWER_API_TOKEN) ─────────────────────

def _watchtower_api():
    url   = os.environ.get("WATCHTOWER_API_URL", "http://host.docker.internal:8080")
    token = os.environ.get("WATCHTOWER_API_TOKEN", "")
    if not token:
        return None
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{url.rstrip('/')}/v1/report",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        report  = data.get("report", data)
        updated = report.get("Updated") or []
        stale   = report.get("Stale") or []
        lines = []
        for entry in updated:
            name    = entry.get("Name", entry.get("name", "?"))
            old_img = entry.get("OldImage", "")
            new_img = entry.get("NewImage", "")
            lines.append(f"Updated {name}: {old_img} → {new_img}")
        for entry in stale:
            name = entry.get("Name", entry.get("name", "?"))
            lines.append(f"Stale (not updated): {name}")
        return lines
    except Exception:
        return None


# ── OpenClaw version check (GitHub Atom feed) ────────────────────────────────

def _check_openclaw_latest():
    """Fetch latest stable OpenClaw release from GitHub Atom feed."""
    import urllib.request, xml.etree.ElementTree as ET
    url = "https://github.com/openclaw/openclaw/releases.atom"
    req = urllib.request.Request(url, headers={"User-Agent": "monitoring-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        tree = ET.fromstring(resp.read().decode())
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in tree.findall("atom:entry", ns):
        tag_id = entry.find("atom:id", ns)
        link = entry.find("atom:link", ns)
        if tag_id is None:
            continue
        tag = tag_id.text.rsplit("/", 1)[-1]  # e.g. "v2026.4.10"
        # Skip pre-release versions
        if any(x in tag.lower() for x in ("beta", "alpha", "rc", "pre", "dev")):
            continue
        version = tag.lstrip("v")
        release_url = link.get("href", "") if link is not None else ""
        return {"latest": version, "url": release_url}
    return None


# ── OpenClaw Gateway API (live sessions/agents) ─────────────────────────────

def _gw_invoke(url, token, ctx, tool_name, action="json"):
    """POST /tools/invoke → parse details from response."""
    import urllib.request
    data = json.dumps({"tool": tool_name, "action": action, "args": {}}).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/tools/invoke",
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
        body = json.loads(resp.read().decode())
    if not body.get("ok"):
        raise RuntimeError(body.get("error", {}).get("message", "unknown error"))
    return body["result"].get("details", body["result"])


def _openclaw_gateway():
    """Query OpenClaw gateway for live session/agent data. Returns dict or None."""
    url   = os.environ.get("OPENCLAW_GATEWAY_URL", "https://127.0.0.1:18789")
    token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
    if not token:
        return None

    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    result = {"status": "ok", "collected_at": datetime.utcnow().isoformat(), "errors": []}

    # ── sessions_list ──
    try:
        sl = _gw_invoke(url, token, ctx, "sessions_list")
        sessions = sl.get("sessions", [])
        total_cost = sum(s.get("estimatedCostUsd", 0) for s in sessions)
        total_tokens = sum(s.get("totalTokens", 0) for s in sessions)
        by_model = {}
        by_channel = {}
        for s in sessions:
            m = s.get("model", "unknown")
            by_model.setdefault(m, {"tokens": 0, "cost_usd": 0, "count": 0, "provider": "?"})
            by_model[m]["tokens"] += s.get("totalTokens", 0)
            by_model[m]["cost_usd"] += s.get("estimatedCostUsd", 0)
            by_model[m]["count"] += 1
            ch = s.get("channel", "unknown")
            by_channel.setdefault(ch, 0)
            by_channel[ch] += 1
        active = [
            {
                "key": s.get("key", ""),
                "name": s.get("displayName", s.get("label", s.get("key", "?"))),
                "model": s.get("model", "?"),
                "channel": s.get("channel", "?"),
                "tokens": s.get("totalTokens", 0),
                "cost_usd": round(s.get("estimatedCostUsd", 0), 6),
                "status": s.get("status", "?"),
                "updated": datetime.utcfromtimestamp(s["updatedAt"] / 1000).strftime("%H:%M:%S")
                           if s.get("updatedAt") else "?",
                "updated_at_ms": s.get("updatedAt", 0),
            }
            for s in sessions
        ]
        result["sessions"] = {
            "count": sl.get("count", len(sessions)),
            "total_cost_usd": round(total_cost, 6),
            "total_tokens": total_tokens,
            "by_model": by_model,
            "by_channel": by_channel,
            "active": active,
        }
    except Exception as e:
        result["errors"].append(f"sessions_list: {e}")

    # ── agents_list ──
    try:
        al = _gw_invoke(url, token, ctx, "agents_list")
        agents = al.get("agents", [])
        result["agents"] = {
            "count": len(agents),
            "list": [{"id": a.get("id", "?"), "name": a.get("name", a.get("id", "?"))} for a in agents],
        }
    except Exception as e:
        result["errors"].append(f"agents_list: {e}")

    # ── session_status (version) ──
    try:
        ss = _gw_invoke(url, token, ctx, "session_status")
        status_text = ss.get("statusText", "")
        version = ""
        for line in status_text.splitlines():
            if "OpenClaw" in line:
                parts = line.split()
                for p in parts:
                    if p and p[0].isdigit():
                        version = p
                        break
                break
        result["version"] = version or "?"
        result["session_status_text"] = status_text
    except Exception as e:
        result["errors"].append(f"session_status: {e}")

    # ── version check (GitHub Atom feed) ──
    try:
        latest = _check_openclaw_latest()
        if latest:
            result["latest_stable"] = latest
    except Exception:
        pass

    # ── cron (from jobs.json file — no HTTP tool needed) ──
    try:
        _cron_path = os.environ.get("OPENCLAW_CRON_JOBS", "/openclaw-cron-jobs.json")
        with open(_cron_path) as _f:
            _cron_data = json.load(_f)
        jobs = _cron_data.get("jobs", [])
        cron_jobs = []
        for j in jobs:
            state = j.get("state", {})
            last_run_ms = state.get("lastRunAtMs")
            next_run_ms = state.get("nextRunAtMs")
            cron_jobs.append({
                "name": j.get("name", "?"),
                "enabled": j.get("enabled", False),
                "schedule": j.get("schedule", {}).get("expr", "?"),
                "tz": j.get("schedule", {}).get("tz", "?"),
                "model": j.get("payload", {}).get("model", "?"),
                "last_status": state.get("lastRunStatus", "?"),
                "last_duration_s": round(state.get("lastDurationMs", 0) / 1000, 1),
                "last_run": datetime.utcfromtimestamp(last_run_ms / 1000).strftime("%Y-%m-%d %H:%M UTC")
                            if last_run_ms else "?",
                "next_run": datetime.utcfromtimestamp(next_run_ms / 1000).strftime("%Y-%m-%d %H:%M UTC")
                            if next_run_ms else "?",
                "consecutive_errors": state.get("consecutiveErrors", 0),
                "delivery": j.get("delivery", {}).get("mode", "none"),
            })
        result["cron"] = {"count": len(cron_jobs), "jobs": cron_jobs}
    except Exception as e:
        result["errors"].append(f"cron: {e}")

    # ── Build provider map from multiple sources ──
    _known = ("openrouter", "openai", "anthropic", "google", "claude-cli", "openai-codex")
    prov_map = {}
    # Source 1: sessions.json files (most reliable — has modelProvider field)
    _sessions_dir = os.environ.get("OPENCLAW_SESSIONS_DIR", "/openclaw-sessions")
    try:
        import glob as _glob
        for sf in _glob.glob(f"{_sessions_dir}/*/sessions/sessions.json"):
            with open(sf) as _f:
                for _sess in json.load(_f).values():
                    mp = _sess.get("modelProvider", "")
                    model = _sess.get("model", "")
                    if mp and model:
                        prov_map[model] = mp
    except Exception:
        pass
    # Source 2: cron job payloads (e.g. "openai/gpt-4o-mini")
    for j in result.get("cron", {}).get("jobs", []):
        full = j.get("model", "")
        parts = full.split("/", 1)
        if len(parts) == 2 and parts[0] in _known:
            prov_map.setdefault(parts[1], parts[0])
    # Source 3: session_status text (e.g. "Model: claude-cli/claude-sonnet-4-6")
    for line in result.get("session_status_text", "").splitlines():
        if "Model:" in line:
            for token in line.split():
                parts = token.split("/", 1)
                if len(parts) == 2 and parts[0] in _known:
                    prov_map.setdefault(parts[1], parts[0])
                    break
    # Apply provider to by_model
    if prov_map and "sessions" in result:
        for m, v in result["sessions"]["by_model"].items():
            if v.get("provider") == "?":
                v["provider"] = prov_map.get(m, "?")

    if len(result["errors"]) == 3:  # sessions, agents, session_status all failed
        return None  # total failure → fallback sidecar
    if result["errors"]:
        result["status"] = "partial"
    return result


# ── Lecture du sidecar hôte ───────────────────────────────────────────────────

def _load_sidecar():
    try:
        with open(HEALTH_SIDECAR) as f:
            return json.load(f)
    except Exception:
        return {}


# ── Collecte principale ───────────────────────────────────────────────────────

def collect_system():
    # Métriques depuis /proc (fonctionnent dans tout container Linux)
    uptime, uptime_since = _proc_uptime()
    load      = run("cat /proc/loadavg")
    mem       = _proc_memory()
    mem_detail = _proc_memory_detail()
    disk      = _proc_disk()
    cpu_pct   = _proc_cpu_percent()
    cpu_cores = run("nproc")

    # Watchtower — API HTTP opt-in uniquement (aucun docker.sock nécessaire)
    wt_api    = _watchtower_api()
    wt_source = "api" if wt_api is not None else "sidecar"

    # OpenClaw Gateway API — live sessions/agents
    gw_data   = _openclaw_gateway()

    # ── Données hôte depuis le sidecar (daily-health-check.py) ──────────────
    sidecar = _load_sidecar()

    # Docker containers — nouvelle structure sidecar ou ancienne (compat)
    docker_section = sidecar.get("docker", {})
    containers = docker_section.get(
        "containers",
        sidecar.get("docker_containers", [])   # ancien format
    )
    docker_counts = {
        "total":   docker_section.get("total",   len(containers)),
        "running": docker_section.get("running", sum(1 for c in containers if "Up" in c.get("status",""))),
        "stopped": docker_section.get("stopped", 0),
    }

    # Watchtower — API en priorité, puis sidecar
    wt_section = sidecar.get("watchtower", {})
    if wt_api is not None:
        wt_lines  = wt_api
        wt_errors = []
    else:
        wt_lines = wt_section.get(
            "updates",
            sidecar.get("watchtower_updates", sidecar.get("watchtower_raw", []))
        )
        wt_errors = wt_section.get("errors", [])

    # APT
    apt_section   = sidecar.get("apt", {})
    apt_updates   = apt_section.get("recent_lines", sidecar.get("apt_updates", []))
    apt_upgradable = apt_section.get("upgradable", [])
    apt_upgradable_count = apt_section.get("upgradable_count", len(apt_upgradable))
    apt_timers    = apt_section.get("apt_timers", {})

    # Services
    services = sidecar.get("services", {})

    # OpenClaw doctor / security audit — garder les données structurées
    doctor_section = sidecar.get("openclaw_doctor", {})
    audit_section  = sidecar.get("openclaw_security", {})
    # Raw output pour expander debug
    doctor_raw  = doctor_section.get("output", sidecar.get("doctor", ""))
    audit_raw   = audit_section.get("output",  sidecar.get("security_audit", ""))
    if isinstance(doctor_raw, list): doctor_raw = "\n".join(doctor_raw)
    if isinstance(audit_raw, list):  audit_raw  = "\n".join(audit_raw)

    # WireGuard, GitHub CLI, tmux, Claude Code, Network
    wireguard    = sidecar.get("wireguard", {})
    github_cli   = sidecar.get("github_cli", {})
    tmux         = sidecar.get("tmux", {})
    claude_code  = sidecar.get("claude_code", {})
    network      = sidecar.get("network", {})
    fail2ban     = sidecar.get("fail2ban", {})
    ufw          = sidecar.get("ufw", {})
    ssh_sessions = sidecar.get("ssh_sessions", {})
    docker_stats = sidecar.get("docker", {}).get("docker_stats", [])
    disks        = sidecar.get("resources", {}).get("disks", [])
    openclaw_version = sidecar.get("openclaw_version", {})
    # Prefer gateway version (live) over sidecar version (10min stale)
    if gw_data and gw_data.get("version"):
        openclaw_version = {**openclaw_version, "installed": gw_data["version"], "source": "gateway"}
    # Latest stable from GitHub Atom feed
    if gw_data and gw_data.get("latest_stable"):
        ls = gw_data["latest_stable"]
        openclaw_version["latest"] = ls["latest"]
        openclaw_version["latest_url"] = ls["url"]
        installed = openclaw_version.get("installed", "")
        openclaw_version["up_to_date"] = (installed == ls["latest"])
    wt_image_updates = sidecar.get("watchtower", {}).get("image_updates", [])

    # Métadonnées sidecar
    meta       = sidecar.get("meta", {})
    sidecar_at = meta.get("collected_at", sidecar.get("collected_at", ""))
    global_status = meta.get("global_status", "")

    result = {
        "collected_at":       datetime.utcnow().isoformat(),
        "uptime":             uptime,
        "uptime_since":       uptime_since,
        "load":               load,
        "memory":             mem,
        "memory_detail":      mem_detail,
        "disk":               disk,
        "cpu_cores":          cpu_cores,
        "cpu_percent":        cpu_pct,
        "docker_containers":  containers,
        "docker_counts":      docker_counts,
        "watchtower_updates": wt_lines,
        "watchtower_errors":  wt_errors,
        "watchtower_source":  wt_source,
        "apt_updates":        apt_updates,
        "apt_upgradable":     apt_upgradable,
        "apt_upgradable_count": apt_upgradable_count,
        "apt_timers":         apt_timers,
        "services":           services,
        "global_status":      global_status,
        "doctor":             doctor_raw or "Lancer daily-health-check.py sur le host",
        "security_audit":     audit_raw  or "Lancer daily-health-check.py sur le host",
        "doctor_structured":  doctor_section,
        "security_structured": audit_section,
        "wireguard":          wireguard,
        "github_cli":         github_cli,
        "tmux":               tmux,
        "claude_code":        claude_code,
        "network":            network,
        "fail2ban":           fail2ban,
        "ufw":                ufw,
        "ssh_sessions":       ssh_sessions,
        "docker_stats":       docker_stats,
        "disks":              disks,
        "openclaw_version":   openclaw_version,
        "wt_image_updates":   wt_image_updates,
        "sidecar_at":         sidecar_at,
        "sidecar_stale":      _is_stale(sidecar_at),
        "openclaw_gateway":   gw_data,
    }

    # Écrire le cache
    try:
        os.makedirs(os.path.dirname(HEALTH_CACHE), exist_ok=True)
        with open(HEALTH_CACHE, "w") as f:
            json.dump(result, f, indent=2)
    except Exception as e:
        result["cache_error"] = str(e)

    return result


if __name__ == "__main__":
    r = collect_system()
    print(json.dumps(r, indent=2))
