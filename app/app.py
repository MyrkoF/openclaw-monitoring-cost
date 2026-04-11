#!/usr/bin/env python3
"""
app.py — AI Cost Monitor. Cards compactes, période sélectionnable, health autonome.
"""

import os, json, time, threading, sqlite3
from datetime import datetime, timedelta
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import psutil
from collectors import collect_all
from health_collector import collect_system, HEALTH_CACHE

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


def _webmin_worker():
    while True:
        try:
            result = collect_webmin()
            st.session_state["webmin_live"] = result
        except Exception:
            pass
        time.sleep(30)


# ── Background health refresh (5 min) ─────────────────────────────────────────
def _health_worker():
    while True:
        try:
            collect_system()
        except Exception:
            pass
        time.sleep(300)

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


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("**⚙️ Controls**")
    period = st.radio("Période", ["1j", "7j", "30j"], index=2, horizontal=True)
    period_days = {"1j": 1, "7j": 7, "30j": 30}[period]
    refresh_interval = st.selectbox("Auto-refresh", [10, 30, 60],
                                     format_func=lambda x: f"{x}s", index=1)
    alerts_enabled = st.checkbox("🔔 Alertes CPU>80% / Disk<20%")

    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.caption(f"UTC {datetime.utcnow().strftime('%H:%M:%S')}")


# ── Data ───────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def load(days):
    from collectors import (collect_openrouter, collect_openai,
                             collect_anthropic, collect_google)
    return {
        "collected_at": datetime.utcnow().isoformat(),
        "providers": {
            "openrouter": collect_openrouter(days=days),
            "openai":     collect_openai(days=days),
            "anthropic":  collect_anthropic(days=days),
            "google":     collect_google(days=days),
        }
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
st.caption(f"VPS local · période : **{period}** · {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")

tabs = st.tabs(["💰 AI Costs", "🖥️ System Health", "🗂 Raw"])


# ══════════════════════════════════════════════════════════════════
# TAB 1 — AI Costs
# ══════════════════════════════════════════════════════════════════
with tabs[0]:

    # ── Summary cards row ──────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)

    def summary_card(col, icon, label, pd_):
        s = pd_.get("status", "unknown")
        with col:
            if s == "ok":
                rem  = pd_.get("remaining_usd") or pd_.get("credits_remaining_usd")
                used = (pd_.get("total_usage_usd_30d")
                        or pd_.get("total_usage_usd")
                        or pd_.get("estimated_cost_usd", 0) or 0)
                # Fallback: si pas de solde et pas de coût API, montrer coût estimé par modèle
                if not used and pd_.get("by_model"):
                    used = round(sum(v.get("cost_usd", 0) for v in pd_["by_model"].values()), 4)
                if rem is not None:
                    clr    = "green" if rem > 5 else ("yellow" if rem > 1 else "red")
                    label2 = "restant"
                    big    = f"${rem:.2f}"
                elif pd_.get("prepaid_remaining_usd") is None and pd_.get("provider") == "openai":
                    # OpenAI: pas d'accès au solde prépayé via API
                    clr    = "grey"
                    big    = "N/A"
                    label2 = "solde non dispo via API"
                else:
                    clr    = "yellow"
                    big    = f"${used:.4f}"
                    label2 = f"utilisé ({period})"
                st.markdown(
                    f'<div class="card">'
                    f'<div class="card-header">{icon} {label}</div>'
                    f'<div class="big-num {clr}">{big}</div>'
                    f'<div class="sub-num">{label2}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            elif s in ("no_logs", "no_key"):
                msg = "clé manquante" if s == "no_key" else "aucun log"
                st.markdown(
                    f'<div class="card">'
                    f'<div class="card-header">{icon} {label}</div>'
                    f'<div class="big-num grey">—</div>'
                    f'<div class="sub-num grey">{msg}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            else:
                err = (pd_.get("error", "erreur"))[:40]
                st.markdown(
                    f'<div class="card">'
                    f'<div class="card-header">{icon} {label}</div>'
                    f'<div class="big-num red">❌</div>'
                    f'<div class="sub-num red">{err}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    summary_card(c1, "🔀", "OpenRouter", p.get("openrouter", {}))
    summary_card(c2, "🤖", "OpenAI",    p.get("openai", {}))
    summary_card(c3, "🧠", "Anthropic", p.get("anthropic", {}))
    summary_card(c4, "🌐", "Google",    p.get("google", {}))

    st.markdown("<br>", unsafe_allow_html=True)

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
                f'<hr class="divider"><div class="sub-num grey" style="margin-bottom:5px">PAR MODÈLE (via logs)</div>{model_rows}'
                if model_rows else ""
            )

            st.markdown(
                f'<div class="card">'
                f'<div class="card-header">🔀 OpenRouter</div>'
                f'<div class="nums-row">'
                f'<div class="info-block"><div class="big-num {clr}">${rem:.4f}</div><div class="sub-num">restant</div></div>'
                f'<div class="info-block"><div class="big-num">${used:.4f}</div><div class="sub-num">utilisé (total)</div></div>'
                f'<div class="info-block"><div class="big-num grey">${total:.2f}</div><div class="sub-num">crédits total</div></div>'
                f'</div>'
                f'<div class="prog-bar-bg"><div class="prog-bar-fill" style="width:{pct:.1f}%"></div></div>'
                f'<div class="sub-num grey">{pct:.2f}% consommé</div>'
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

    # ── OpenAI ─────────────────────────────────────────────────────
    with col_oai:
        d = p.get("openai", {})
        if d.get("status") == "ok":
            usage  = d.get("total_usage_usd_30d", 0) or 0
            org    = d.get("org", "")
            daily  = d.get("daily", [])
            by_m   = d.get("by_model") or {}
            pre_rem  = d.get("prepaid_remaining_usd")
            pre_tot  = d.get("prepaid_total_usd")
            pre_used = d.get("prepaid_used_usd")

            if period_days < 30 and daily:
                cutoff = (datetime.utcnow() - timedelta(days=period_days)).strftime("%Y-%m-%d")
                daily = [x for x in daily if x["date"] >= cutoff]
            usage_period = sum(x["cost_usd"] for x in daily) if daily else usage

            badge_org = (
                f'<span class="badge" style="margin-left:auto;color:#667eea">{org}</span>'
                if org else ""
            )

            # Section crédits prépayés (si disponible, format identique OpenRouter)
            if pre_rem is not None and pre_tot:
                pre_pct = min((pre_used or 0) / pre_tot * 100, 100) if pre_tot else 0
                pre_clr = "green" if pre_rem > 5 else ("yellow" if pre_rem > 1 else "red")
                credits_section = (
                    f'<div class="nums-row">'
                    f'<div class="info-block"><div class="big-num {pre_clr}">${pre_rem:.4f}</div><div class="sub-num">crédits restants</div></div>'
                    f'<div class="info-block"><div class="big-num">${pre_used:.4f}</div><div class="sub-num">utilisé (prépayé)</div></div>'
                    f'<div class="info-block"><div class="big-num grey">${pre_tot:.2f}</div><div class="sub-num">total prépayé</div></div>'
                    f'</div>'
                    f'<div class="prog-bar-bg"><div class="prog-bar-fill" style="width:{pre_pct:.1f}%"></div></div>'
                    f'<div class="sub-num grey">{pre_pct:.2f}% consommé · ${usage_period:.4f} utilisé ({period})</div>'
                )
            else:
                credits_section = (
                    f'<div class="nums-row">'
                    f'<div class="info-block"><div class="big-num yellow">${usage_period:.4f}</div><div class="sub-num">utilisé ({period})</div></div>'
                    f'<div class="info-block"><div class="big-num grey">postpayé</div><div class="sub-num">pas de solde prépayé</div></div>'
                    f'</div>'
                )

            # Lignes par modèle avec coût USD estimé
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
            model_section = (
                f'<hr class="divider"><div class="sub-num grey" style="margin-bottom:5px">PAR MODÈLE (30j) — coût estimé</div>{model_rows}'
                if model_rows else '<div class="sub-num grey">Aucune donnée</div>'
            )

            st.markdown(
                f'<div class="card">'
                f'<div class="card-header">🤖 OpenAI {badge_org}</div>'
                f'{credits_section}'
                f'{model_section}'
                f'</div>',
                unsafe_allow_html=True,
            )

        else:
            st.markdown(
                f'<div class="card">'
                f'<div class="card-header">🤖 OpenAI</div>'
                f'<div class="sub-num red">{d.get("error","erreur")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Detail row 2 : Anthropic + Google ─────────────────────────
    col_ant, col_goo = st.columns(2)

    # ── Anthropic (API réelle) + Claude Code (stats locales) ─────
    with col_ant:
        d        = p.get("anthropic", {})
        b_status = d.get("billing_status", "unavailable")
        b_rem    = d.get("credits_remaining_usd")
        b_tot    = d.get("credits_total_usd")
        b_used   = d.get("credits_used_usd")
        b_src    = d.get("billing_source", "none")

        # Card Anthropic API — seulement si billing réel disponible
        if b_status == "ok" and b_rem is not None and b_tot:
            b_pct = min((b_used or 0) / b_tot * 100, 100) if b_tot else 0
            b_clr = "green" if b_rem > 5 else ("yellow" if b_rem > 1 else "red")
            src_label = "Console API" if b_src == "console_key" else "Claude CLI"
            st.markdown(
                f'<div class="card">'
                f'<div class="card-header">🧠 Anthropic API</div>'
                f'<div class="nums-row">'
                f'<div class="info-block"><div class="big-num {b_clr}">${b_rem:.2f}</div><div class="sub-num">crédits restants</div></div>'
                f'<div class="info-block"><div class="big-num">${b_used:.2f}</div><div class="sub-num">utilisé</div></div>'
                f'<div class="info-block"><div class="big-num grey">${b_tot:.2f}</div><div class="sub-num">total</div></div>'
                f'</div>'
                f'<div class="prog-bar-bg"><div class="prog-bar-fill" style="width:{b_pct:.1f}%"></div></div>'
                f'<div class="sub-num grey">{b_pct:.2f}% consommé · {src_label}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div class="card">'
                f'<div class="card-header">🧠 Anthropic API</div>'
                f'<div class="sub-num grey">Clé API requise (ANTHROPIC_API_KEY_MONITORING)</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # Card Claude Code — stats locales depuis sidecar
        cc = _health.get("claude_code", {})
        if cc.get("status") == "ok":
            sub_type = (cc.get("subscription_type") or "?").upper()
            tier     = cc.get("rate_limit_tier", "")
            tier_short = tier.replace("default_claude_", "").replace("_", " ") if tier else ""
            sessions = cc.get("total_sessions", 0)
            messages = cc.get("total_messages", 0)
            model_usage = cc.get("model_usage", {})
            cc_rows = ""
            for m, v in model_usage.items():
                inp = v.get("inputTokens", 0) + v.get("cacheReadInputTokens", 0) + v.get("cacheCreationInputTokens", 0)
                out = v.get("outputTokens", 0)
                cc_rows += (
                    f'<div class="model-row">'
                    f'<span>{m}</span>'
                    f'<span>'
                    f'<span class="badge">in {_fmt_tokens(inp)}</span>'
                    f'<span class="badge">out {_fmt_tokens(out)}</span>'
                    f'</span></div>'
                )
            last_computed = cc.get("last_computed", "")
            st.markdown(
                f'<div class="card">'
                f'<div class="card-header">🤖 Claude Code <span class="badge badge-green" style="margin-left:auto">{sub_type}</span></div>'
                f'<div class="nums-row">'
                f'<div class="info-block"><div class="big-num">{sessions}</div><div class="sub-num">sessions</div></div>'
                f'<div class="info-block"><div class="big-num">{messages}</div><div class="sub-num">messages</div></div>'
                f'<div class="info-block"><div class="big-num grey">{tier_short}</div><div class="sub-num">tier</div></div>'
                f'</div>'
                f'{cc_rows}'
                f'<div class="sub-num grey" style="margin-top:4px">Stats CLI locales · màj {last_computed}</div>'
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
                f'<div class="info-block"><div class="big-num yellow">${b_tot:.4f}</div><div class="sub-num">coût GCP réel (mois)</div></div>'
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
            note = "Modèles Google passent via OpenRouter — comptabilisés dans OpenRouter"
        else:
            note = "Estimation depuis logs OpenClaw · clé API simple"
        if b_stat not in ("ok", "no_sa_key"):
            note += f" · GCP billing: {b_stat}"

        usage_section = (
            f'<div class="nums-row">'
            f'<div class="info-block"><div class="big-num {cost_color}">${cost:.4f}</div><div class="sub-num">estimé ({period})</div></div>'
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
        stale_note = f" · sidecar {'⚠️ expiré' if sidecar_stale else 'OK'} ({sidecar_ts})" if sidecar_ts else ""
        st.caption(
            f"{gs_badge} Métriques système : {ts} UTC · thread 5 min{stale_note}"
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
            st.warning("Fail2ban non accessible (vérifier sudoers sur host)")
        elif fb.get("status") == "inactive":
            st.error("Fail2ban INACTIF")
            st.warning("Sur le host : `sudo systemctl start fail2ban`")
        elif fb.get("status") == "active":
            st.success("Fail2ban actif")
            for jail, info in fb.get("jails", {}).items():
                banned = info.get("banned", 0)
                color = "#fc8181" if banned > 0 else "#48bb78"
                st.markdown(f'<span style="color:{color}">● {jail}: {banned} banni(s)</span>', unsafe_allow_html=True)
        else:
            st.caption("No data — run daily-health-check.py")
    with cs2:
        st.markdown("#### 🔥 UFW / SSH")
        ufw = _health.get("ufw", {})
        ssh_s = _health.get("ssh_sessions", {})
        if ufw:
            st.metric("UFW Denies/h", ufw.get("denies_hour", 0))
            top_ips = ufw.get("top_blocked_ips", [])
            recent = ufw.get("recent_blocks", [])
            if top_ips or recent:
                with st.expander(f"🔍 Détails blocks UFW ({ufw.get('denies_hour', 0)} bloqués)"):
                    if top_ips:
                        st.markdown("**Top IPs bloquées**")
                        st.dataframe(
                            pd.DataFrame(top_ips).rename(columns={"ip": "IP", "count": "Blocks"}),
                            use_container_width=True, hide_index=True,
                        )
                    if recent:
                        st.markdown("**Derniers blocks**")
                        st.dataframe(
                            pd.DataFrame(recent).rename(columns={
                                "time": "Heure", "src": "Source", "dst": "Dest",
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
            st.caption("Aucune session SSH active")

    # ── ROW D — Webmin Live ──────────────────────────────────────────────────
    wm = st.session_state.get("webmin_live", {})
    if wm.get("status") == "not_configured":
        st.caption("Webmin : définir WEBMIN_USER + WEBMIN_PASSWORD dans .env pour activer")
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
                f'<div class="model-row"><span>Depuis</span><span>{health.get("uptime_since","")}</span></div>'
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
                sys_rows += f'<div class="model-row"><span>Mémoire</span><span>{health.get("memory","")}</span></div>'
            disks = health.get("disks", [])
            if disks:
                for dk in disks:
                    pct_num = int(dk["pct"].rstrip("%")) if dk.get("pct") else 0
                    dk_clr = "green" if pct_num < 70 else ("yellow" if pct_num < 90 else "red")
                    sys_rows += f'<div class="model-row"><span>Disque {dk.get("mount","")}</span><span class="{dk_clr}">{dk.get("used","?")} / {dk.get("total","?")} ({dk.get("pct","?")})</span></div>'
            else:
                sys_rows += f'<div class="model-row"><span>Disque</span><span>{health.get("disk","")}</span></div>'
            # Network info
            net = health.get("network", {})
            if net:
                pub_ip = net.get("public_ip", "")
                if pub_ip:
                    sys_rows += f'<div class="model-row"><span>IP publique</span><span>{pub_ip}</span></div>'
                for iface in net.get("interfaces", []):
                    if iface["type"] in ("lan", "vpn"):
                        sys_rows += f'<div class="model-row"><span>{iface["iface"]}</span><span class="badge {"badge-green" if iface["type"]=="vpn" else "badge"}">{iface["addr"]}</span></div>'
            st.markdown(
                f'<div class="card"><div class="card-header">🖥️ Système</div>{sys_rows}</div>',
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
                timer_row = f'<div class="sub-num" style="margin-top:4px">⏱ Prochain upgrade auto : {apt_timers.get("left_upgrade", "")} ({apt_timers["next_upgrade"]})</div>'
            # Upgradable packages list
            upg_rows = ""
            for pkg in upgradable_list[:8]:
                pkg_name = pkg.split("/")[0] if "/" in pkg else pkg
                upg_rows += f'<div class="sub-num" style="margin-top:1px">• {pkg_name}</div>'
            if len(upgradable_list) > 8:
                upg_rows += f'<div class="sub-num grey">… et {len(upgradable_list) - 8} autres</div>'
            st.markdown(
                f'<div class="card">'
                f'<div class="card-header">📦 APT {upgradable_badge}</div>'
                f'<div class="big-num {apt_clr}">{len(pkgs)}</div>'
                f'<div class="sub-num">package(s) mis à jour récemment</div>'
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
                    f'<div class="card-header">🔒 WireGuard — {total_c}/{total_p} actifs</div>'
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
                    '<div class="sub-num grey">Lancer daily-health-check.py sur le host</div>'
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
                f'<span class="badge" style="color:#fc8181">{len(wt_errors)} erreur(s)</span>'
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
                f'<div class="sub-num">image(s) mise(s) à jour</div>'
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
                    gh_label = gh.get("account") or "non authentifié"
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
                    tmx_label = f'{len(sessions)} session(s)' if sessions else "aucune"
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
                            f'• {s["name"]} · {s["windows"]} fenêtre(s){att}{dur}'
                            f'</div>'
                        )
                st.markdown(
                    f'<div class="card"><div class="card-header">🛠️ DevTools</div>{dev_rows}</div>',
                    unsafe_allow_html=True,
                )

        # ── OpenClaw card — structured doctor/security + version ────────────
        oc_ver = health.get("openclaw_version", {})
        doc_s  = health.get("doctor_structured", {})
        sec_s  = health.get("security_structured", {})

        if oc_ver or doc_s or sec_s:
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
                agents = doc_s.get("agents", [])
                agents_row = (
                    f'<div class="model-row"><span>Agents</span>'
                    f'<span class="badge">{len(agents)}</span></div>'
                ) if agents else ""
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

                st.markdown(
                    f'<div class="card">'
                    f'<div class="card-header">🦞 OpenClaw {ver_badge} {doc_icon}</div>'
                    f'{matrix_row}{agents_row}{hb_row}{sess_row}{mem_row}{pe_row}{sb_row}{compat_rows}'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # Session activity
                activity = doc_s.get("session_activity", [])
                if activity:
                    with st.expander(f"Sessions récentes ({len(activity)})"):
                        for a in activity:
                            short_name = a["name"].replace("agent:main:", "")
                            st.caption(f"  {short_name} — {a['ago']}")

            with oc_cols[1]:
                # Security summary
                sec_status = sec_s.get("status", "?")
                sec_icon = {"ok": "✅", "warn": "⚠️", "error": "❌"}.get(sec_status, "")
                summary = sec_s.get("summary", {})
                crit = summary.get("critical", 0)
                warn = summary.get("warn", 0)
                info = summary.get("info", 0)
                crit_clr = "color:#fc8181" if crit else ""
                warn_clr = "color:#ecc94b" if warn else ""

                atk = sec_s.get("attack_surface", {})
                trust_row = ""
                if atk.get("trust_model"):
                    short_trust = atk["trust_model"].split(",")[0][:40]
                    trust_row = f'<div class="sub-num" style="margin-top:4px">🔒 {short_trust}</div>'

                st.markdown(
                    f'<div class="card">'
                    f'<div class="card-header">🛡️ Security {sec_icon}</div>'
                    f'<div class="nums-row">'
                    f'<div class="info-block"><div class="big-num" style="{crit_clr}">{crit}</div><div class="sub-num">critical</div></div>'
                    f'<div class="info-block"><div class="big-num" style="{warn_clr}">{warn}</div><div class="sub-num">warn</div></div>'
                    f'<div class="info-block"><div class="big-num">{info}</div><div class="sub-num">info</div></div>'
                    f'</div>'
                    f'{trust_row}'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # Warnings detail
                warnings = sec_s.get("warnings", [])
                if warnings:
                    with st.expander(f"Warnings ({len(warnings)})"):
                        for w in warnings:
                            msg = w.get("message", "")[:80]
                            fix = w.get("fix", "")
                            st.caption(f"⚠ {msg}")
                            if fix:
                                st.caption(f"  Fix: {fix[:80]}")

            # Raw expanders for debug
            doctor_raw = health.get("doctor", "")
            audit_raw  = health.get("security_audit", "")
            if doctor_raw and "daily-health-check" not in doctor_raw:
                with st.expander("Détails bruts — Doctor"):
                    st.code(doctor_raw, language=None)
            if audit_raw and "daily-health-check" not in audit_raw:
                with st.expander("Détails bruts — Security Audit"):
                    st.code(audit_raw, language=None)

    else:
        st.info("Collecte système en cours, rafraîchis dans quelques secondes…")


# ══════════════════════════════════════════════════════════════════
# TAB 3 — Raw
# ══════════════════════════════════════════════════════════════════
with tabs[2]:
    st.json(data)

if "initial_render_done" not in st.session_state:
    st.session_state["initial_render_done"] = True
else:
    time.sleep(refresh_interval)
    st.rerun()
