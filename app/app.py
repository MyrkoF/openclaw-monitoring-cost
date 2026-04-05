#!/usr/bin/env python3
"""
app.py — AI Cost Monitor. Cards compactes, période sélectionnable, health autonome.
"""

import os, json, time, threading
from datetime import datetime
import streamlit as st
import pandas as pd
from collectors import collect_all
from health_collector import collect_system, HEALTH_CACHE

st.set_page_config(page_title="AI Monitor", page_icon="📊", layout="wide")

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
    # Premier run immédiat si pas de cache
    if not os.path.exists(HEALTH_CACHE):
        try:
            collect_system()
        except Exception:
            pass


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("**⚙️ Controls**")
    period = st.radio("Période", ["1j", "7j", "30j"], index=2, horizontal=True)
    period_days = {"1j": 1, "7j": 7, "30j": 30}[period]

    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    auto = st.checkbox("Auto 5min", value=False)
    st.caption(f"UTC {datetime.utcnow().strftime('%H:%M')}")


# ── Data ───────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def load(days):
    from collectors import (collect_openrouter, collect_openai,
                             collect_anthropic, collect_google)
    return {
        "collected_at": datetime.utcnow().isoformat(),
        "providers": {
            "openrouter": collect_openrouter(),
            "openai":     collect_openai(),
            "anthropic":  collect_anthropic(days=days),
            "google":     collect_google(),
        }
    }

data = load(period_days)
p = data.get("providers", {})


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
                rem  = pd_.get("remaining_usd")
                used = (pd_.get("total_usage_usd_30d")
                        or pd_.get("total_usage_usd")
                        or pd_.get("estimated_cost_usd", 0) or 0)
                if rem is not None:
                    clr = "green" if rem > 5 else ("yellow" if rem > 1 else "red")
                    sub = f"${used:.4f} utilisé"
                    big = f"${rem:.2f}"
                    label2 = "restant"
                else:
                    clr = "yellow"
                    big = f"${used:.4f}"
                    label2 = f"utilisé ({period})"
                st.markdown(f"""<div class="card">
                    <div class="card-header">{icon} {label}</div>
                    <div class="big-num {clr}">{big}</div>
                    <div class="sub-num">{label2}</div>
                </div>""", unsafe_allow_html=True)
            elif s in ("no_logs", "no_key"):
                st.markdown(f"""<div class="card">
                    <div class="card-header">{icon} {label}</div>
                    <div class="big-num grey">—</div>
                    <div class="sub-num grey">{"clé manquante" if s=="no_key" else "aucun log"}</div>
                </div>""", unsafe_allow_html=True)
            else:
                err = (pd_.get("error","erreur"))[:40]
                st.markdown(f"""<div class="card">
                    <div class="card-header">{icon} {label}</div>
                    <div class="big-num red">❌</div>
                    <div class="sub-num red">{err}</div>
                </div>""", unsafe_allow_html=True)

    summary_card(c1, "🔀", "OpenRouter", p.get("openrouter", {}))
    summary_card(c2, "🤖", "OpenAI",    p.get("openai", {}))
    summary_card(c3, "🧠", "Anthropic", p.get("anthropic", {}))
    summary_card(c4, "🌐", "Google",    p.get("google", {}))

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Detail row 1 : OpenRouter + OpenAI ────────────────────────
    col_or, col_oai = st.columns(2)

    # OpenRouter
    with col_or:
        d = p.get("openrouter", {})
        if d.get("status") == "ok":
            total = d.get("total_credits_usd", 0) or 0
            used  = d.get("total_usage_usd", 0) or 0
            rem   = d.get("remaining_usd", 0) or 0
            pct   = min(used / total * 100, 100) if total else 0
            clr   = "green" if rem > 5 else ("yellow" if rem > 1 else "red")
            bar_w = f"{pct:.1f}%"

            st.markdown(f"""<div class="card">
                <div class="card-header">🔀 OpenRouter</div>
                <div class="nums-row">
                    <div class="info-block">
                        <div class="big-num {clr}">${rem:.4f}</div>
                        <div class="sub-num">restant</div>
                    </div>
                    <div class="info-block">
                        <div class="big-num">${used:.4f}</div>
                        <div class="sub-num">utilisé (total)</div>
                    </div>
                    <div class="info-block">
                        <div class="big-num grey">${total:.2f}</div>
                        <div class="sub-num">crédits total</div>
                    </div>
                </div>
                <div class="prog-bar-bg">
                    <div class="prog-bar-fill" style="width:{bar_w}"></div>
                </div>
                <div class="sub-num grey">{pct:.2f}% consommé</div>
            </div>""", unsafe_allow_html=True)
        else:
            st.markdown(f"""<div class="card">
                <div class="card-header">🔀 OpenRouter</div>
                <div class="sub-num grey">{d.get("status","?")} — {d.get("error","")}</div>
            </div>""", unsafe_allow_html=True)

    # OpenAI
    with col_oai:
        d = p.get("openai", {})
        if d.get("status") == "ok":
            usage = d.get("total_usage_usd_30d", 0) or 0
            org   = d.get("org", "")
            daily = d.get("daily", [])
            by_m  = d.get("by_model") or {}

            # Filtrer daily selon période
            if period_days < 30 and daily:
                from datetime import timedelta
                cutoff = (datetime.utcnow() - timedelta(days=period_days)).strftime("%Y-%m-%d")
                daily = [x for x in daily if x["date"] >= cutoff]
            usage_period = sum(x["cost_usd"] for x in daily) if daily else usage

            rows_html = ""
            for m, v in sorted(by_m.items(), key=lambda x: x[1].get("input_tokens",0), reverse=True):
                inp  = v.get("input_tokens", 0)
                out  = v.get("output_tokens", 0)
                reqs = v.get("requests", 0)
                rows_html += (
                    f'<div class="model-row">'
                    f'<span style="max-width:45%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:inline-block">{m}</span>'
                    f'<span>'
                    f'<span class="badge">in {inp:,}</span>'
                    f'<span class="badge">out {out:,}</span>'
                    f'<span class="badge">{reqs}r</span>'
                    f'</span></div>'
                )
            badge_org = f'<span class="badge" style="margin-left:auto;color:#667eea">{org}</span>' if org else ""

            st.markdown(f"""<div class="card">
                <div class="card-header">🤖 OpenAI {badge_org}</div>
                <div class="nums-row">
                    <div class="info-block">
                        <div class="big-num yellow">${usage_period:.4f}</div>
                        <div class="sub-num">utilisé ({period})</div>
                    </div>
                    <div class="info-block">
                        <div class="big-num grey">postpayé</div>
                        <div class="sub-num">pas de solde prépayé</div>
                    </div>
                </div>
                <hr class="divider">
                <div class="sub-num grey" style="margin-bottom:5px">PAR MODÈLE (30j)</div>
                {rows_html if rows_html else '<div class="sub-num grey">Aucune donnée</div>'}
            </div>""", unsafe_allow_html=True)

            # Mini graphe journalier
            if daily:
                df = pd.DataFrame(daily)
                df = df[df["cost_usd"] > 0]
                if not df.empty:
                    st.line_chart(df.set_index("date")["cost_usd"], height=70, use_container_width=True)
        else:
            st.markdown(f"""<div class="card">
                <div class="card-header">🤖 OpenAI</div>
                <div class="sub-num red">{d.get("error","erreur")}</div>
            </div>""", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Detail row 2 : Anthropic + Google ─────────────────────────
    col_ant, col_goo = st.columns(2)

    # Anthropic
    with col_ant:
        d = p.get("anthropic", {})
        cost    = d.get("estimated_cost_usd", 0) or 0
        inp_tot = d.get("total_input_tokens", 0) or 0
        out_tot = d.get("total_output_tokens", 0) or 0
        by_m    = d.get("by_model") or {}

        rows_html = ""
        for m, v in sorted(by_m.items(), key=lambda x: x[1].get("cost_usd",0), reverse=True):
            rows_html += (
                f'<div class="model-row">'
                f'<span>{m}</span>'
                f'<span>'
                f'<span class="badge">in {v["input_tokens"]:,}</span>'
                f'<span class="badge">out {v["output_tokens"]:,}</span>'
                f'<span class="badge badge-green">${v["cost_usd"]:.4f}</span>'
                f'</span></div>'
            )

        st.markdown(f"""<div class="card">
            <div class="card-header">🧠 Anthropic</div>
            <div class="nums-row">
                <div class="info-block">
                    <div class="big-num yellow">${cost:.4f}</div>
                    <div class="sub-num">estimé ({period})</div>
                </div>
                <div class="info-block">
                    <div class="big-num grey">{inp_tot:,}</div>
                    <div class="sub-num">tokens in</div>
                </div>
                <div class="info-block">
                    <div class="big-num grey">{out_tot:,}</div>
                    <div class="sub-num">tokens out</div>
                </div>
            </div>
            {'<hr class="divider">'+rows_html if rows_html else ""}
            <div class="sub-num grey" style="margin-top:6px">
                ⚠️ Pas d'API billing publique — estimation depuis logs OpenClaw
            </div>
        </div>""", unsafe_allow_html=True)

    # Google
    with col_goo:
        d     = p.get("google", {})
        cost  = d.get("estimated_cost_usd", 0) or 0
        by_m  = d.get("by_model") or {}
        s     = d.get("status", "?")

        rows_html = ""
        for m, v in by_m.items():
            rows_html += (
                f'<div class="model-row">'
                f'<span>{m}</span>'
                f'<span>${v.get("cost_usd",0):.6f}</span>'
                f'</div>'
            )

        note = ("Modèles Google passent via OpenRouter — comptabilisés dans OpenRouter"
                if s in ("no_logs", "ok") and not by_m
                else "Clé API simple — pas d'endpoint usage Google")

        st.markdown(f"""<div class="card">
            <div class="card-header">🌐 Google Gemini</div>
            <div class="nums-row">
                <div class="info-block">
                    <div class="big-num {'yellow' if cost>0 else 'grey'}">${cost:.4f}</div>
                    <div class="sub-num">estimé ({period})</div>
                </div>
            </div>
            {'<hr class="divider">'+rows_html if rows_html else ""}
            <div class="sub-num grey" style="margin-top:6px">ℹ️ {note}</div>
        </div>""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════
# TAB 2 — System Health
# ══════════════════════════════════════════════════════════════════
with tabs[1]:

    # Charger le cache (toujours disponible si thread tournant)
    health = {}
    if os.path.exists(HEALTH_CACHE):
        try:
            with open(HEALTH_CACHE) as f:
                health = json.load(f)
        except Exception:
            pass

    ts = health.get("collected_at", "")
    if ts:
        st.caption(f"Dernier refresh : {ts} UTC · auto-refresh toutes les 5 min")
    else:
        st.caption("Collecte en cours…")

    if health:
        c1, c2 = st.columns(2)
        with c1:
            # Uptime + Load
            st.markdown(f"""<div class="card">
                <div class="card-header">🖥️ Système</div>
                <div class="model-row"><span>Uptime</span><span><b>{health.get("uptime","N/A")}</b></span></div>
                <div class="model-row"><span>Depuis</span><span>{health.get("uptime_since","")}</span></div>
                <div class="model-row"><span>Load avg</span><span>{health.get("load","")}</span></div>
                <div class="model-row"><span>CPU cores</span><span>{health.get("cpu_cores","")}</span></div>
                <div class="model-row"><span>Mémoire</span><span>{health.get("memory","")}</span></div>
                <div class="model-row"><span>Disque</span><span>{health.get("disk","")}</span></div>
            </div>""", unsafe_allow_html=True)

            # Apt updates
            pkgs = health.get("apt_updates", [])
            clr = "yellow" if pkgs else "green"
            st.markdown(f"""<div class="card">
                <div class="card-header">📦 Apt updates (24h)</div>
                <div class="big-num {clr}">{len(pkgs)}</div>
                <div class="sub-num">package(s) mis à jour</div>
                {''.join(f'<div class="sub-num" style="margin-top:2px">{x[-80:]}</div>' for x in pkgs[-5:]) if pkgs else ""}
            </div>""", unsafe_allow_html=True)

        with c2:
            # Docker containers
            containers = health.get("docker_containers", [])
            if containers:
                st.markdown('<div class="card"><div class="card-header">🐳 Docker containers</div>', unsafe_allow_html=True)
                for c in containers:
                    running = "Up" in c.get("status", "")
                    dot = "🟢" if running else "🔴"
                    st.markdown(
                        f'<div class="model-row"><span>{dot} {c["name"]}</span>'
                        f'<span class="sub-num">{c["status"][:40]}</span></div>',
                        unsafe_allow_html=True
                    )
                st.markdown('</div>', unsafe_allow_html=True)
            else:
                st.markdown("""<div class="card">
                    <div class="card-header">🐳 Docker</div>
                    <div class="sub-num grey">Docker socket non monté dans le container</div>
                </div>""", unsafe_allow_html=True)

            # Watchtower
            wt = health.get("watchtower_updates", [])
            st.markdown(f"""<div class="card">
                <div class="card-header">🔄 Watchtower (24h)</div>
                <div class="big-num {'yellow' if wt else 'green'}">{len(wt)}</div>
                <div class="sub-num">image(s) mise(s) à jour</div>
                {''.join(f'<div class="sub-num" style="margin-top:2px">{x[-80:]}</div>' for x in wt[-5:]) if wt else ""}
            </div>""", unsafe_allow_html=True)

        # OpenClaw doctor / audit — depuis daily-health-check.py si dispo
        doctor = health.get("doctor", "")
        audit  = health.get("security_audit", "")
        if doctor and "manually" not in doctor:
            with st.expander("🔒 OpenClaw Doctor"):
                st.code(doctor, language=None)
        if audit and "manually" not in audit:
            with st.expander("🛡️ Security Audit"):
                st.code(audit, language=None)

    else:
        st.info("Collecte système en cours, rafraîchis dans quelques secondes…")


# ══════════════════════════════════════════════════════════════════
# TAB 3 — Raw
# ══════════════════════════════════════════════════════════════════
with tabs[2]:
    st.json(data)

if auto:
    time.sleep(300)
    st.rerun()
