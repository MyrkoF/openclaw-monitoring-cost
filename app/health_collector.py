#!/usr/bin/env python3
"""
health_collector.py — Collecte les infos système Linux du VPS.
Tourne toutes les 5 min via thread dans le dashboard.
Lit /proc directement (pas besoin de procps dans le container).
"""

import subprocess, json, os, time as _time
from datetime import datetime, timedelta

HEALTH_CACHE  = os.environ.get("HEALTH_CACHE",  "/data/health-cache.json")
HEALTH_SIDECAR = os.environ.get("HEALTH_SIDECAR", "/data/daily-health.json")


def run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"


# ── /proc helpers (pas de dépendance procps) ──────────────────────────────────

def _proc_uptime():
    """Retourne (uptime_human, uptime_since_utc) depuis /proc/uptime."""
    try:
        secs = float(open("/proc/uptime").read().split()[0])
        d = int(secs // 86400)
        h = int((secs % 86400) // 3600)
        m = int((secs % 3600) // 60)
        parts = (([f"{d}j"] if d else []) +
                 ([f"{h}h"] if h else []) +
                 [f"{m}min"])
        human = " ".join(parts)
        boot_dt = datetime.utcfromtimestamp(_time.time() - secs)
        return human, boot_dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception as e:
        return f"ERROR: {e}", ""


def _proc_memory():
    """Retourne une chaîne 'X total / Y used / Z free [· swap ...]' depuis /proc/meminfo."""
    try:
        info = {}
        for line in open("/proc/meminfo"):
            p = line.split()
            if len(p) >= 2:
                info[p[0].rstrip(":")] = int(p[1])  # kB
        def fmt(kb):
            if kb >= 1024 * 1024:
                return f"{kb / 1024 / 1024:.1f}G"
            return f"{kb / 1024:.0f}M"
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


def _proc_disk():
    """Retourne l'usage du disque racine (df sans awk)."""
    try:
        out = run("df -h /")
        lines = [l for l in out.splitlines() if l and not l.startswith("Filesystem")]
        if not lines:
            return "N/A"
        p = lines[0].split()
        # p: [filesystem, size, used, avail, use%, mountpoint]
        return f"{p[1]} total / {p[2]} used / {p[3]} free / {p[4]} use%"
    except Exception as e:
        return f"ERROR: {e}"


def _proc_cpu_percent():
    """Retourne le % CPU instantané (2 lectures /proc/stat espacées de 1s)."""
    def _stat():
        vals = list(map(int, open("/proc/stat").readline().split()[1:]))
        idle  = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        return idle, sum(vals)
    try:
        i1, t1 = _stat()
        _time.sleep(1)
        i2, t2 = _stat()
        dt = t2 - t1
        return f"{100 * (dt - (i2 - i1)) / dt:.1f}%" if dt else "0.0%"
    except Exception as e:
        return f"ERROR: {e}"


# ── Watchtower HTTP API ───────────────────────────────────────────────────────

def _watchtower_api():
    """
    Interroge l'API HTTP Watchtower GET /v1/report.
    Retourne une liste de chaînes de mise à jour, ou None si indisponible/non configuré.
    Env: WATCHTOWER_API_URL, WATCHTOWER_API_TOKEN
    """
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


# ── Sidecar daily-health-check.py ────────────────────────────────────────────

def _load_sidecar():
    """Lit le JSON produit par daily-health-check.py sur le host."""
    try:
        with open(HEALTH_SIDECAR) as f:
            return json.load(f)
    except Exception:
        return {}


# ── Collecte principale ───────────────────────────────────────────────────────

def collect_system():
    uptime, uptime_since = _proc_uptime()
    load      = run("cat /proc/loadavg")
    mem       = _proc_memory()
    disk      = _proc_disk()
    cpu_pct   = _proc_cpu_percent()
    cpu_cores = run("nproc")

    # Docker containers (socket monté via docker-compose)
    docker_out = run("docker ps --format '{{.Names}}|{{.Status}}|{{.Image}}' 2>/dev/null")
    containers = []
    for line in docker_out.splitlines():
        parts = line.split("|")
        if len(parts) == 3:
            containers.append({"name": parts[0], "status": parts[1], "image": parts[2]})

    # Watchtower — API HTTP en premier, fallback docker logs
    _wt_from_api = True
    wt_lines = _watchtower_api()
    if wt_lines is None:
        _wt_from_api = False
        wt_raw = run(
            "docker logs watchtower --since 24h 2>&1 "
            "| grep -iE 'updated|pulled|updating' | tail -10"
        )
        wt_lines = [l for l in wt_raw.splitlines() if l.strip()]

    # Apt updates 24h
    since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d")
    apt_out = run(
        f"grep -E ' install | upgrade ' /var/log/dpkg.log 2>/dev/null "
        f"| awk '$0 >= \"{since}\"'"
    )
    apt_updates = [l for l in apt_out.splitlines() if l.strip()]

    result = {
        "collected_at":      datetime.utcnow().isoformat(),
        "uptime":            uptime,
        "uptime_since":      uptime_since,
        "load":              load,
        "memory":            mem,
        "disk":              disk,
        "cpu_cores":         cpu_cores,
        "cpu_percent":       cpu_pct,
        "docker_containers": containers,
        "watchtower_updates": wt_lines,
        "watchtower_source":  "api" if _wt_from_api else "logs",
        "apt_updates":       apt_updates,
        "doctor":            "Run 'openclaw doctor' manually to populate",
        "security_audit":    "Run 'openclaw security audit' manually to populate",
    }

    # Fusionner les données du sidecar (daily-health-check.py sur le host)
    sidecar = _load_sidecar()
    if sidecar:
        result["doctor"]         = sidecar.get("doctor", result["doctor"])
        result["security_audit"] = sidecar.get("security_audit", result["security_audit"])
        result["sidecar_at"]     = sidecar.get("collected_at", "")
        # Préférer les données sidecar pour watchtower/apt si plus complètes
        if sidecar.get("watchtower_raw") and not wt_lines:
            result["watchtower_updates"] = sidecar["watchtower_raw"]
        if sidecar.get("apt_updates") and not apt_updates:
            result["apt_updates"] = sidecar["apt_updates"]

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
