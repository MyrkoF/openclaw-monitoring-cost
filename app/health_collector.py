#!/usr/bin/env python3
"""
health_collector.py — Collecte les infos système Linux du VPS.
Tourne toutes les 5 min via thread dans le dashboard.
Ne dépend pas du script daily-health-check.py.
"""

import subprocess, json, os
from datetime import datetime

HEALTH_CACHE = os.environ.get("HEALTH_CACHE", "/data/health-cache.json")


def run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"


def collect_system():
    uptime      = run("uptime -p")
    uptime_since = run("uptime -s")
    load        = run("cat /proc/loadavg")
    mem         = run("free -h | awk 'NR==2{print $2\" total / \"$3\" used / \"$4\" free\"}'")
    disk        = run("df -h / | awk 'NR==2{print $2\" total / \"$3\" used / \"$4\" free / \"$5\" use%'}")
    cpu_cores   = run("nproc")

    # Docker containers (hôte monté via socket ou pas disponible en container)
    docker_out  = run("docker ps --format '{{.Names}}|{{.Status}}|{{.Image}}' 2>/dev/null")
    containers  = []
    for line in docker_out.splitlines():
        parts = line.split("|")
        if len(parts) == 3:
            containers.append({"name": parts[0], "status": parts[1], "image": parts[2]})

    # Watchtower updates
    wt = run("docker logs watchtower --since 24h 2>&1 | grep -iE 'updated|pulled|updating' | tail -10")
    wt_lines = [l for l in wt.splitlines() if l.strip()]

    # Apt updates 24h
    from datetime import timedelta
    since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d")
    apt_out = run(f"grep -E ' install | upgrade ' /var/log/dpkg.log 2>/dev/null | awk '$0 >= \"{since}\"'")
    apt_updates = [l for l in apt_out.splitlines() if l.strip()]

    result = {
        "collected_at": datetime.utcnow().isoformat(),
        "uptime": uptime,
        "uptime_since": uptime_since,
        "load": load,
        "memory": mem,
        "disk": disk,
        "cpu_cores": cpu_cores,
        "docker_containers": containers,
        "watchtower_updates": wt_lines,
        "apt_updates": apt_updates,
        "doctor": "Run 'openclaw doctor' manually to populate",
        "security_audit": "Run 'openclaw security audit' manually to populate",
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
