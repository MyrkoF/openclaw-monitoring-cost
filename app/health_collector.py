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

    # OpenClaw doctor / security audit
    doctor_section = sidecar.get("openclaw_doctor", {})
    audit_section  = sidecar.get("openclaw_security", {})
    doctor  = doctor_section.get("output", sidecar.get("doctor", ""))
    audit   = audit_section.get("output",  sidecar.get("security_audit", ""))
    # Convertir liste → string si nécessaire pour l'affichage existant
    if isinstance(doctor, list): doctor = "\n".join(doctor)
    if isinstance(audit, list):  audit  = "\n".join(audit)

    # WireGuard, GitHub CLI, tmux, Claude Code
    wireguard   = sidecar.get("wireguard", {})
    github_cli  = sidecar.get("github_cli", {})
    tmux        = sidecar.get("tmux", {})
    claude_code = sidecar.get("claude_code", {})

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
        "doctor":             doctor or "Lancer daily-health-check.py sur le host",
        "security_audit":     audit  or "Lancer daily-health-check.py sur le host",
        "wireguard":          wireguard,
        "github_cli":         github_cli,
        "tmux":               tmux,
        "claude_code":        claude_code,
        "sidecar_at":         sidecar_at,
        "sidecar_stale":      _is_stale(sidecar_at),
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
