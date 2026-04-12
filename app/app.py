#!/usr/bin/env python3
"""
app.py — AI Cost Monitor. Compact cards, selectable period, autonomous health.
"""

import os, json, time, threading, sqlite3
from datetime import datetime, timedelta
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import psutil
from collectors import collect_all
from health_collector import collect_system, HEALTH_CACHE, _openclaw_gateway

st.set_page_config(page_title="AI Monitor", page_icon="📊", layout="wide")

METRICS_DB = "/data/metrics.db"

def init_metrics_db():
    conn = sqlite3.connect(METRICS_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS metrics (
        ts TEXT NOT NULL, cpu REAL, mem_pct REAL,
        net_in REAL, net_out REAL, wg_peers INTEGER
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON metrics(ts)")
    conn.commit()
    conn.close()

try:
    init_metrics_db()
except Exception:
    pass


def _fmt_tokens(n):
    """Format token count: 1234 -> '1.2K', 123456 -> '123K', 1234567 -> '1.2M'."""
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return str(n)

# ── CSS ────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
* { font-size: 13px !important; }
h1 { font-size: 1.3rem !important; margin-bottom: 0.1rem !important; }
.card {
    background: #161b27;
    border: 1px solid #2a3045;
    border-radius: 10px;
    padding: 14px 16px 12px;
    margin-bottom: 10px;
}
.card-header {
    font-size: 0.72rem !important;
    font-weight: 700;
    color: #718096;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 10px;
    display: flex;
    align-items: center;
    gap: 6px;
}
.big-num { font-size: 1.5rem !important; font-weight: 700; color: #e2e8f0; line-height: 1.1; }
.sub-num { font-size: 0.72rem !important; color: #718096; margin-top: 2px; }
.green  { color: #48bb78 !important; }
.yellow { color: #ecc94b !important; }
.red    { color: #fc8181 !important; }
.grey   { color: #4a5568 !important; }
.badge  { display:inline-block; background:#2d3748; border-radius:4px; padding:1px 5px;
          font-size:0.68rem !important; color:#a0aec0; margin-right:3px; }
.badge-green { background:#1a3a2a; color:#48bb78 !important; }
.badge-blue  { background:#1a2a3a; color:#63b3ed !important; }
.model-row { display:flex; justify-content:space-between; align-items:center;
             padding:4px 0; border-bottom:1px solid #1e2535; font-size:0.72rem !important; }
.model-row:last-child { border-bottom:none; }
.divider { border:none; border-top:1px solid #2a3045; margin:8px 0; }
.prog-bar-bg { background:#2d3748; border-radius:3px; height:5px; margin:6px 0; }
.prog-bar-fill { background:#48bb78; height:5px; border-radius:3px; }
.info-row { display:flex; gap:24px; margin-bottom:8px; }
.info-block { flex:1; }
.nums-row { display:flex; gap:20px; flex-wrap:wrap; margin-bottom:8px; }
.stTabs [data-baseweb="tab"] { font-size:0.82rem !important; }
[data-testid="stMetricValue"] { font-size:1rem !important; }
</style>
""", unsafe_allow_html=True)


# ── Live metrics collector (10s → SQLite) ─────────────────────────────────────
def live_collector():
    _prev_net = None
    while True:
        try:
            cpu = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory().percent
            net = psutil.net_io_counters()
            net_in = net_out = 0.0
            if _prev_net:
                net_in  = (net.bytes_recv - _prev_net.bytes_recv) / 1_048_576
                net_out = (net.bytes_sent - _prev_net.bytes_sent) / 1_048_576
            _prev_net = net
            wg_peers = 0
            try:
                s = json.loads(open("/data/host-health.json").read())
                wg_peers = s.get("wireguard", {}).get("connected_peers", 0)
            except Exception:
                pass
            conn = sqlite3.connect(METRICS_DB)
            conn.execute(
                "INSERT INTO metrics VALUES (?,?,?,?,?,?)",
                (datetime.utcnow().isoformat(), cpu, mem, net_in, net_out, wg_peers)
            )
            conn.execute("DELETE FROM metrics WHERE ts < datetime('now','-2 hours')")
            conn.commit()
            conn.close()
        except Exception:
            pass
        time.sleep(9)


def collect_webmin():
    url  = os.environ.get("WEBMIN_URL", "https://host.docker.internal:10000/xmlrpc.cgi")
    user = os.environ.get("WEBMIN_USER", "")
    pwd  = os.environ.get("WEBMIN_PASSWORD", "")
    if not user or not pwd:
        return {"status": "not_configured"}
    try:
        import xmlrpc.client, ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        transport = xmlrpc.client.SafeTransport(context=ctx)
        proxy = xmlrpc.client.ServerProxy(
            f"https://{user}:{pwd}@{url.split('://')[-1]}",
            transport=transport
        )
        sys_status  = proxy.system.status_info()
        services    = {s['name']: bool(s.get('running', 0))
                       for s in (proxy.init.list_services() or [])}
        apt_count   = len(proxy.package_updates.available_packages() or [])
        recent_logs = proxy.webmin_log.get_recent_logs(3600)[-10:]
        return {
            "status": "ok",
            "system_status": sys_status,
            "services": services,
            "apt_count": apt_count,
            "recent_logs": recent_logs,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# Global shared state (set by sidebar/init, read by workers — no st.session_state)
_g = {"refresh": 60, "webmin": {}, "gw": {}}

# One-time init: fetch gateway data before any render
if not _g["gw"]:
    try:
        _init = _openclaw_gateway()
        if _init:
            _g["gw"] = _init
    except Exception:
        pass

def _webmin_worker():
    while True:
        try:
            _g["webmin"] = collect_webmin()
        except Exception:
            pass
        time.sleep(max(_g["refresh"], 30))


def _openclaw_gw_worker():
    while True:
        try:
            result = _openclaw_gateway()
            if result is not None:
                _g["gw"] = result
        except Exception:
            pass
        time.sleep(max(_g["refresh"], 30))


# ── Background health refresh ────────────────────────────────────────────────
def _health_worker():
    while True:
        try:
            collect_system()
        except Exception:
            pass
        time.sleep(max(_g["refresh"], 30))

if "health_thread_started" not in st.session_state:
    t = threading.Thread(target=_health_worker, daemon=True)
    t.start()
    st.session_state["health_thread_started"] = True
    if not os.path.exists(HEALTH_CACHE):
        try:
            collect_system()
        except Exception:
            pass

if "live_collector_started" not in st.session_state:
    threading.Thread(target=live_collector, daemon=True).start()
    st.session_state["live_collector_started"] = True

if "webmin_thread_started" not in st.session_state:
    threading.Thread(target=_webmin_worker, daemon=True).start()
    st.session_state["webmin_thread_started"] = True

if "openclaw_gw_thread_started" not in st.session_state:
    threading.Thread(target=_openclaw_gw_worker, daemon=True).start()
    st.session_state["openclaw_gw_thread_started"] = True


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("**⚙️ Controls**")
    period = st.radio("Period", ["1d", "7d", "30d"], index=2, horizontal=True)
    period_days = {"1d": 1, "7d": 7, "30d": 30}[period]
    _backend_opts = [30, 60, 300, 1800, 3600, 43200]
    _backend_labels = {30: "30s", 60: "1min", 300: "5min", 1800: "30min", 3600: "1h", 43200: "12h"}
    backend_interval = st.selectbox("Backend interval", _backend_opts,
                                     format_func=lambda x: _backend_labels[x], index=1)
    _g["refresh"] = backend_interval
    alerts_enabled = st.checkbox("🔔 Alerts CPU>80% / Disk<20%")

    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.caption(f"UTC {datetime.utcnow().strftime('%H:%M:%S')}")


# ── Data ───────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def load(days):
    from collectors import (collect_openrouter, collect_openai,
                             collect_anthropic, collect_google,
                             collect_chatgpt_plus)
    return {
        "collected_at": datetime.utcnow().isoformat(),
        "providers": {
            "openrouter": collect_openrouter(days=days),
            "openai":     collect_openai(days=days),
            "anthropic":  collect_anthropic(days=days),
            "google":     collect_google(days=days),
        },
        "chatgpt_plus": collect_chatgpt_plus(),
    }

data = load(period_days)
p = data.get("providers", {})

# Health data (shared between AI Costs and System Health tabs)
_health = {}
if os.path.exists(HEALTH_CACHE):
    try:
        with open(HEALTH_CACHE) as f:
            _health = json.load(f)
    except Exception:
        pass


@st.cache_data(ttl=10)
def load_metrics(n=60):
    try:
        conn = sqlite3.connect(METRICS_DB)
        rows = conn.execute(
            "SELECT ts,cpu,mem_pct,net_in,net_out,wg_peers FROM metrics ORDER BY ts DESC LIMIT ?", (n,)
        ).fetchall()
        conn.close()
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["ts", "cpu", "mem_pct", "net_in", "net_out", "wg_peers"])
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        return df.sort_values("ts")
    except Exception:
        return None


# ── Header ─────────────────────────────────────────────────────────────────────
st.markdown("# 📊 AI Cost Monitor")
st.caption(f"Local VPS · period: **{period}** · {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")

tabs = st.tabs(["💰 AI Costs", "🖥️ System Health", "🗂 Raw"])


# ══════════════════════════════════════════════════════════════════
# TAB 1 — AI Costs
# ══════════════════════════════════════════════════════════════════
with tabs[0]:

    # ── Usage OpenClaw (gateway live) — coût par modèle ──────────
    st.markdown(f"##### Usage OpenClaw ({period})")
    gw_live = _g["gw"] or {}
    gw_all_sessions = gw_live.get("sessions", {}).get("active", [])

    # Filter sessions by period
    import time as _t
    _cutoff_ms = int((_t.time() - period_days * 86400) * 1000)
    gw_filtered = [s for s in gw_all_sessions if s.get("updated_at_ms", 0) >= _cutoff_ms]
    # For real period filtering, recompute by_model from filtered active sessions
    gw_by_model = {}
    for s in gw_filtered:
        m = s.get("model", "unknown")
        gw_by_model.setdefault(m, {"tokens": 0, "cost_usd": 0, "count": 0, "provider": "?"})
        gw_by_model[m]["tokens"] += s.get("tokens", 0)
        gw_by_model[m]["cost_usd"] += s.get("cost_usd", 0)
        gw_by_model[m]["count"] += 1
    # Apply provider from gateway data
    gw_prov = gw_live.get("sessions", {}).get("by_model", {})
    for m in gw_by_model:
        if m in gw_prov:
            gw_by_model[m]["provider"] = gw_prov[m].get("provider", "?")

    if gw_by_model:
        # Sort models by cost descending
        sorted_models = sorted(gw_by_model.items(), key=lambda x: x[1].get("cost_usd", 0), reverse=True)
        total_cost = sum(v["cost_usd"] for v in gw_by_model.values())
        total_tokens = sum(v["tokens"] for v in gw_by_model.values())
        total_sessions = sum(v["count"] for v in gw_by_model.values())

        _PROV_ICONS = {"openrouter": "🔀", "openai": "🤖", "anthropic": "🧠", "google": "🌐",
                       "claude-cli": "🧠", "openai-codex": "🤖"}
        model_rows = ""
        for m, v in sorted_models:
            cost = v.get("cost_usd", 0)
            tokens = v.get("tokens", 0)
            count = v.get("count", 0)
            prov = v.get("provider", "?")
            prov_icon = _PROV_ICONS.get(prov, "")
            prov_badge = f'<span class="badge" style="font-size:10px!important">{prov_icon} {prov}</span>' if prov != "?" else ""
            # claude-cli and openai-codex: $0 cost = included in subscription
            cost_badge = (
                '<span class="badge" style="color:#48bb78">included</span>'
                if cost == 0 and prov in ("claude-cli", "openai-codex")
                else f'<span class="badge badge-green">${cost:.4f}</span>'
            )
            model_rows += (
                f'<div class="model-row">'
                f'<span style="max-width:40%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:inline-block">{m}</span>'
                f'<span>'
                f'{prov_badge}'
                f'<span class="badge">{count} sess</span>'
                f'<span class="badge">{tokens:,} tok</span>'
                f'{cost_badge}'
                f'</span></div>'
            )
        clr = "yellow" if total_cost > 1 else "green"
        st.markdown(
            f'<div class="card">'
            f'<div class="card-header">🦞 OpenClaw Sessions'
            f'<span class="badge badge-green" style="margin-left:auto;font-size:10px!important">LIVE</span></div>'
            f'<div class="nums-row">'
            f'<div class="info-block"><div class="big-num {clr}">${total_cost:.4f}</div><div class="sub-num">total cost</div></div>'
            f'<div class="info-block"><div class="big-num">{total_sessions}</div><div class="sub-num">sessions</div></div>'
            f'<div class="info-block"><div class="big-num grey">{total_tokens:,}</div><div class="sub-num">tokens</div></div>'
            f'</div>'
            f'{model_rows}'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.caption("OpenClaw gateway unavailable — using JSONL logs")

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Comptes fournisseurs (APIs directes) ──────────────────────
    st.markdown("##### Provider Accounts")

    # ── Detail row 1 : OpenRouter + OpenAI ────────────────────────
    col_or, col_oai = st.columns(2)

    # ── OpenRouter ─────────────────────────────────────────────────
    with col_or:
        d = p.get("openrouter", {})
        if d.get("status") == "ok":
            total = d.get("total_credits_usd", 0) or 0
            used  = d.get("total_usage_usd", 0) or 0
            rem   = d.get("remaining_usd", 0) or 0
            pct   = min(used / total * 100, 100) if total else 0
            clr   = "green" if rem > 5 else ("yellow" if rem > 1 else "red")
            by_m  = d.get("by_model") or {}

            # Lignes par modèle (depuis logs OpenClaw)
            model_rows = ""
            for m, v in sorted(by_m.items(), key=lambda x: x[1].get("cost_usd", 0), reverse=True):
                model_rows += (
                    f'<div class="model-row">'
                    f'<span style="max-width:50%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:inline-block">{m}</span>'
                    f'<span>'
                    f'<span class="badge">in {_fmt_tokens(v["input_tokens"])}</span>'
                    f'<span class="badge">out {_fmt_tokens(v["output_tokens"])}</span>'
                    f'<span class="badge badge-green">${v["cost_usd"]:.4f}</span>'
                    f'</span></div>'
                )
            model_section = (
                f'<hr class="divider"><div class="sub-num grey" style="margin-bottom:5px">BY MODEL (via logs)</div>{model_rows}'
                if model_rows else ""
            )

            st.markdown(
                f'<div class="card">'
                f'<div class="card-header">🔀 OpenRouter</div>'
                f'<div class="nums-row">'
                f'<div class="info-block"><div class="big-num {clr}">${rem:.4f}</div><div class="sub-num">restant</div></div>'
                f'<div class="info-block"><div class="big-num">${used:.4f}</div><div class="sub-num">used (total)</div></div>'
                f'<div class="info-block"><div class="big-num grey">${total:.2f}</div><div class="sub-num">total credits</div></div>'
                f'</div>'
                f'<div class="prog-bar-bg"><div class="prog-bar-fill" style="width:{pct:.1f}%"></div></div>'
                f'<div class="sub-num grey">{pct:.2f}% consumed</div>'
                f'{model_section}'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div class="card">'
                f'<div class="card-header">🔀 OpenRouter</div>'
                f'<div class="sub-num grey">{d.get("status","?")} — {d.get("error","")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # ── OpenAI (API + ChatGPT Plus OAuth — merged card) ─────────
    with col_oai:
        d = p.get("openai", {})
        cgpt = data.get("chatgpt_plus", {})

        # --- API billing section ---
        api_html = ""
        oai_badge = ""
        if d.get("status") == "ok":
            usage  = d.get("total_usage_usd_30d", 0) or 0
            org    = d.get("org", "")
            daily  = d.get("daily", [])
            by_m   = d.get("by_model") or {}
            pre_rem  = d.get("prepaid_remaining_usd")
            pre_tot  = d.get("prepaid_total_usd")
            pre_used = d.get("prepaid_used_usd")

            oai_period_label = period
            if period_days < 30 and daily:
                cutoff = (datetime.utcnow() - timedelta(days=period_days)).strftime("%Y-%m-%d")
                daily = [x for x in daily if x["date"] >= cutoff]
            if daily:
                usage_period = sum(x["cost_usd"] for x in daily)
            elif usage > 0:
                usage_period = usage
                oai_period_label = f"~{period}"
            else:
                usage_period = 0
                oai_period_label = period

            if org:
                oai_badge = f' <span class="badge" style="margin-left:auto;color:#667eea">{org}</span>'

            if pre_rem is not None and pre_tot:
                pre_pct = min((pre_used or 0) / pre_tot * 100, 100) if pre_tot else 0
                pre_clr = "green" if pre_rem > 5 else ("yellow" if pre_rem > 1 else "red")
                api_html = (
                    f'<div class="nums-row">'
                    f'<div class="info-block"><div class="big-num {pre_clr}">${pre_rem:.4f}</div><div class="sub-num">remaining credits</div></div>'
                    f'<div class="info-block"><div class="big-num">${pre_used:.4f}</div><div class="sub-num">used (prepaid)</div></div>'
                    f'<div class="info-block"><div class="big-num grey">${pre_tot:.2f}</div><div class="sub-num">total prepaid</div></div>'
                    f'</div>'
                    f'<div class="prog-bar-bg"><div class="prog-bar-fill" style="width:{pre_pct:.1f}%"></div></div>'
                    f'<div class="sub-num grey">{pre_pct:.2f}% consumed · ${usage_period:.4f} used ({oai_period_label})</div>'
                )
            else:
                api_html = (
                    f'<div class="nums-row">'
                    f'<div class="info-block"><div class="big-num yellow">${usage_period:.4f}</div><div class="sub-num">used ({oai_period_label})</div></div>'
                    f'<div class="info-block"><div class="big-num grey">postpaid</div><div class="sub-num">no prepaid balance</div></div>'
                    f'</div>'
                )

            model_rows = ""
            for m, v in sorted(by_m.items(), key=lambda x: x[1].get("cost_usd", 0), reverse=True):
                inp   = v.get("input_tokens", 0)
                out   = v.get("output_tokens", 0)
                reqs  = v.get("requests", 0)
                cost  = v.get("cost_usd", 0)
                model_rows += (
                    f'<div class="model-row">'
                    f'<span style="max-width:40%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:inline-block">{m}</span>'
                    f'<span>'
                    f'<span class="badge">in {_fmt_tokens(inp)}</span>'
                    f'<span class="badge">out {_fmt_tokens(out)}</span>'
                    f'<span class="badge">{reqs}r</span>'
                    f'<span class="badge badge-green">${cost:.4f}</span>'
                    f'</span></div>'
                )
            if model_rows:
                api_html += f'<hr class="divider"><div class="sub-num grey" style="margin-bottom:5px">BY MODEL ({oai_period_label}) — estimated cost</div>{model_rows}'
        else:
            api_html = f'<div class="sub-num grey">{d.get("error", "API key required (OPENAI_API_KEY)")}</div>'

        # --- ChatGPT Plus section ---
        cgpt_html = ""
        if cgpt.get("status") == "ok":
            plan = cgpt.get("plan", "?").upper()
            if plan and plan != "UNKNOWN":
                oai_badge = f' <span class="badge badge-green" style="margin-left:auto">{plan}</span>'
            rl = cgpt.get("rate_limits", {})
            rl_rows = ""
            if rl:
                req_rem = int(rl.get("remaining-requests", 0))
                req_lim = int(rl.get("limit-requests", 1))
                tok_rem = int(rl.get("remaining-tokens", 0))
                tok_lim = int(rl.get("limit-tokens", 1))
                reset_req = rl.get("reset-requests", "")
                req_pct = round((req_lim - req_rem) / req_lim * 100, 1) if req_lim else 0
                tok_pct = round((tok_lim - tok_rem) / tok_lim * 100, 1) if tok_lim else 0
                _clr = lambda pct: "#48bb78" if pct < 80 else ("#ecc94b" if pct < 95 else "#fc8181")
                rl_rows = (
                    f'<div class="model-row"><span>Requests</span>'
                    f'<span><span class="badge">{req_rem:,} / {req_lim:,}</span>'
                    f'<span class="badge">reset {reset_req}</span></span></div>'
                    f'<div class="prog-bar-bg"><div class="prog-bar-fill" style="width:{req_pct}%;background:{_clr(req_pct)}"></div></div>'
                    f'<div class="model-row" style="margin-top:4px"><span>Tokens</span>'
                    f'<span><span class="badge">{tok_rem:,} / {tok_lim:,}</span></span></div>'
                    f'<div class="prog-bar-bg"><div class="prog-bar-fill" style="width:{tok_pct}%;background:{_clr(tok_pct)}"></div></div>'
                )
            name = cgpt.get("name", "")
            name_line = f'<div class="sub-num grey" style="margin-top:4px">OAuth · {name}</div>' if name else ""
            cgpt_html = (
                f'<hr class="divider">'
                f'<div class="sub-num grey" style="margin-bottom:4px">ChatGPT Plus — Rate Limits</div>'
                f'{rl_rows}'
                f'{name_line}'
            )

        st.markdown(
            f'<div class="card">'
            f'<div class="card-header">🤖 OpenAI{oai_badge}</div>'
            f'{api_html}'
            f'{cgpt_html}'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Detail row 2 : Anthropic + Google ─────────────────────────
    col_ant, col_goo = st.columns(2)

    # ── Anthropic (API billing + Claude Code — une seule card) ────
    with col_ant:
        d        = p.get("anthropic", {})
        b_status = d.get("billing_status", "unavailable")
        b_rem    = d.get("credits_remaining_usd")
        b_tot    = d.get("credits_total_usd")
        b_used   = d.get("credits_used_usd")
        b_src    = d.get("billing_source", "none")

        # Billing section
        billing_html = ""
        if b_status == "ok" and b_rem is not None and b_tot:
            b_pct = min((b_used or 0) / b_tot * 100, 100) if b_tot else 0
            b_clr = "green" if b_rem > 5 else ("yellow" if b_rem > 1 else "red")
            src_label = "Console API" if b_src == "console_key" else "Claude CLI"
            billing_html = (
                f'<div class="nums-row">'
                f'<div class="info-block"><div class="big-num {b_clr}">${b_rem:.2f}</div><div class="sub-num">remaining credits</div></div>'
                f'<div class="info-block"><div class="big-num">${b_used:.2f}</div><div class="sub-num">used</div></div>'
                f'<div class="info-block"><div class="big-num grey">${b_tot:.2f}</div><div class="sub-num">total</div></div>'
                f'</div>'
                f'<div class="prog-bar-bg"><div class="prog-bar-fill" style="width:{b_pct:.1f}%"></div></div>'
                f'<div class="sub-num grey">{b_pct:.2f}% consumed · {src_label}</div>'
            )
        else:
            billing_html = '<div class="sub-num grey">API key required (ANTHROPIC_API_KEY_MONITORING)</div>'

        # Claude Code section
        cc = _health.get("claude_code", {})
        cc_html = ""
        cc_badge = ""
        if cc.get("status") == "ok":
            sub_type = (cc.get("subscription_type") or "?").upper()
            tier     = cc.get("rate_limit_tier", "")
            tier_short = tier.replace("default_claude_", "").replace("_", " ") if tier else ""
            sessions = cc.get("total_sessions", 0)
            messages = cc.get("total_messages", 0)
            model_usage = cc.get("model_usage", {})
            cc_badge = f' <span class="badge badge-green" style="margin-left:auto">{sub_type}</span>'
            cc_rows = ""
            for m, v in model_usage.items():
                inp = v.get("inputTokens", 0) + v.get("cacheReadInputTokens", 0) + v.get("cacheCreationInputTokens", 0)
                out = v.get("outputTokens", 0)
                cc_rows += (
                    f'<div class="model-row"><span>{m}</span><span>'
                    f'<span class="badge">in {_fmt_tokens(inp)}</span>'
                    f'<span class="badge">out {_fmt_tokens(out)}</span>'
                    f'</span></div>'
                )
            last_computed = cc.get("last_computed", "")
            cc_html = (
                f'<hr class="divider">'
                f'<div class="sub-num grey" style="margin-bottom:4px">Claude Code</div>'
                f'<div class="nums-row">'
                f'<div class="info-block"><div class="big-num">{sessions}</div><div class="sub-num">sessions</div></div>'
                f'<div class="info-block"><div class="big-num">{messages}</div><div class="sub-num">messages</div></div>'
                f'<div class="info-block"><div class="big-num grey">{tier_short}</div><div class="sub-num">tier</div></div>'
                f'</div>'
                f'{cc_rows}'
                f'<div class="sub-num grey" style="margin-top:4px">Local CLI stats · updated {last_computed}</div>'
            )

        # Claude-cli section (Max subscription — subprocess sessions)
        cli_data = (_g["gw"] or {}).get("claude_cli", {})
        cli_html = ""
        if cli_data:
            msg_h = cli_data.get("messages_this_hour", 0)
            limit_h = cli_data.get("estimated_limit_hour", 60)
            model = cli_data.get("current_model", "?")
            today_msg = cli_data.get("today_messages", 0)
            today_tok = cli_data.get("today_tokens", 0)
            today_cost = cli_data.get("today_cost_estimated", 0)
            cli_sess = cli_data.get("cli_sessions", [])

            # Rate limit bar
            rate_pct = min(round(msg_h / limit_h * 100, 1), 100) if limit_h else 0
            _clr = lambda pct: "#48bb78" if pct < 70 else ("#ecc94b" if pct < 90 else "#fc8181")

            cli_html = (
                f'<hr class="divider">'
                f'<div class="sub-num grey" style="margin-bottom:4px">Claude-cli (Max subscription)</div>'
                f'<div class="model-row"><span>Rate limit ({model})</span>'
                f'<span><span class="badge">{msg_h} / ~{limit_h} msg/h</span></span></div>'
                f'<div class="prog-bar-bg"><div class="prog-bar-fill" style="width:{rate_pct}%;background:{_clr(rate_pct)}"></div></div>'
                f'<div class="model-row" style="margin-top:4px"><span>Today</span>'
                f'<span><span class="badge">{today_msg} msg</span>'
                f'<span class="badge">{today_tok:,} tok</span>'
                f'<span class="badge" style="color:#48bb78">~${today_cost:.4f}</span></span></div>'
            )

            # Session list
            if cli_sess:
                sess_rows = ""
                for s in cli_sess[:5]:
                    from datetime import datetime as _dt, timezone as _tz
                    ts = _dt.fromtimestamp(s["started_at_ms"] / 1000, tz=_tz.utc).strftime("%H:%M") if s["started_at_ms"] else "?"
                    sess_rows += (
                        f'<div class="model-row"><span>{s["agent"]}:{s["session_id"]}</span>'
                        f'<span><span class="badge">{s["status"]}</span>'
                        f'<span class="badge">{s["runtime_s"]}s</span>'
                        f'<span class="badge">{s["tokens"]:,} tok</span>'
                        f'<span class="badge">{ts}</span></span></div>'
                    )
                cli_html += f'<div class="sub-num grey" style="margin-top:6px">Subprocess sessions</div>{sess_rows}'

        st.markdown(
            f'<div class="card">'
            f'<div class="card-header">🧠 Anthropic{cc_badge}</div>'
            f'{billing_html}'
            f'{cc_html}'
            f'{cli_html}'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ── Google Gemini ───────────────────────────────────────────────
    with col_goo:
        d      = p.get("google", {})
        cost   = d.get("estimated_cost_usd", 0) or 0
        by_m   = d.get("by_model") or {}
        s      = d.get("status", "?")
        b_stat = d.get("billing_status", "no_sa_key")
        b_tot  = d.get("billing_total_usd")
        b_svc  = d.get("billing_by_service") or {}

        # Section billing GCP (si disponible)
        if b_stat == "ok" and b_tot is not None:
            gcp_rows = "".join(
                f'<div class="model-row"><span>{svc}</span><span class="badge badge-green">${c:.4f}</span></div>'
                for svc, c in b_svc.items()
            )
            billing_section = (
                f'<div class="nums-row">'
                f'<div class="info-block"><div class="big-num yellow">${b_tot:.4f}</div><div class="sub-num">actual GCP cost (month)</div></div>'
                f'</div>'
                f'{"<hr class=\"divider\">" + gcp_rows if gcp_rows else ""}'
                f'<hr class="divider">'
            )
        else:
            billing_section = ""

        model_rows = "".join(
            f'<div class="model-row">'
            f'<span>{m}</span>'
            f'<span>'
            f'<span class="badge">in {v["input_tokens"]:,}</span>'
            f'<span class="badge">out {v["output_tokens"]:,}</span>'
            f'<span class="badge badge-green">${v.get("cost_usd",0):.4f}</span>'
            f'</span></div>'
            for m, v in sorted(by_m.items(), key=lambda x: x[1].get("cost_usd", 0), reverse=True)
        )
        cost_color = "yellow" if cost > 0 else "grey"
        if not model_rows and not by_m:
            note = "Google models routed via OpenRouter — costs included in OpenRouter"
        else:
            note = "Estimated from OpenClaw logs · basic API key"
        if b_stat not in ("ok", "no_sa_key"):
            note += f" · GCP billing: {b_stat}"

        usage_section = (
            f'<div class="nums-row">'
            f'<div class="info-block"><div class="big-num {cost_color}">${cost:.4f}</div><div class="sub-num">estimated ({period})</div></div>'
            f'</div>'
        )
        model_divider = '<hr class="divider">' if model_rows else ""

        st.markdown(
            f'<div class="card">'
            f'<div class="card-header">🌐 Google Gemini</div>'
            f'{billing_section}'
            f'{usage_section}'
            f'{model_divider}{model_rows}'
            f'<div class="sub-num grey" style="margin-top:6px">ℹ️ {note}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )


# ══════════════════════════════════════════════════════════════════
# TAB 2 — System Health
# ══════════════════════════════════════════════════════════════════
with tabs[1]:

    health = _health

    ts          = health.get("collected_at", "")
    sidecar_ts  = health.get("sidecar_at", "")
    sidecar_stale = health.get("sidecar_stale", False)
    gs          = health.get("global_status", "")
    gs_badge    = {"ok": "✅", "warn": "⚠️", "error": "❌"}.get(gs, "")
    if ts:
        stale_note = f" · sidecar {'⚠️ stale' if sidecar_stale else 'OK'} ({sidecar_ts})" if sidecar_ts else ""
        st.caption(
            f"{gs_badge} System metrics : {ts} UTC · 5min thread{stale_note}"
        )
    else:
        st.caption("Collecte en cours…")

    # ── ROW A — Graphs live ──────────────────────────────────────────────────
    df_metrics = load_metrics(60)
    if df_metrics is not None and len(df_metrics) > 2:
        cg1, cg2 = st.columns(2)
        GRAPH_LAYOUT = dict(height=180, margin=dict(l=20, r=10, t=30, b=20),
                            paper_bgcolor="rgba(0,0,0,0)",
                            plot_bgcolor="rgba(14,17,23,0.5)",
                            font_color="#e2e8f0", showlegend=True)
        with cg1:
            fig = go.Figure(layout={**GRAPH_LAYOUT, "title": "CPU & RAM %"})
            fig.add_trace(go.Scatter(x=df_metrics.ts, y=df_metrics.cpu, name="CPU", line=dict(color="#4CAF50", width=1.5)))
            fig.add_trace(go.Scatter(x=df_metrics.ts, y=df_metrics.mem_pct, name="RAM", line=dict(color="#ff9800", width=1.5)))
            fig.update_yaxes(range=[0, 100])
            st.plotly_chart(fig, use_container_width=True)
        with cg2:
            fig2 = go.Figure(layout={**GRAPH_LAYOUT, "title": "Network I/O (MB/10s)"})
            fig2.add_trace(go.Scatter(x=df_metrics.ts, y=df_metrics.net_in, name="In", fill="tozeroy", line=dict(color="#2196F3", width=1)))
            fig2.add_trace(go.Scatter(x=df_metrics.ts, y=df_metrics.net_out, name="Out", fill="tozeroy", line=dict(color="#f44336", width=1)))
            st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("Collecte en cours... graphs disponibles dans ~30s")

    # ── ROW B — Docker Stats top 5 ──────────────────────────────────────────
    docker_stats = _health.get("docker_stats", [])
    if docker_stats:
        df_stats = pd.DataFrame(docker_stats)
        with st.expander("🐳 Docker Stats live — top 5 CPU", expanded=True):
            st.dataframe(df_stats, use_container_width=True, hide_index=True)

    # ── ROW C — Sécurité (Fail2ban | UFW/SSH) ───────────────────────────────
    cs1, cs2 = st.columns(2)
    with cs1:
        st.markdown("#### 🛡️ Fail2ban")
        fb = _health.get("fail2ban", {})
        if fb.get("status") == "unavailable":
            st.warning("Fail2ban not accessible (check sudoers on host)")
        elif fb.get("status") == "inactive":
            st.error("Fail2ban INACTIVE")
            st.warning("Sur le host : `sudo systemctl start fail2ban`")
        elif fb.get("status") == "active":
            st.success("Fail2ban active")
            for jail, info in fb.get("jails", {}).items():
                banned = info.get("banned", 0)
                color = "#fc8181" if banned > 0 else "#48bb78"
                st.markdown(f'<span style="color:{color}">● {jail}: {banned} banned</span>', unsafe_allow_html=True)
        else:
            st.caption("No data — run daily-health-check.py")
    with cs2:
        st.markdown("#### 🔥 UFW / SSH")
        ufw = _health.get("ufw", {})
        ssh_s = _health.get("ssh_sessions", {})
        if ufw:
            # Filter out internal/container IPs (Docker, LAN, sandbox)
            _INTERNAL_PREFIXES = ("172.", "10.", "192.168.", "127.")
            def _is_external(ip):
                return not any(ip.startswith(p) for p in _INTERNAL_PREFIXES)

            all_top = ufw.get("top_blocked_ips", [])
            all_recent = ufw.get("recent_blocks", [])
            ext_top = [x for x in all_top if _is_external(x.get("ip", ""))][:5]
            ext_recent = [x for x in all_recent if _is_external(x.get("src", ""))]
            ext_denies = sum(x.get("count", 0) for x in ext_top)

            st.metric("External attacks/h", ext_denies)

            # Top 5 external IPs
            if ext_top:
                st.dataframe(
                    pd.DataFrame(ext_top).rename(columns={"ip": "IP", "count": "Blocks"}),
                    use_container_width=True, hide_index=True,
                )

            # Show more — full log in same section
            if all_recent:
                with st.expander(f"Full log ({len(all_recent)} entries)"):
                    st.dataframe(
                        pd.DataFrame(all_recent).rename(columns={
                            "time": "Time", "src": "Source", "dst": "Dest",
                            "port": "Port", "proto": "Proto", "iface": "Interface",
                        }),
                        use_container_width=True, hide_index=True,
                    )

            auth_fails = ufw.get("auth_failures", [])
            if auth_fails:
                with st.expander(f"auth.log failures ({len(auth_fails)})"):
                    st.code("\n".join(auth_fails), language=None)
        sessions = ssh_s.get("sessions", [])
        if sessions:
            st.dataframe(pd.DataFrame(sessions), use_container_width=True, hide_index=True)
        else:
            st.caption("No active SSH sessions")

    # ── ROW D — Webmin Live ──────────────────────────────────────────────────
    wm = _g["webmin"]
    if wm.get("status") == "not_configured":
        st.caption("Webmin: set WEBMIN_USER + WEBMIN_PASSWORD in .env to enable")
    elif wm.get("status") == "ok":
        with st.expander("🖥️ Webmin Live", expanded=False):
            wm_cols = st.columns(3)
            sys_s = wm.get("system_status", {})
            wm_cols[0].metric("Webmin CPU", f"{sys_s.get('cpu', '?')}%")
            wm_cols[1].metric("Services UP", sum(1 for v in wm.get("services", {}).values() if v))
            wm_cols[2].metric("APT Updates", wm.get("apt_count", "?"))
            df_svc = pd.DataFrame([
                {"Service": k, "Status": "🟢" if v else "🔴"}
                for k, v in wm.get("services", {}).items()
            ])
            st.dataframe(df_svc, use_container_width=True, hide_index=True)
            for log in wm.get("recent_logs", []):
                st.caption(f"{log.get('time', '')} · {log.get('user', '')} → {log.get('module', '')}")
    elif wm.get("status") == "error":
        st.warning(f"Webmin: {wm.get('error', 'unreachable')}")

    if health:
        c1, c2 = st.columns(2)
        with c1:
            # Système card
            sys_rows = (
                f'<div class="model-row"><span>Uptime</span><span><b>{health.get("uptime","N/A")}</b></span></div>'
                f'<div class="model-row"><span>Since</span><span>{health.get("uptime_since","")}</span></div>'
                f'<div class="model-row"><span>Load avg</span><span>{health.get("load","")}</span></div>'
                f'<div class="model-row"><span>CPU cores</span><span>{health.get("cpu_cores","")}</span></div>'
                f'<div class="model-row"><span>CPU %</span><span>{health.get("cpu_percent","")}</span></div>'
            )
            md = health.get("memory_detail", {})
            if md:
                ram_pct = md.get("ram_pct", 0)
                ram_clr = "green" if ram_pct < 70 else ("yellow" if ram_pct < 90 else "red")
                sys_rows += (
                    f'<div class="model-row"><span>RAM</span><span class="{ram_clr}">{md.get("ram_used","?")} / {md.get("ram_total","?")} ({ram_pct}%)</span></div>'
                    f'<div class="prog-bar-bg"><div class="prog-bar-fill" style="width:{ram_pct}%;background:{"#48bb78" if ram_pct < 70 else ("#ecc94b" if ram_pct < 90 else "#fc8181")}"></div></div>'
                )
                if md.get("swap_total"):
                    sys_rows += f'<div class="model-row"><span>Swap</span><span>{md.get("swap_used","0")} / {md.get("swap_total","0")}</span></div>'
            else:
                sys_rows += f'<div class="model-row"><span>Memory</span><span>{health.get("memory","")}</span></div>'
            disks = health.get("disks", [])
            if disks:
                for dk in disks:
                    pct_num = int(dk["pct"].rstrip("%")) if dk.get("pct") else 0
                    dk_clr = "green" if pct_num < 70 else ("yellow" if pct_num < 90 else "red")
                    sys_rows += f'<div class="model-row"><span>Disk {dk.get("mount","")}</span><span class="{dk_clr}">{dk.get("used","?")} / {dk.get("total","?")} ({dk.get("pct","?")})</span></div>'
            else:
                sys_rows += f'<div class="model-row"><span>Disk</span><span>{health.get("disk","")}</span></div>'
            # Network info
            net = health.get("network", {})
            if net:
                pub_ip = net.get("public_ip", "")
                if pub_ip:
                    sys_rows += f'<div class="model-row"><span>Public IP</span><span>{pub_ip}</span></div>'
                for iface in net.get("interfaces", []):
                    if iface["type"] in ("lan", "vpn"):
                        sys_rows += f'<div class="model-row"><span>{iface["iface"]}</span><span class="badge {"badge-green" if iface["type"]=="vpn" else "badge"}">{iface["addr"]}</span></div>'
            st.markdown(
                f'<div class="card"><div class="card-header">🖥️ System</div>{sys_rows}</div>',
                unsafe_allow_html=True,
            )

            # Apt updates card
            pkgs            = health.get("apt_updates", [])
            upgradable_cnt  = health.get("apt_upgradable_count", 0)
            upgradable_list = health.get("apt_upgradable", [])
            apt_timers      = health.get("apt_timers", {})
            apt_clr         = "yellow" if upgradable_cnt > 10 else ("yellow" if pkgs else "green")
            # Format recent dpkg lines: extract package name + version
            apt_rows = ""
            for x in pkgs[-5:]:
                parts = x.split()
                if len(parts) >= 4:
                    date_str = parts[0] if parts[0].startswith("20") else ""
                    action = next((p for p in parts if p in ("install", "upgrade", "remove")), "")
                    pkg = parts[3] if len(parts) > 3 else x
                    pkg_short = pkg.split(":")[0] if ":" in pkg else pkg
                    apt_rows += f'<div class="sub-num" style="margin-top:2px">{date_str} {action} <b>{pkg_short}</b></div>'
                else:
                    apt_rows += f'<div class="sub-num" style="margin-top:2px">{x[-60:]}</div>'
            upgradable_badge = (
                f'<span class="badge {"badge" if upgradable_cnt <= 10 else ""}" '
                f'style="{"color:#fc8181" if upgradable_cnt > 10 else ""}">'
                f'{upgradable_cnt} upgradable</span>'
                if upgradable_cnt else ""
            )
            # Timer info
            timer_row = ""
            if apt_timers.get("next_upgrade"):
                timer_row = f'<div class="sub-num" style="margin-top:4px">⏱ Next auto upgrade: {apt_timers.get("left_upgrade", "")} ({apt_timers["next_upgrade"]})</div>'
            # Upgradable packages list
            upg_rows = ""
            for pkg in upgradable_list[:8]:
                pkg_name = pkg.split("/")[0] if "/" in pkg else pkg
                upg_rows += f'<div class="sub-num" style="margin-top:1px">• {pkg_name}</div>'
            if len(upgradable_list) > 8:
                upg_rows += f'<div class="sub-num grey">… et {len(upgradable_list) - 8} more</div>'
            st.markdown(
                f'<div class="card">'
                f'<div class="card-header">📦 APT {upgradable_badge}</div>'
                f'<div class="big-num {apt_clr}">{len(pkgs)}</div>'
                f'<div class="sub-num">package(s) recently updated</div>'
                f'{apt_rows}'
                f'{"<hr class=\"divider\">" + upg_rows if upg_rows else ""}'
                f'{timer_row}'
                f'</div>',
                unsafe_allow_html=True,
            )

            # WireGuard card
            wg = health.get("wireguard", {})
            if wg.get("interfaces"):
                total_c = wg.get("connected_peers", 0)
                total_p = wg.get("total_peers", 0)
                wg_clr  = "green" if total_c == total_p and total_p > 0 else ("yellow" if total_c > 0 else "red")
                iface_rows = ""
                for iface in wg["interfaces"]:
                    c = iface["peers_connected"]; t = iface["peers_total"]
                    badge_clr = "badge-green" if c == t and t > 0 else "badge"
                    iface_rows += (
                        f'<div class="model-row">'
                        f'<span><b>{iface["name"]}</b> :{iface["port"]}</span>'
                        f'<span class="badge {badge_clr}">{c}/{t} peers</span>'
                        f'</div>'
                    )
                    for p in iface["peers"]:
                        rx = f'{p["rx_mb"]}MB↓' if p.get("rx_mb", 0) > 0 else ""
                        tx = f'{p["tx_mb"]}MB↑' if p.get("tx_mb", 0) > 0 else ""
                        ep = p.get("endpoint") or "no endpoint"
                        ep_host = ep.split(":")[0] if ep != "no endpoint" else ""
                        dot = "🟢" if p.get("connected") else "🔴"
                        hs_label = f'up {p["handshake"]}' if p.get("connected") else p["handshake"]
                        iface_rows += (
                            f'<div class="sub-num" style="margin-left:8px;margin-top:2px">'
                            f'{dot} {p["pubkey_short"]} · {ep_host} · {hs_label} · {rx} {tx}'
                            f'</div>'
                        )
                st.markdown(
                    f'<div class="card">'
                    f'<div class="card-header">🔒 WireGuard — {total_c}/{total_p} active</div>'
                    f'{iface_rows}'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        with c2:
            # Docker containers card
            containers    = health.get("docker_containers", [])
            docker_counts = health.get("docker_counts", {})
            d_total   = docker_counts.get("total",   len(containers))
            d_running = docker_counts.get("running", sum(1 for c in containers if "Up" in c.get("status","")))
            d_stopped = docker_counts.get("stopped", d_total - d_running)
            if containers or d_total > 0:
                container_rows = ""
                for c in containers:
                    icon = "🟢" if c.get("state","") == "running" or "Up" in c.get("status","") else "🔴"
                    port = f' :{c["main_port"]}' if c.get("main_port") else ""
                    container_rows += (
                        f'<div class="model-row">'
                        f'<span>{icon} {c.get("name","?")}{port}</span>'
                        f'<span class="sub-num">{str(c.get("status",""))[:35]}</span>'
                        f'</div>'
                    )
                docker_header = f'🐳 Docker — {d_running}▲ {d_stopped}▼ / {d_total} total'
                st.markdown(
                    f'<div class="card"><div class="card-header">{docker_header}</div>{container_rows}</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<div class="card">'
                    '<div class="card-header">🐳 Docker</div>'
                    '<div class="sub-num grey">Run daily-health-check.py on the host</div>'
                    '</div>',
                    unsafe_allow_html=True,
                )

            # Watchtower card
            wt         = health.get("watchtower_updates", [])
            wt_errors  = health.get("watchtower_errors", [])
            wt_source  = health.get("watchtower_source", "logs")
            wt_imgs    = health.get("wt_image_updates", [])
            wt_color   = "red" if wt_errors else ("yellow" if wt_imgs else "green")
            src_badge  = (
                '<span class="badge badge-green">API</span>'
                if wt_source == "api"
                else '<span class="badge">sidecar</span>'
            )
            err_badge  = (
                f'<span class="badge" style="color:#fc8181">{len(wt_errors)} error(s)</span>'
                if wt_errors else ""
            )
            # Image updates — show container + image name
            import re as _re
            img_rows = ""
            for u in wt_imgs[-8:]:
                ts = u.get("time", "")[-5:]  # HH:MM
                ctr = u.get("container", "?")
                img = u.get("image", "?")
                img_rows += (
                    f'<div class="model-row">'
                    f'<span>{ctr}</span>'
                    f'<span><span class="badge badge-blue">{img}</span>'
                    f'<span class="badge">{ts}</span></span>'
                    f'</div>'
                )
            # Session summaries
            wt_rows = ""
            for x in wt[-3:]:
                m = _re.search(r'msg="([^"]+)"', x)
                msg = m.group(1) if m else x[-60:]
                parts = []
                for kv in _re.findall(r'(\w+)=(\d+)', x):
                    if kv[0] in ("scanned", "updated", "failed") and kv[1] != "0":
                        parts.append(f'{kv[0]}={kv[1]}')
                extra = f' ({", ".join(parts)})' if parts else ""
                wt_rows += f'<div class="sub-num" style="margin-top:2px">{msg}{extra}</div>'
            err_rows = "".join(
                f'<div class="sub-num" style="color:#fc8181;margin-top:2px">{x[-60:]}</div>'
                for x in wt_errors[-3:]
            )
            st.markdown(
                f'<div class="card">'
                f'<div class="card-header">🔄 Watchtower {src_badge}{err_badge}</div>'
                f'<div class="big-num {wt_color}">{len(wt_imgs)}</div>'
                f'<div class="sub-num">image(s) updated</div>'
                f'{img_rows}'
                f'<hr class="divider">'
                f'{wt_rows}{err_rows}'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Services card
            services = health.get("services", {})
            if services:
                service_rows = ""
                for svc, st_ in services.items():
                    is_active = st_ in ("active", "active (docker)")
                    badge_cls = "badge-green" if is_active else "badge"
                    style = "" if is_active else "color:#fc8181"
                    docker_tag = ' <span class="badge badge-blue">docker</span>' if "(docker)" in st_ else ""
                    label = "active" if is_active else st_
                    service_rows += (
                        f'<div class="model-row">'
                        f'<span>{svc}{docker_tag}</span>'
                        f'<span class="badge {badge_cls}" style="{style}">{label}</span>'
                        f'</div>'
                    )
                st.markdown(
                    f'<div class="card"><div class="card-header">⚙️ Services</div>{service_rows}</div>',
                    unsafe_allow_html=True,
                )

            # DevTools card
            gh  = health.get("github_cli", {})
            tmx = health.get("tmux", {})
            if gh or tmx:
                dev_rows = ""
                if gh:
                    gh_clr   = "badge-green" if gh.get("authenticated") else "badge"
                    gh_label = gh.get("account") or "not authenticated"
                    gh_src   = f' · {gh["token_source"]}' if gh.get("token_source") else ""
                    dev_rows += (
                        f'<div class="model-row">'
                        f'<span>GitHub CLI</span>'
                        f'<span class="badge {gh_clr}">{gh_label}{gh_src}</span>'
                        f'</div>'
                    )
                    for push in gh.get("last_pushes", []):
                        repo = push.get("repo", "").split("/")[-1] if "/" in push.get("repo", "") else push.get("repo", "")
                        at = push.get("at", "")[:16].replace("T", " ")
                        dev_rows += f'<div class="sub-num" style="margin-left:8px;margin-top:1px">↑ {repo} · {at}</div>'
                if tmx:
                    sessions = tmx.get("sessions", [])
                    tmx_label = f'{len(sessions)} session(s)' if sessions else "none"
                    dev_rows += (
                        f'<div class="model-row">'
                        f'<span>tmux</span>'
                        f'<span class="badge">{tmx_label}</span>'
                        f'</div>'
                    )
                    for s in sessions:
                        att = " · attached" if s.get("attached") else ""
                        dur = f' · {s["duration"]}' if s.get("duration") else ""
                        dev_rows += (
                            f'<div class="sub-num" style="margin-top:2px">'
                            f'• {s["name"]} · {s["windows"]} window(s){att}{dur}'
                            f'</div>'
                        )
                st.markdown(
                    f'<div class="card"><div class="card-header">🛠️ DevTools</div>{dev_rows}</div>',
                    unsafe_allow_html=True,
                )

        # ── OpenClaw card — structured doctor/security + version + gateway live ─
        oc_ver = health.get("openclaw_version", {})
        doc_s  = health.get("doctor_structured", {})
        sec_s  = health.get("security_structured", {})
        gw     = _g["gw"] or health.get("openclaw_gateway")

        if oc_ver or doc_s or sec_s or gw:
            st.markdown("#### 🦞 OpenClaw")
            oc_cols = st.columns(2)

            with oc_cols[0]:
                # Version
                installed = oc_ver.get("installed", "?")
                latest    = oc_ver.get("latest", "?")
                up2date   = oc_ver.get("up_to_date")
                if up2date is True:
                    ver_badge = f'<span class="badge badge-green">{installed}</span>'
                elif up2date is False:
                    ver_badge = f'<span class="badge" style="color:#fc8181">{installed} → {latest}</span>'
                else:
                    ver_badge = f'<span class="badge">{installed}</span>'

                # Doctor
                doc_status = doc_s.get("status", "?")
                doc_icon = {"ok": "✅", "warn": "⚠️", "error": "❌"}.get(doc_status, "")
                matrix = doc_s.get("matrix")
                matrix_row = (
                    f'<div class="model-row"><span>Matrix</span>'
                    f'<span class="badge badge-green">{matrix["status"]} ({matrix["latency"]})</span></div>'
                ) if matrix else ""
                # Agents — prefer gateway count (live) over doctor (sidecar)
                gw_agents = gw.get("agents", {}) if gw else {}
                doc_agents = doc_s.get("agents", [])
                agents_count = gw_agents.get("count") or len(doc_agents)
                agents_row = (
                    f'<div class="model-row"><span>Agents</span>'
                    f'<span class="badge">{agents_count}</span></div>'
                ) if agents_count else ""
                hb = doc_s.get("heartbeat")
                hb_row = (
                    f'<div class="model-row"><span>Heartbeat</span>'
                    f'<span class="badge">{hb["interval"]} ({hb["agent"]})</span></div>'
                ) if hb else ""
                sess_count = doc_s.get("sessions_count")
                sess_row = (
                    f'<div class="model-row"><span>Sessions store</span>'
                    f'<span class="badge">{sess_count} entries</span></div>'
                ) if sess_count is not None else ""
                mem_status = doc_s.get("memory_status")
                mem_row = (
                    f'<div class="model-row"><span>Memory plugin</span>'
                    f'<span class="badge" style="color:#ecc94b">inactive</span></div>'
                ) if mem_status == "inactive" else ""
                # Plugin errors (only if > 0)
                pe = doc_s.get("plugin_errors", 0)
                pe_row = (
                    f'<div class="model-row"><span>Plugin errors</span>'
                    f'<span class="badge" style="color:#fc8181">{pe}</span></div>'
                ) if pe else ""
                # Skills blocked
                sb = doc_s.get("skills_blocked", 0)
                sb_row = (
                    f'<div class="model-row"><span>Skills blocked</span>'
                    f'<span class="badge" style="color:#fc8181">{sb}</span></div>'
                ) if sb else ""
                # Compat warnings
                compat = doc_s.get("compat_warnings", [])
                compat_rows = "".join(
                    f'<div class="sub-num" style="color:#ecc94b;margin-top:2px">'
                    f'⚠ {c["plugin"]}: legacy {c["hook"]}</div>'
                    for c in compat
                )

                # Gateway live rows (sessions, cost, tokens)
                gw_rows = ""
                gw_badge = ""
                if gw and gw.get("sessions"):
                    gs = gw["sessions"]
                    gw_badge = ' <span class="badge badge-green" style="font-size:10px!important">LIVE</span>'
                    gw_rows = (
                        f'<div class="model-row"><span>Live Sessions</span>'
                        f'<span class="badge badge-green">{gs["count"]}</span></div>'
                        f'<div class="model-row"><span>Total Cost</span>'
                        f'<span class="badge">${gs["total_cost_usd"]:.4f}</span></div>'
                        f'<div class="model-row"><span>Total Tokens</span>'
                        f'<span class="badge">{gs["total_tokens"]:,}</span></div>'
                    )
                    # By channel breakdown
                    for ch, cnt in gs.get("by_channel", {}).items():
                        gw_rows += (
                            f'<div class="model-row"><span>&nbsp;&nbsp;{ch}</span>'
                            f'<span class="badge">{cnt}</span></div>'
                        )

                st.markdown(
                    f'<div class="card">'
                    f'<div class="card-header">🦞 OpenClaw {ver_badge} {doc_icon}{gw_badge}</div>'
                    f'{matrix_row}{agents_row}{hb_row}{sess_row}{gw_rows}{mem_row}{pe_row}{sb_row}{compat_rows}'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # Live sessions detail (gateway)
                if gw and gw.get("sessions", {}).get("active"):
                    active_sessions = gw["sessions"]["active"]
                    with st.expander(f"Live Sessions ({len(active_sessions)})"):
                        for s in active_sessions:
                            name = s["name"]
                            if len(name) > 40:
                                name = name[:37] + "..."
                            st.caption(
                                f"  {name} · {s['model']} · {s['channel']} · "
                                f"{s['tokens']:,} tok · ${s['cost_usd']:.4f} · {s['status']} · {s['updated']}"
                            )

                # Cron jobs detail (gateway)
                if gw and gw.get("cron", {}).get("jobs"):
                    cron_jobs = gw["cron"]["jobs"]
                    all_ok = all(j["last_status"] == "ok" and j["consecutive_errors"] == 0 for j in cron_jobs)
                    cron_icon = "✅" if all_ok else "⚠️"
                    with st.expander(f"Cron Jobs ({len(cron_jobs)}) {cron_icon}"):
                        for j in cron_jobs:
                            status_icon = "✅" if j["last_status"] == "ok" else "❌"
                            err_note = f" · {j['consecutive_errors']} errors" if j["consecutive_errors"] else ""
                            st.caption(
                                f"  {status_icon} {j['name']} · {j['schedule']} ({j['tz']}) · "
                                f"{j['model']} · {j['last_duration_s']}s{err_note} · next: {j['next_run']}"
                            )

                # Session activity (sidecar doctor)
                activity = doc_s.get("session_activity", [])
                if activity:
                    with st.expander(f"Recent sessions ({len(activity)})"):
                        for a in activity:
                            short_name = a["name"].replace("agent:main:", "")
                            st.caption(f"  {short_name} — {a['ago']}")

                # Claude-cli subprocess sessions (all agents)
                _cli = (gw.get("claude_cli", {}) if gw else {})
                _all_cli = _cli.get("cli_sessions", []) + _cli.get("api_sessions", [])
                if _all_cli:
                    with st.expander(f"Claude subprocess sessions ({len(_all_cli)})"):
                        for s in _all_cli:
                            from datetime import datetime as _dt, timezone as _tz
                            ts = _dt.fromtimestamp(s["started_at_ms"] / 1000, tz=_tz.utc).strftime("%m-%d %H:%M") if s["started_at_ms"] else "?"
                            prov_label = "cli" if s["provider"] == "claude-cli" else "api"
                            cost_label = "included" if s["provider"] == "claude-cli" and s["cost_usd"] == 0 else f"${s['cost_usd']:.4f}"
                            st.caption(
                                f"  {s['agent']}:{s['session_id']} · {prov_label} · {s['model']} · "
                                f"{s['status']} · {s['runtime_s']}s · {s['tokens']:,} tok · {cost_label} · {ts}"
                            )

            with oc_cols[1]:
                # Security — classified warnings
                cl = health.get("security_classified", {})
                cl_danger = cl.get("danger", [])
                cl_warning = cl.get("warning", [])
                cl_silenced = cl.get("silenced", [])
                cl_cond = cl.get("conditions", {})

                if cl_danger:
                    sec_icon = "❌"
                elif cl_warning:
                    sec_icon = "⚠️"
                else:
                    sec_icon = "✅"

                # Conditions badges
                cond_badges = ""
                _cond_labels = {
                    "gateway_loopback": "loopback",
                    "matrix_allowlist": "allowlist",
                    "matrix_single_user": "single user",
                    "comm_exec_deny": "comm deny",
                    "web_exec_deny": "web deny",
                }
                for ck, label in _cond_labels.items():
                    ok = cl_cond.get(ck, False)
                    clr = "badge-green" if ok else ""
                    style = ' style="color:#fc8181"' if not ok else ""
                    cond_badges += f'<span class="badge {clr}"{style}>{label} {"✓" if ok else "✗"}</span> '

                st.markdown(
                    f'<div class="card">'
                    f'<div class="card-header">🛡️ Security {sec_icon}</div>'
                    f'<div class="nums-row">'
                    f'<div class="info-block"><div class="big-num" style="color:#fc8181">{len(cl_danger)}</div><div class="sub-num">danger</div></div>'
                    f'<div class="info-block"><div class="big-num" style="color:#ecc94b">{len(cl_warning)}</div><div class="sub-num">warning</div></div>'
                    f'<div class="info-block"><div class="big-num" style="color:#48bb78">{len(cl_silenced)}</div><div class="sub-num">silenced</div></div>'
                    f'</div>'
                    f'<div style="margin-top:4px">{cond_badges}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # Danger — always visible
                if cl_danger:
                    for d_item in cl_danger:
                        st.error(f"❌ {d_item['message'][:100]}\n→ {d_item['reason']}")

                # Warning — visible
                if cl_warning:
                    with st.expander(f"Active warnings ({len(cl_warning)})"):
                        for w in cl_warning:
                            st.caption(f"⚠️ {w['message'][:100]}")
                            st.caption(f"  → {w['reason']}")
                            if w.get("fix"):
                                st.caption(f"  Fix: {w['fix'][:80]}")

                # Silenced — closed expander
                if cl_silenced:
                    with st.expander(f"Baseline warnings ({len(cl_silenced)})"):
                        for s in cl_silenced:
                            st.caption(f"✅ {s['message'][:80]}")
                            st.caption(f"  → {s['reason']}")

            # Raw expanders for debug
            doctor_raw = health.get("doctor", "")
            audit_raw  = health.get("security_audit", "")
            if doctor_raw and "daily-health-check" not in doctor_raw:
                with st.expander("Raw output — Doctor"):
                    st.code(doctor_raw, language=None)
            if audit_raw and "daily-health-check" not in audit_raw:
                with st.expander("Raw output — Security Audit"):
                    st.code(audit_raw, language=None)

    else:
        st.info("System collection in progress, refreshing shortly…")


# ══════════════════════════════════════════════════════════════════
# TAB 3 — Raw
# ══════════════════════════════════════════════════════════════════
with tabs[2]:
    st.json(data)

# Auto-refresh: fragment reruns itself every 30s and triggers full rerun
# only when backend data has actually changed (no unnecessary visual flash)
_g["_last_ts"] = _g.get("_last_ts", "")

@st.fragment(run_every=timedelta(seconds=30))
def _auto_refresh():
    gw_ts = _g["gw"].get("collected_at", "")
    if gw_ts and gw_ts != _g["_last_ts"]:
        _g["_last_ts"] = gw_ts
        st.rerun(scope="app")

_auto_refresh()

