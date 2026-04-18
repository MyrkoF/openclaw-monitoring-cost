#!/usr/bin/env python3
"""
daily-health-check.py — Collecte les métriques hôte pour le dashboard monitoring.

Tourne sur le HOST (pas dans le container Docker).
Écrit ./data/host-health.json (lu par le container via le volume ./data:/data).
Stdout : rapport markdown pour Matrix/notifications.

Cron : */30 * * * * cd ~/openclaw-monitoring-cost && python3 daily-health-check.py >/dev/null 2>&1

Collectes légères (chaque run, 30min) : meta, resources, docker, services,
wireguard, fail2ban, ufw, ssh, tmux, apt.

Collectes lourdes (1x/jour OU flag /data/.refresh-requested) : openclaw
doctor/security/version, network, github cli, watchtower logs, classification.
"""

import subprocess
import json
import os
import shlex
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import re

# ── Configuration ──────────────────────────────────────────────────────────────

SIDECAR_PATH = Path(os.environ.get(
    "HEALTH_SIDECAR",
    Path(__file__).parent / "data" / "host-health.json",
))

SERVICES_TO_CHECK = ["docker", "caddy", "nginx", "ssh", "ufw", "fail2ban"]

REFRESH_FLAG = Path(os.environ.get(
    "REFRESH_FLAG",
    SIDECAR_PATH.parent / ".refresh-requested",
))


# ── Helpers ────────────────────────────────────────────────────────────────────

def run(cmd, timeout=30):
    """Execute shell command, return (stdout, stderr, returncode). Never raises."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", -1
    except Exception as e:
        return "", str(e), -1


def _calc_status(*statuses):
    """Aggregate module statuses: error > warn > ok."""
    if "error" in statuses:
        return "error"
    if "warn" in statuses:
        return "warn"
    return "ok"


# ── Modules de collecte ────────────────────────────────────────────────────────

def collect_meta():
    hostname, _, _  = run("hostname -f")
    uptime_str, _, _ = run("uptime -p")
    kernel, _, _    = run("uname -r")
    return {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "hostname":     hostname or "unknown",
        "uptime":       uptime_str or "",
        "kernel":       kernel or "",
        "global_status": "ok",
    }


def collect_network():
    """Collect network interfaces, public IP, VPN IPs."""
    public_ip, _, _ = run("curl -4 -s --max-time 5 ifconfig.me 2>/dev/null")
    interfaces = []
    ip_out, _, _ = run("ip -4 addr show")
    for line in ip_out.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            parts = line.split()
            addr = parts[1]  # e.g. 10.8.0.1/24
            # find interface name (last word after "scope ... <iface>")
            iface = parts[-1] if len(parts) > 1 else ""
            ip_only = addr.split("/")[0]
            if ip_only == "127.0.0.1":
                continue
            itype = "vpn" if iface.startswith("wg") else ("docker" if iface.startswith(("br-", "docker", "veth")) else "lan")
            interfaces.append({"iface": iface, "addr": addr, "type": itype})
    return {
        "public_ip": public_ip.strip() if public_ip else "",
        "interfaces": interfaces,
    }


def collect_resources():
    try:
        load_raw, _, _ = run("cat /proc/loadavg")
        load_parts = load_raw.split() if load_raw else []
        load_1m  = float(load_parts[0]) if len(load_parts) > 0 else None
        load_5m  = float(load_parts[1]) if len(load_parts) > 1 else None
        load_15m = float(load_parts[2]) if len(load_parts) > 2 else None
    except Exception:
        load_1m = load_5m = load_15m = None

    try:
        free_out, _, _ = run("free -m")
        mem_line = [l for l in free_out.splitlines() if l.startswith("Mem:")][0].split()
        ram_total = int(mem_line[1])
        ram_used  = int(mem_line[2])
        ram_free  = int(mem_line[3])
        ram_pct   = round(ram_used / ram_total * 100) if ram_total else 0
    except Exception:
        ram_total = ram_used = ram_free = ram_pct = None

    # Multi-disk support
    disks = []
    try:
        df_out, _, _ = run("df -h --type=ext4 --type=xfs --type=btrfs --type=vfat 2>/dev/null || df -h")
        for line in df_out.splitlines():
            if line.startswith("/dev"):
                parts = line.split()
                if len(parts) >= 6:
                    mount = parts[5]
                    if mount.startswith("/boot") or mount.startswith("/snap"):
                        continue
                    disks.append({
                        "device":  parts[0],
                        "total":   parts[1],
                        "used":    parts[2],
                        "avail":   parts[3],
                        "pct":     parts[4],
                        "mount":   mount,
                    })
    except Exception:
        pass
    # Compat: keep first disk as flat fields
    d0 = disks[0] if disks else {}

    return {
        "load_1m":     load_1m,
        "load_5m":     load_5m,
        "load_15m":    load_15m,
        "ram_total_mb": ram_total,
        "ram_used_mb":  ram_used,
        "ram_free_mb":  ram_free,
        "ram_pct":      ram_pct,
        "disk_total":  d0.get("total"),
        "disk_used":   d0.get("used"),
        "disk_avail":  d0.get("avail"),
        "disk_pct":    d0.get("pct"),
        "disks":       disks,
    }


def collect_docker():
    try:
        out, _, _ = run("docker ps -a --format '{{json .}}' 2>/dev/null")
        containers = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                ports_raw = c.get("Ports", "")
                # Extract first host-mapped port (e.g. "0.0.0.0:8888->8888/tcp" -> "8888")
                main_port = ""
                for pm in re.findall(r'(?:[\d.]+:)?(\d+)->\d+', ports_raw):
                    main_port = pm
                    break
                containers.append({
                    "name":      c.get("Names", c.get("Name", "")),
                    "image":     c.get("Image", ""),
                    "state":     c.get("State", ""),
                    "status":    c.get("Status", ""),
                    "ports":     ports_raw,
                    "main_port": main_port,
                })
            except json.JSONDecodeError:
                # Fallback parsing si le format JSON est partiel
                pass
        running = sum(1 for c in containers if c.get("state") == "running")
        docker_stats = []
        stats_out, _, _ = run(
            'docker stats --no-stream --format "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"',
            timeout=15
        )
        if stats_out:
            rows = []
            for line in stats_out.splitlines():
                p = line.split("\t")
                if len(p) >= 3:
                    rows.append({"name": p[0], "cpu_pct": p[1],
                                 "mem_usage": p[2], "mem_pct": p[3] if len(p) > 3 else ""})
            rows.sort(key=lambda x: float(x["cpu_pct"].rstrip("%") or 0), reverse=True)
            docker_stats = rows[:5]
        return {
            "containers":   containers,
            "total":        len(containers),
            "running":      running,
            "stopped":      len(containers) - running,
            "docker_stats": docker_stats,
        }
    except Exception as e:
        return {"containers": [], "total": 0, "running": 0, "stopped": 0, "docker_stats": [], "error": str(e)}


def collect_watchtower():
    try:
        out, err, _ = run("docker logs --tail 200 watchtower 2>&1")
        combined = out or err
        raw_lines = [l for l in combined.splitlines() if l.strip()]
        updates = [l for l in raw_lines if "Update session completed" in l]
        errors  = [l for l in raw_lines if "error" in l.lower() and "Update session" not in l]

        # Parse image updates: "Found new image" → container + image
        image_updates = []
        for line in raw_lines:
            if "Found new image" not in line:
                continue
            ts_m = re.search(r'time="([^"]+)"', line)
            ctr_m = re.search(r'container=(\S+)', line)
            img_m = re.search(r'image="([^"]+)"', line)
            if ctr_m and img_m:
                image_updates.append({
                    "time":      ts_m.group(1)[:16] if ts_m else "",
                    "container": ctr_m.group(1),
                    "image":     img_m.group(1),
                })

        return {
            "raw_last50":    raw_lines[-50:],
            "updates":       updates,
            "errors":        errors,
            "image_updates": image_updates,
        }
    except Exception as e:
        return {"raw_last50": [], "updates": [], "errors": [], "image_updates": [], "error": str(e)}


def collect_apt():
    try:
        out, _, _ = run("tail -200 /var/log/dpkg.log 2>/dev/null")
        lines = out.splitlines() if out else []
        installs  = [l for l in lines if " install " in l]
        upgrades  = [l for l in lines if " upgrade " in l]
        recent    = sorted(set(installs + upgrades))
    except Exception:
        recent = installs = upgrades = []

    try:
        upg_out, _, _ = run("apt list --upgradable 2>/dev/null")
        upgradable = [
            l for l in upg_out.splitlines()
            if "/" in l and "upgradable" not in l.lower()
        ]
    except Exception:
        upgradable = []

    # APT auto-update timers
    apt_timers = {}
    try:
        timer_out, _, _ = run("systemctl list-timers apt-daily.timer apt-daily-upgrade.timer --no-pager 2>/dev/null")
        for line in (timer_out or "").splitlines():
            if "apt-daily-upgrade" in line:
                m = re.match(r'(\S+ \S+ \S+ \S+)\s+(\S+)\s', line)
                if m:
                    apt_timers["next_upgrade"] = m.group(1)
                    apt_timers["left_upgrade"] = m.group(2)
            elif "apt-daily.timer" in line or "apt-daily.service" in line:
                m = re.match(r'(\S+ \S+ \S+ \S+)\s+(\S+)\s', line)
                if m:
                    apt_timers["next_check"] = m.group(1)
                    apt_timers["left_check"] = m.group(2)
    except Exception:
        pass

    return {
        "recent_lines":    recent[-50:],
        "install_count":   len(installs),
        "upgrade_count":   len(upgrades),
        "upgradable":      upgradable,
        "upgradable_count": len(upgradable),
        "apt_timers":      apt_timers,
    }


def collect_openclaw_version():
    """Version installée vs dernière release (blogwatcher).
    La version installée vient de la gateway API (côté container), pas d'un
    subprocess openclaw --version qui pourrait bloquer."""
    installed = ""
    # Version installée récupérée côté container via gateway API
    # (health_collector.py → _openclaw_gateway → session_status)
    # On ne lance plus `openclaw --version` ici pour éviter la contention.

    latest = ""
    blogwatcher_bin = os.environ.get(
        "BLOGWATCHER_BIN",
        os.path.expanduser("~/go/bin/blogwatcher"),
    )
    blog_out, _, rc2 = run(
        f'{blogwatcher_bin} articles -b "OpenClaw Releases" --all 2>/dev/null',
        timeout=10,
    )
    if rc2 == 0 and blog_out:
        for line in blog_out.splitlines():
            # Skip beta/rc releases — find first stable version line
            if "beta" in line.lower() or "-rc" in line.lower():
                continue
            m = re.search(r'(\d{4}\.\d+\.\d+)', line)
            if m:
                latest = m.group(1)
                break

    up_to_date = (installed == latest) if installed and latest else None
    return {
        "installed":  installed or "unknown",
        "latest":     latest or "unknown",
        "up_to_date": up_to_date,
    }


def collect_openclaw_doctor():
    out, err, code = run("openclaw doctor --yes 2>&1", timeout=60)
    combined = out or err
    lines = [l for l in combined.splitlines() if l.strip()]
    filtered = lines

    has_error = any(
        kw in l.lower() for l in filtered
        for kw in ("error", "✗", "failed", "critical")
    )
    has_warn  = any(
        kw in l.lower() for l in filtered
        for kw in ("warn", "⚠", "warning")
    )
    if code != 0 and has_error:
        status = "error"
    elif has_warn:
        status = "warn"
    else:
        status = "ok"

    # ── Extraction structurée ──
    raw_text = "\n".join(filtered)

    # Messaging channel status + latence (Matrix or Mattermost)
    chan_m = re.search(r'(Matrix|Mattermost):\s*(\w+)(?:\s*\([^)]*\))?\s*\((\d+\w+)\)', raw_text)
    matrix = {"channel": chan_m.group(1), "status": chan_m.group(2), "latency": chan_m.group(3)} if chan_m else None

    # Agents listés
    agents_m = re.search(r'Agents:\s*(.+)', raw_text)
    agents = [a.strip() for a in agents_m.group(1).split(",")] if agents_m else []

    # Heartbeat
    heartbeat_m = re.search(r'Heartbeat interval:\s*(\S+)\s*\((\w+)\)', raw_text)
    heartbeat = {"interval": heartbeat_m.group(1), "agent": heartbeat_m.group(2)} if heartbeat_m else None

    # Sessions store
    session_m = re.search(r'Session store.*?(\d+)\s*entr', raw_text)
    sessions_count = int(session_m.group(1)) if session_m else None
    # Recent session activity lines
    session_activity = re.findall(r'-\s+(agent:\S+)\s+\((\S+\s+ago)\)', raw_text)

    # Plugin errors
    errors_m = re.search(r'Errors:\s*(\d+)', raw_text)
    plugin_errors = int(errors_m.group(1)) if errors_m else 0

    # Blocked by allowlist
    blocked_m = re.search(r'Blocked by allowlist:\s*(\d+)', raw_text)
    skills_blocked = int(blocked_m.group(1)) if blocked_m else 0

    # Memory plugin
    memory_m = re.search(r'No active memory plugin', raw_text)
    memory_status = "inactive" if memory_m else "active"

    # Plugin compat warnings
    compat_warnings = re.findall(r'([\w-]+)\s+still uses legacy\s+(\w+)', raw_text)

    return {
        "output":          filtered if filtered else ["✅ All clear"],
        "exit_code":       code,
        "status":          status,
        "matrix":          matrix,
        "agents":          agents,
        "heartbeat":       heartbeat,
        "sessions_count":  sessions_count,
        "session_activity": [{"name": a[0], "ago": a[1]} for a in session_activity[:5]],
        "plugin_errors":   plugin_errors,
        "skills_blocked":  skills_blocked,
        "memory_status":   memory_status,
        "compat_warnings": [{"plugin": c[0], "hook": c[1]} for c in compat_warnings],
    }


def collect_openclaw_security():
    out, err, code = run("openclaw security audit 2>&1", timeout=60)
    combined = out or err
    lines = [l for l in combined.splitlines() if l.strip()]
    filtered = lines

    # ── Summary counts ──
    summary_m = re.search(r'(\d+)\s*critical.*?(\d+)\s*warn.*?(\d+)\s*info', "\n".join(filtered))
    summary = {
        "critical": int(summary_m.group(1)) if summary_m else 0,
        "warn":     int(summary_m.group(2)) if summary_m else 0,
        "info":     int(summary_m.group(3)) if summary_m else 0,
    }

    # ── Extract structured warnings with severity (CRITICAL/WARN/INFO sections) ──
    warnings = []
    current_warn = None
    current_severity = "warn"  # default if no section header
    SECTION_HEADERS = {"CRITICAL": "critical", "WARN": "warn", "INFO": "info"}
    TOPIC_PREFIXES = ("gateway.", "tools.", "summary.", "models.", "plugins.",
                      "hooks.", "agents.", "skills.", "exec.")
    # Heuristique : une ligne "key: value" courte (= statut, pas un warning)
    # Ex : "tools.elevated: enabled", "hooks.webhooks: disabled", "browser control: enabled"
    _STATUS_LINE_RE = re.compile(r'^[\w.\s]+:\s*\S+(\s+\S+)?\s*$')

    def _is_status_detail(line):
        """Detecte les lignes 'key: value' qui sont des details, pas des warnings."""
        if ":" not in line:
            return False
        # Doit avoir un format 'key: value' avec value courte (1-2 mots)
        parts = line.split(":", 1)
        if len(parts) != 2:
            return False
        value = parts[1].strip()
        # value courte (max 4 mots) = statut, pas un message descriptif
        return len(value.split()) <= 4 and not value.endswith(".")

    for line in filtered:
        stripped = line.strip()
        if not stripped:
            continue
        # Section header
        if stripped in SECTION_HEADERS:
            if current_warn:
                warnings.append(current_warn)
                current_warn = None
            current_severity = SECTION_HEADERS[stripped]
            continue
        # Topic line (starts a new warning) — sauf si c'est une status line dans INFO
        is_topic_prefix = any(stripped.startswith(p) for p in TOPIC_PREFIXES)
        is_status_in_info = (current_severity == "info" and _is_status_detail(stripped))

        if is_topic_prefix and not is_status_in_info:
            if current_warn:
                warnings.append(current_warn)
            parts = stripped.split(" ", 1)
            wid = parts[0]
            msg = parts[1] if len(parts) > 1 else stripped
            current_warn = {
                "id": wid,
                "message": msg if msg else stripped,
                "fix": "",
                "severity": current_severity,
            }
        elif stripped.startswith("Fix:") and current_warn:
            current_warn["fix"] = stripped[4:].strip()
        # Continuation line OR status detail in INFO — append to message
        elif current_warn and not current_warn.get("fix"):
            extra = stripped[:200]
            sep = " · " if is_status_in_info else " "
            if len(current_warn["message"]) < 400:
                current_warn["message"] += sep + extra
    if current_warn:
        warnings.append(current_warn)

    # ── Attack surface ──
    attack_surface = {}
    for line in filtered:
        if "groups:" in line and "open=" in line:
            am = re.search(r'open=(\d+).*allowlist=(\d+)', line)
            if am:
                attack_surface["groups_open"] = int(am.group(1))
                attack_surface["groups_allowlist"] = int(am.group(2))
        if "trust model:" in line:
            attack_surface["trust_model"] = line.split("trust model:")[-1].strip()

    issues = [
        l for l in filtered
        if any(kw in l.lower() for kw in ("warn", "error", "✗", "⚠", "warning", "failed"))
    ]
    if code != 0 and any("error" in l.lower() for l in issues):
        status = "error"
    elif issues:
        status = "warn"
    else:
        status = "ok"

    return {
        "output":         filtered if filtered else ["✅ All clear"],
        "issues":         issues,
        "exit_code":      code,
        "status":         status,
        "summary":        summary,
        "warnings":       warnings,
        "attack_surface": attack_surface,
    }


def collect_services(docker_data=None):
    docker_containers = (docker_data or {}).get("containers", [])
    result = {}
    for svc in SERVICES_TO_CHECK:
        out, _, code = run(f"systemctl is-active {svc} 2>/dev/null")
        status = out.strip() if out.strip() else ("active" if code == 0 else "inactive")
        if status != "active" and docker_containers:
            for c in docker_containers:
                cname  = (c.get("name", "") or "").lower()
                cimage = (c.get("image", "") or "").lower()
                if svc.lower() in cname or svc.lower() in cimage:
                    if c.get("state") == "running":
                        status = "active (docker)"
                    break
        result[svc] = status
    return result


def collect_wireguard():
    """Parse `wg show all dump` (tab-separated). Tente sans sudo puis avec."""
    out, err, rc = run("wg show all dump 2>/dev/null || sudo wg show all dump 2>/dev/null")
    if not out:
        return {"interfaces": [], "total_peers": 0, "connected_peers": 0, "status": "unavailable"}

    interfaces = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 5:             # ligne interface
            iface = parts[0]
            interfaces[iface] = {"port": parts[3], "peers": []}
        elif len(parts) == 9:           # ligne peer
            iface, pubkey, _, endpoint, allowed_ips, last_hs, rx, tx, _ = parts
            if iface not in interfaces:
                interfaces[iface] = {"port": "?", "peers": []}
            hs_epoch = int(last_hs)
            hs = int(time.time()) - hs_epoch if hs_epoch > 0 else 0
            hs_str = ("never" if hs_epoch == 0
                      else f"{hs}s ago" if hs < 180
                      else f"{hs//60}min ago" if hs < 3600
                      else f"{hs//3600}h ago" if hs < 86400
                      else f"{hs//86400}d ago" if hs < 604800
                      else f"{hs//604800}w ago" if hs < 2592000
                      else "stale")
            interfaces[iface]["peers"].append({
                "pubkey_short": pubkey[:8] + "…",
                "endpoint":     endpoint if endpoint != "(none)" else None,
                "allowed_ips":  allowed_ips,
                "handshake":    hs_str,
                "rx_mb":        round(int(rx) / 1_048_576, 1),
                "tx_mb":        round(int(tx) / 1_048_576, 1),
                "connected":    0 < hs < 300,
            })

    iface_list = []
    for name, d in interfaces.items():
        connected = sum(1 for p in d["peers"] if p["connected"])
        iface_list.append({
            "name":            name,
            "port":            d["port"],
            "peers_total":     len(d["peers"]),
            "peers_connected": connected,
            "peers":           d["peers"],
        })

    # Peer summary par catégorie
    all_peers = [p for i in iface_list for p in i["peers"]]
    active = sum(1 for p in all_peers if p["connected"])                      # < 5min
    recent = sum(1 for p in all_peers if not p["connected"] and p["handshake"] not in ("never", "stale"))
    stale  = sum(1 for p in all_peers if p["handshake"] in ("never", "stale"))

    return {
        "interfaces":      iface_list,
        "total_peers":     sum(i["peers_total"]     for i in iface_list),
        "connected_peers": sum(i["peers_connected"] for i in iface_list),
        "peer_summary":    {"active": active, "recent": recent, "stale": stale},
        "status":          "ok" if iface_list else "unavailable",
    }


def collect_github_auth():
    out, err, rc = run("gh auth status 2>&1", timeout=10)
    combined = (out + "\n" + err).strip()
    account, token_src = "", ""
    for line in combined.splitlines():
        m = re.search(r'account (\S+)', line)
        if m:
            account = m.group(1).strip("()")
        m2 = re.search(r'\(([A-Z_]+)\)', line)
        if m2:
            token_src = m2.group(1)
    # Last push per repo (deduplicated)
    last_pushes = []
    if rc == 0 and account:
        push_out, _, push_rc = run(
            f'gh api "/users/{account}/events?per_page=30" '
            f'--jq \'[.[] | select(.type=="PushEvent") | '
            f'{{repo: .repo.name, at: .created_at}}]\'',
            timeout=10,
        )
        if push_rc == 0 and push_out:
            try:
                raw = json.loads(push_out)
                seen_repos = set()
                for p in raw:
                    repo = p.get("repo", "")
                    if repo not in seen_repos:
                        seen_repos.add(repo)
                        last_pushes.append(p)
                    if len(last_pushes) >= 5:
                        break
            except json.JSONDecodeError:
                pass

    return {
        "authenticated": rc == 0,
        "account":       account,
        "token_source":  token_src,
        "last_pushes":   last_pushes,
        "status":        "ok" if rc == 0 else "error",
    }


def collect_tmux():
    out, err, rc = run("tmux ls 2>/dev/null")
    if rc != 0 or not out:
        return {"sessions": [], "count": 0, "status": "ok"}
    # Get session creation times
    created_out, _, _ = run("tmux list-sessions -F '#{session_name} #{session_created}' 2>/dev/null")
    created_map = {}
    import time as _time
    now = _time.time()
    for cl in (created_out or "").splitlines():
        parts = cl.strip().split()
        if len(parts) == 2:
            try:
                age = now - int(parts[1])
                if age < 3600:
                    dur = f"{int(age//60)}min"
                elif age < 86400:
                    dur = f"{int(age//3600)}h{int((age%3600)//60)}min"
                else:
                    dur = f"{int(age//86400)}d {int((age%86400)//3600)}h"
                created_map[parts[0]] = dur
            except ValueError:
                pass
    sessions = []
    for line in out.splitlines():
        if ":" not in line:
            continue
        name = line.split(":")[0].strip()
        m = re.search(r'(\d+) windows?', line)
        windows = int(m.group(1)) if m else 0
        sessions.append({
            "name": name, "windows": windows,
            "attached": "(attached)" in line,
            "duration": created_map.get(name, ""),
        })
    return {"sessions": sessions, "count": len(sessions), "status": "ok"}


def collect_fail2ban(period_days=30):
    _, _, rc = run("sudo fail2ban-client ping", timeout=5)
    if rc != 0:
        return {"status": "unavailable", "jails": {}, "bans_period": 0, "period_days": period_days}
    jails = {}
    for jail in ["sshd", "nginx-http-auth", "caddy"]:
        out, _, rc2 = run(f"sudo fail2ban-client status {jail}", timeout=10)
        if rc2 != 0 or not out:
            continue
        m_banned = re.search(r'Currently banned:\s+(\d+)', out)
        m_total  = re.search(r'Total banned:\s+(\d+)', out)
        jails[jail] = {
            "banned": int(m_banned.group(1)) if m_banned else 0,
            "total_banned": int(m_total.group(1)) if m_total else 0,
            "active": "Currently failed" in out,
        }

    # Parse fail2ban.log over the period for ban/unban counts
    cutoff = datetime.now(timezone.utc) - timedelta(days=period_days)
    log_lines = _read_logs_period("/var/log/fail2ban.log", days=period_days)
    bans_period = 0
    unbans_period = 0
    daily_bans = {}
    for l in log_lines:
        ts_dt = _parse_syslog_ts(l)
        if ts_dt and ts_dt < cutoff:
            continue
        if "[NOTICE]" in l and "Ban " in l and "Unban" not in l:
            bans_period += 1
            if ts_dt:
                day_key = ts_dt.strftime("%Y-%m-%d")
                daily_bans[day_key] = daily_bans.get(day_key, 0) + 1
        elif "[NOTICE]" in l and "Unban " in l:
            unbans_period += 1

    return {
        "status": "active" if jails else "inactive",
        "jails": jails,
        "bans_period": bans_period,
        "unbans_period": unbans_period,
        "daily_bans": daily_bans,
        "period_days": period_days,
    }


def _read_logs_period(base_path, days=30):
    """Read all log lines from base_path + rotated copies (.1, .2.gz...) for the period.
    Returns a list of (timestamp_iso, line) tuples sorted by timestamp ascending."""
    import gzip
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    files = []
    # Newest first: base, .1, .2.gz, .3.gz...
    for suffix in ("", ".1", ".2.gz", ".3.gz", ".4.gz"):
        p = base_path + suffix
        if os.path.exists(p):
            files.append(p)
    lines = []
    for p in files:
        opener = gzip.open if p.endswith(".gz") else open
        try:
            cmd = f"sudo cat {p}" if not p.endswith(".gz") else f"sudo zcat {p}"
            data, _, _ = run(cmd, timeout=10)
            for ln in (data or "").splitlines():
                lines.append(ln)
        except Exception:
            pass
    return lines


def _parse_syslog_ts(line, year=None):
    """Parse syslog timestamp 'Apr 18 09:30:01' or ISO '2026-04-18T09:30:01' to datetime UTC."""
    # Try ISO format first
    m = re.match(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', line)
    if m:
        try:
            return datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
        except: pass
    # Syslog format: "Apr 18 09:30:01"
    m = re.match(r'^(\w{3})\s+(\d+)\s+(\d{2}):(\d{2}):(\d{2})', line)
    if m:
        try:
            mon, day, h, mn, s = m.groups()
            mons = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
            year = year or datetime.now(timezone.utc).year
            return datetime(year, mons.get(mon, 1), int(day), int(h), int(mn), int(s), tzinfo=timezone.utc)
        except: pass
    return None


def collect_ufw(period_days=30):
    out, _, rc = run("sudo ufw status verbose", timeout=10)
    enabled = rc == 0 and "Status: active" in out
    cutoff = datetime.now(timezone.utc) - timedelta(days=period_days)
    log_lines = _read_logs_period("/var/log/ufw.log", days=period_days)
    blocks = []
    ip_counts = {}
    daily_counts = {}  # {date_iso: count}
    for l in log_lines:
        if "BLOCK" not in l:
            continue
        ts_dt = _parse_syslog_ts(l)
        if ts_dt and ts_dt < cutoff:
            continue
        m_src = re.search(r'SRC=(\S+)', l)
        m_dst = re.search(r'DST=(\S+)', l)
        m_dpt = re.search(r'DPT=(\S+)', l)
        m_proto = re.search(r'PROTO=(\S+)', l)
        m_in = re.search(r'IN=(\S+)', l)
        ts = l.split(" ")[0] if l else ""
        src = m_src.group(1) if m_src else "?"
        entry = {
            "time":  ts[:19],
            "src":   src,
            "dst":   m_dst.group(1) if m_dst else "?",
            "port":  m_dpt.group(1) if m_dpt else "?",
            "proto": m_proto.group(1) if m_proto else "?",
            "iface": m_in.group(1) if m_in else "?",
            "ts_iso": ts_dt.isoformat() if ts_dt else "",
        }
        blocks.append(entry)
        ip_counts[src] = ip_counts.get(src, 0) + 1
        if ts_dt:
            day_key = ts_dt.strftime("%Y-%m-%d")
            daily_counts[day_key] = daily_counts.get(day_key, 0) + 1
    top_ips = sorted(ip_counts.items(), key=lambda x: -x[1])[:10]

    # Géolocalisation des top IPs bloquées (ip-api.com batch, gratuit, pas de clé)
    top_blocked = [{"ip": ip, "count": c} for ip, c in top_ips]
    if top_blocked:
        try:
            import urllib.request
            ips_to_lookup = [e["ip"] for e in top_blocked if e["ip"] != "?"]
            if ips_to_lookup:
                req = urllib.request.Request(
                    "http://ip-api.com/batch?fields=query,country,countryCode,isp,org",
                    data=json.dumps(ips_to_lookup).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    geo_data = json.loads(resp.read().decode())
                geo_map = {g["query"]: g for g in geo_data if isinstance(g, dict) and "query" in g}
                for entry in top_blocked:
                    g = geo_map.get(entry["ip"], {})
                    entry["country"] = g.get("countryCode", "")
                    entry["isp"] = g.get("isp", "")
        except Exception:
            pass

    auth_out, _, _ = run("sudo tail -100 /var/log/auth.log", timeout=5)
    auth_failures = [l for l in (auth_out or "").splitlines()
                     if "Failed password" in l or "Invalid user" in l]
    return {
        "enabled": enabled,
        "denies_period": len(blocks),
        "period_days": period_days,
        "daily_counts": daily_counts,
        "top_blocked_ips": top_blocked,
        "recent_blocks": blocks[-30:],
        "auth_failures": auth_failures[-20:],
    }


def collect_cron_history(period_days=30):
    """Parse OpenClaw cron run JSONL files to count success/failed per job over period."""
    runs_dir = Path.home() / ".openclaw" / "cron" / "runs"
    if not runs_dir.is_dir():
        return {"jobs": {}, "period_days": period_days, "total_ok": 0, "total_error": 0}

    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=period_days)).timestamp() * 1000)
    jobs = {}  # {job_id: {"ok": N, "error": N, "skip": N, "last_run_ms": ts, "last_status": str}}

    for f in runs_dir.glob("*.jsonl"):
        try:
            with open(f) as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                        ts = d.get("ts", 0) or d.get("runAtMs", 0)
                        if ts < cutoff_ms:
                            continue
                        if d.get("action") != "finished":
                            continue
                        jid = d.get("jobId", "")
                        if not jid:
                            continue
                        status = d.get("status", "unknown")
                        if jid not in jobs:
                            jobs[jid] = {"ok": 0, "error": 0, "skip": 0, "other": 0,
                                         "last_run_ms": 0, "last_status": ""}
                        if status == "ok":
                            jobs[jid]["ok"] += 1
                        elif status in ("error", "failed"):
                            jobs[jid]["error"] += 1
                        elif status == "skip":
                            jobs[jid]["skip"] += 1
                        else:
                            jobs[jid]["other"] += 1
                        if ts > jobs[jid]["last_run_ms"]:
                            jobs[jid]["last_run_ms"] = ts
                            jobs[jid]["last_status"] = status
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            continue

    total_ok    = sum(j["ok"]    for j in jobs.values())
    total_error = sum(j["error"] for j in jobs.values())
    total_skip  = sum(j["skip"]  for j in jobs.values())
    return {
        "jobs": jobs,
        "period_days": period_days,
        "total_ok": total_ok,
        "total_error": total_error,
        "total_skip": total_skip,
    }


def collect_adguard():
    """Collect AdGuard Home stats + blocked queries for VPN clients."""
    url  = os.environ.get("ADGUARD_URL", "http://10.8.0.1:3000")
    user = os.environ.get("ADGUARD_USER", "")
    pwd  = os.environ.get("ADGUARD_PASSWORD", "")
    if not user or not pwd:
        return {"status": "not_configured"}
    try:
        import urllib.request, base64
        auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}"}

        # Stats globales
        req_stats = urllib.request.Request(
            f"{url}/control/stats", headers=headers
        )
        with urllib.request.urlopen(req_stats, timeout=5) as resp:
            stats = json.loads(resp.read().decode())

        # Requêtes bloquées récentes
        req_log = urllib.request.Request(
            f"{url}/control/querylog?limit=200&response_status=blocked",
            headers=headers,
        )
        with urllib.request.urlopen(req_log, timeout=5) as resp:
            log_data = json.loads(resp.read().decode())

        # Filtrer par clients VPN (10.8.0.x)
        blocked_vpn = []
        for entry in log_data.get("data", []):
            client = entry.get("client", "")
            if client.startswith("10.8.0.") or client.startswith("10.0.0."):
                blocked_vpn.append({
                    "client":  client,
                    "domain":  entry.get("question", {}).get("name", "?"),
                    "reason":  entry.get("reason", ""),
                    "time":    entry.get("time", "")[:19],
                })
            if len(blocked_vpn) >= 50:
                break

        return {
            "status":              "ok",
            "dns_queries":         stats.get("num_dns_queries", 0),
            "blocked_filtering":   stats.get("num_blocked_filtering", 0),
            "blocked_pct":         round(stats.get("num_blocked_filtering", 0) /
                                        max(stats.get("num_dns_queries", 1), 1) * 100, 1),
            "avg_processing_time": stats.get("avg_processing_time", 0),
            "blocked_vpn_clients": blocked_vpn,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


def collect_ssh_sessions():
    out, _, rc = run("w -h", timeout=5)
    sessions = []
    if rc == 0:
        for line in out.splitlines():
            p = line.split()
            if len(p) >= 5 and p[1].startswith("pts"):
                sessions.append({"user": p[0], "tty": p[1], "from": p[2],
                                  "login_at": p[3], "idle": p[4]})
    return {"sessions": sessions, "count": len(sessions)}


def collect_claude_code():
    """Collect Claude Code CLI stats from ~/.claude/"""
    claude_home = Path.home() / ".claude"
    result = {"status": "unavailable"}

    stats_path = claude_home / "stats-cache.json"
    if stats_path.exists():
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            result.update({
                "status":          "ok",
                "last_computed":   stats.get("lastComputedDate"),
                "model_usage":     stats.get("modelUsage", {}),
                "daily_tokens":    stats.get("dailyModelTokens", [])[-14:],
                "total_sessions":  stats.get("totalSessions", 0),
                "total_messages":  stats.get("totalMessages", 0),
            })
        except Exception:
            pass

    creds_path = claude_home / ".credentials.json"
    if creds_path.exists():
        try:
            creds = json.loads(creds_path.read_text(encoding="utf-8"))
            oauth = creds.get("claudeAiOauth", {})
            result.update({
                "subscription_type": oauth.get("subscriptionType"),
                "rate_limit_tier":   oauth.get("rateLimitTier"),
            })
        except Exception:
            pass

    return result


# ── Security warning classification ───────────────────────────────────────────

OPENCLAW_JSON = Path.home() / ".openclaw" / "openclaw.json"
EXEC_APPROVALS_JSON = Path.home() / ".openclaw" / "exec-approvals.json"


def _classify_security_warnings(doctor, security):
    """Classify doctor/security warnings against actual config conditions."""
    # Read current config
    try:
        config = json.loads(OPENCLAW_JSON.read_text(encoding="utf-8"))
    except Exception:
        config = {}
    try:
        approvals = json.loads(EXEC_APPROVALS_JSON.read_text(encoding="utf-8"))
    except Exception:
        approvals = {}

    # Extract protection conditions
    gw = config.get("gateway", {})
    gw_loopback = gw.get("mode", "local") == "local" or gw.get("bind", "") in ("loopback", "127.0.0.1", "localhost", "")
    matrix = config.get("channels", {}).get("matrix", {})
    matrix_allowlist = matrix.get("groupPolicy", "") == "allowlist"
    matrix_users = matrix.get("groupAllowFrom", [])
    matrix_single_user = len(matrix_users) <= 1
    auto_join = matrix.get("autoJoin", "") == "always"
    agents_cfg = approvals.get("agents", {})
    comm_deny = agents_cfg.get("comm", {}).get("security", "deny") == "deny"
    web_deny = agents_cfg.get("web", {}).get("security", "deny") == "deny"

    conditions = {
        "gateway_loopback": gw_loopback,
        "matrix_allowlist": matrix_allowlist,
        "matrix_single_user": matrix_single_user,
        "comm_exec_deny": comm_deny,
        "web_exec_deny": web_deny,
    }

    danger = []
    warning = []
    silenced = []

    all_warnings = security.get("warnings", []) + doctor.get("compat_warnings", [])

    for w in all_warnings:
        msg = w.get("message", "") if isinstance(w, dict) else str(w)
        if not msg.strip():
            continue  # skip empty warnings
        fix = w.get("fix", "") if isinstance(w, dict) else ""
        wid = w.get("id", "") if isinstance(w, dict) else ""
        entry = {"message": msg[:200], "fix": fix[:200], "id": wid}

        # --- Classification rules ---

        # Full exec trust
        if "exec trust" in msg.lower() or "exec.security" in msg.lower():
            if not gw_loopback:
                entry["reason"] = "Gateway exposed to network"
                danger.append(entry)
            elif not matrix_allowlist or not matrix_single_user:
                entry["reason"] = "Matrix not restricted to single user"
                warning.append(entry)
            elif not comm_deny or not web_deny:
                entry["reason"] = "comm/web have exec access"
                warning.append(entry)
            else:
                entry["reason"] = "Loopback + allowlist + comm/web deny"
                silenced.append(entry)

        # autoAllowSkills
        elif "autoallowskills" in msg.lower() or "auto_allow_skills" in msg.lower():
            if not gw_loopback:
                entry["reason"] = "Gateway exposed — skills could be exploited"
                danger.append(entry)
            else:
                entry["reason"] = "Local install, skills controlled manually"
                silenced.append(entry)

        # Multi-user heuristic
        elif "multi" in msg.lower() and "user" in msg.lower():
            if not matrix_allowlist:
                entry["reason"] = "Matrix policy is not allowlist"
                danger.append(entry)
            elif not matrix_single_user:
                entry["reason"] = f"Multiple users in allowFrom: {matrix_users}"
                warning.append(entry)
            else:
                entry["reason"] = "Single-user allowlist active"
                silenced.append(entry)

        # Weak/smaller models
        elif "smaller" in msg.lower() or "weak" in msg.lower() or "susceptible" in msg.lower():
            # Check if weak models are primary on agents with exec
            silenced.append({**entry, "reason": "Weak models on fallback/comm only (no external inputs)"})

        # Sandbox off
        elif "sandbox" in msg.lower():
            if not comm_deny or not web_deny:
                entry["reason"] = "Sandbox off AND exec enabled on comm/web"
                warning.append(entry)
            else:
                entry["reason"] = "comm/web exec denied — sandbox irrelevant"
                silenced.append(entry)

        # exec broader than policy
        elif "broader" in msg.lower() and "exec" in msg.lower():
            agent_name = ""
            for a in ("comm", "web", "siyuan", "main"):
                if a in msg.lower():
                    agent_name = a
                    break
            agent_sec = agents_cfg.get(agent_name, {}).get("security", "deny")
            if agent_sec == "deny":
                entry["reason"] = f"{agent_name} exec-approvals=deny overrides"
                silenced.append(entry)
            elif agent_sec == "full":
                entry["reason"] = f"{agent_name} has full exec AND broader policy"
                warning.append(entry)
            else:
                entry["reason"] = f"{agent_name} exec-approvals={agent_sec}"
                warning.append(entry)

        # Gateway probe failed
        elif "gateway" in msg.lower() and ("probe" in msg.lower() or "failed" in msg.lower()):
            entry["reason"] = "Transient — check if persistent"
            warning.append(entry)

        # Legacy hook (cognee)
        elif "legacy" in msg.lower() or "cognee" in msg.lower():
            entry["reason"] = "Cosmetic deprecation warning"
            silenced.append(entry)

        # Permissive tool policy / elevated
        elif "permissive" in msg.lower() or "elevated" in msg.lower() or "tool policy" in msg.lower():
            if gw_loopback and matrix_allowlist:
                entry["reason"] = "Loopback + allowlist — acceptable"
                silenced.append(entry)
            else:
                entry["reason"] = "Permissive tools with exposed gateway"
                warning.append(entry)

        # Attack surface summary (info)
        elif "attack surface" in msg.lower():
            silenced.append({**entry, "reason": "Informational summary"})

        # Extension plugins
        elif "extension plugin" in msg.lower() or "enabled plugin" in msg.lower():
            silenced.append({**entry, "reason": "Locally installed plugins"})

        # Catch-all: unknown warnings default to warning
        else:
            entry["reason"] = "Unclassified — review manually"
            warning.append(entry)

    return {
        "classified_at": datetime.now(timezone.utc).isoformat(),
        "danger": danger,
        "warning": warning,
        "silenced": silenced,
        "conditions": conditions,
        "summary": {
            "danger": len(danger),
            "warning": len(warning),
            "silenced": len(silenced),
        },
    }


# ── Assemblage ─────────────────────────────────────────────────────────────────

def _need_audit():
    """Check if heavy audit should run: flag file OR > 24h since last audit."""
    if REFRESH_FLAG.exists():
        return True
    try:
        data = json.loads(SIDECAR_PATH.read_text(encoding="utf-8"))
        audit_at = data.get("meta", {}).get("audit_at", "")
        if not audit_at:
            return True  # never run
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(audit_at)).total_seconds()
        return age >= 86400  # > 24h
    except Exception:
        return True  # can't read → run


def _reuse_from_sidecar(*keys):
    """Reuse fields from previous sidecar JSON (for skipped heavy collections)."""
    try:
        data = json.loads(SIDECAR_PATH.read_text(encoding="utf-8"))
        return {k: data.get(k) for k in keys}
    except Exception:
        return {k: None for k in keys}


def build_report():
    # ── Collectes légères (chaque run, 30min) ─────────────────────────────────
    meta         = collect_meta()
    resources    = collect_resources()
    docker       = collect_docker()
    apt          = collect_apt()
    services     = collect_services(docker_data=docker)
    wireguard    = collect_wireguard()
    tmux         = collect_tmux()
    claude_code  = collect_claude_code()
    adguard      = collect_adguard()
    fail2ban     = collect_fail2ban(period_days=30)
    ufw          = collect_ufw(period_days=30)
    cron_history = collect_cron_history(period_days=30)
    ssh_sessions = collect_ssh_sessions()

    # ── Collectes lourdes (1x/jour OU bouton refresh) ─────────────────────────
    run_audit = _need_audit()

    if run_audit:
        network          = collect_network()
        watchtower       = collect_watchtower()
        github_cli       = collect_github_auth()
        openclaw_version = collect_openclaw_version()
        doctor           = collect_openclaw_doctor()
        security         = collect_openclaw_security()
        classified       = _classify_security_warnings(doctor, security)
        meta["audit_at"] = datetime.now(timezone.utc).isoformat()
        # Consume flag
        try:
            REFRESH_FLAG.unlink(missing_ok=True)
        except Exception:
            pass
    else:
        # Reuse previous audit results
        cached = _reuse_from_sidecar(
            "network", "watchtower", "github_cli", "openclaw_version",
            "openclaw_doctor", "openclaw_security", "security_classified",
        )
        network          = cached["network"] or {}
        watchtower       = cached["watchtower"] or {"raw_last50": [], "updates": [], "errors": [], "image_updates": []}
        github_cli       = cached["github_cli"] or {}
        openclaw_version = cached["openclaw_version"] or {}
        doctor           = cached["openclaw_doctor"] or {"output": ["Audit pas encore exécuté"], "status": "ok", "exit_code": 0}
        security         = cached["openclaw_security"] or {"output": ["Audit pas encore exécuté"], "issues": [], "status": "ok", "exit_code": 0}
        classified       = cached["security_classified"] or {}
        # Preserve audit timestamp from previous run
        try:
            prev = json.loads(SIDECAR_PATH.read_text(encoding="utf-8"))
            meta["audit_at"] = prev.get("meta", {}).get("audit_at", "")
        except Exception:
            pass

    # Calcul du statut global
    if classified.get("danger"):
        security["status"] = "error"
    elif classified.get("warning"):
        security["status"] = "warn"

    global_status = _calc_status(
        doctor.get("status", "ok"),
        security.get("status", "ok"),
        wireguard.get("status", "ok"),
        "warn" if watchtower.get("errors") else "ok",
        "warn" if apt.get("upgradable_count", 0) > 10 else "ok",
    )
    meta["global_status"] = global_status

    return {
        "meta":              meta,
        "resources":         resources,
        "network":           network,
        "docker":            docker,
        "watchtower":        watchtower,
        "apt":               apt,
        "openclaw_version":  openclaw_version,
        "openclaw_doctor":   doctor,
        "openclaw_security": security,
        "security_classified": classified,
        "services":          services,
        "wireguard":         wireguard,
        "github_cli":        github_cli,
        "tmux":              tmux,
        "claude_code":       claude_code,
        "adguard":           adguard,
        "fail2ban":          fail2ban,
        "ufw":               ufw,
        "cron_history":      cron_history,
        "ssh_sessions":      ssh_sessions,
    }


def format_markdown(data):
    """Rapport markdown pour stdout (Matrix/notifications)."""
    meta = data["meta"]
    res  = data["resources"]
    gs   = {"ok": "✅", "warn": "⚠️", "error": "❌"}.get(meta["global_status"], "")
    lines = [
        f"## {gs} Health Check — {meta['hostname']} — {meta['collected_at'][:16]} UTC",
        "",
        f"### 🖥 Système",
        f"- Uptime : {meta['uptime']}  |  Kernel : {meta['kernel']}",
        f"- Load : {res.get('load_1m')} / {res.get('load_5m')} / {res.get('load_15m')}",
        f"- RAM : {res.get('ram_used_mb')} / {res.get('ram_total_mb')} MB ({res.get('ram_pct')}%)",
        f"- Disque / : {res.get('disk_used')} / {res.get('disk_total')} ({res.get('disk_pct')})",
        "",
    ]

    d = data["docker"]
    lines += [
        f"### 🐳 Docker — {d['running']} running / {d['stopped']} stopped",
    ]
    for c in d["containers"]:
        icon = "🟢" if c["state"] == "running" else "🔴"
        lines.append(f"  {icon} {c['name']} — {c['status']}")
    lines.append("")

    wt = data["watchtower"]
    lines += [f"### 🔄 Watchtower — {len(wt['updates'])} updates"]
    for u in wt["updates"]:
        lines.append(f"  - {u}")
    if wt["errors"]:
        lines.append(f"  ⚠️ {len(wt['errors'])} erreur(s)")
    lines.append("")

    a = data["apt"]
    lines += [
        f"### 📦 APT — {a['install_count']} installs, {a['upgrade_count']} upgrades (log récent) · {a['upgradable_count']} upgradable",
    ]
    for pkg in a["upgradable"][:10]:
        lines.append(f"  - {pkg}")
    lines.append("")

    doc = data["openclaw_doctor"]
    lines += [f"### 🔒 OpenClaw Doctor — {doc['status'].upper()}"]
    for l in doc["output"][:10]:
        lines.append(f"  {l}")
    lines.append("")

    sec = data["openclaw_security"]
    lines += [f"### 🛡 Security Audit — {sec['status'].upper()}"]
    for l in (sec["issues"] or sec["output"])[:10]:
        lines.append(f"  {l}")
    lines.append("")

    svcs = data["services"]
    lines += ["### ⚙️ Services"]
    for svc, st in svcs.items():
        icon = "🟢" if st == "active" else "🔴"
        lines.append(f"  {icon} {svc} : {st}")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    data = build_report()

    # Stdout markdown (Matrix / notification)
    print(format_markdown(data))

    # JSON sidecar pour le dashboard (écriture atomique)
    try:
        SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
        import tempfile
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(SIDECAR_PATH.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, str(SIDECAR_PATH))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"[WARN] Impossible d'écrire le sidecar JSON : {e}", flush=True)
