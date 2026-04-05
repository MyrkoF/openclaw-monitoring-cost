#!/usr/bin/env python3
"""
app.py — AI Cost Monitor. Cards compactes, période sélectionnable, health autonome.
"""

import os, json, time, threading
from datetime import datetime, timedelta
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


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("**⚙️ Controls**")
    period = st.radio("Période", ["1j", "7j", "30j"], index=2, horizontal=True)
    period_days = {"1j": 1, "7j": 7, "30j": 30}[period]

    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    auto = st.checkbox(
        "Auto 5min (coûts IA)", value=False,
        help="Recharge la page toutes les 5 min pour actualiser les coûts IA. "
             "Les métriques système se rafraîchissent via un thread indépendant."
    )
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
                rem  = pd_.get("remaining_usd") or pd_.get("credits_remaining_usd")
                used = (pd_.get("total_usage_usd_30d")
                        or pd_.get("total_usage_usd")
                        or pd_.get("estimated_cost_usd", 0) or 0)
                if rem is not None:
                    clr    = "green" if rem > 5 else ("yellow" if rem > 1 else "red")
                    label2 = "restant"
                    big    = f"${rem:.2f}"
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
                    f'<span class="badge">in {v["input_tokens"]:,}</span>'
                    f'<span class="badge">out {v["output_tokens"]:,}</span>'
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
                    f'<span class="badge">in {inp:,}</span>'
                    f'<span class="badge">out {out:,}</span>'
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

            if daily:
                df_chart = pd.DataFrame(daily)
                df_chart = df_chart[df_chart["cost_usd"] > 0]
                if not df_chart.empty:
                    st.line_chart(df_chart.set_index("date")["cost_usd"], height=70, use_container_width=True)
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

    # ── Anthropic ──────────────────────────────────────────────────
    with col_ant:
        d        = p.get("anthropic", {})
        cost     = d.get("estimated_cost_usd", 0) or 0
        inp_tot  = d.get("total_input_tokens", 0) or 0
        out_tot  = d.get("total_output_tokens", 0) or 0
        by_m     = d.get("by_model") or {}
        b_status = d.get("billing_status", "unavailable")
        b_rem    = d.get("credits_remaining_usd")
        b_tot    = d.get("credits_total_usd")
        b_used   = d.get("credits_used_usd")
        b_src    = d.get("billing_source", "none")

        # Section billing API (si disponible)
        if b_status == "ok" and b_rem is not None and b_tot:
            b_pct = min((b_used or 0) / b_tot * 100, 100) if b_tot else 0
            b_clr = "green" if b_rem > 5 else ("yellow" if b_rem > 1 else "red")
            src_label = "Console API" if b_src == "console_key" else "Claude CLI"
            billing_section = (
                f'<div class="nums-row">'
                f'<div class="info-block"><div class="big-num {b_clr}">${b_rem:.2f}</div><div class="sub-num">crédits restants</div></div>'
                f'<div class="info-block"><div class="big-num">${b_used:.2f}</div><div class="sub-num">utilisé (prépayé)</div></div>'
                f'<div class="info-block"><div class="big-num grey">${b_tot:.2f}</div><div class="sub-num">total prépayé</div></div>'
                f'</div>'
                f'<div class="prog-bar-bg"><div class="prog-bar-fill" style="width:{b_pct:.1f}%"></div></div>'
                f'<div class="sub-num grey">{b_pct:.2f}% consommé · source: {src_label}</div>'
                f'<hr class="divider">'
            )
        else:
            billing_section = ""

        # Lignes par modèle depuis logs
        model_rows = "".join(
            f'<div class="model-row">'
            f'<span>{m}</span>'
            f'<span>'
            f'<span class="badge">in {v["input_tokens"]:,}</span>'
            f'<span class="badge">out {v["output_tokens"]:,}</span>'
            f'<span class="badge badge-green">${v["cost_usd"]:.4f}</span>'
            f'</span></div>'
            for m, v in sorted(by_m.items(), key=lambda x: x[1].get("cost_usd", 0), reverse=True)
        )
        usage_section = (
            f'<div class="nums-row">'
            f'<div class="info-block"><div class="big-num yellow">${cost:.4f}</div><div class="sub-num">estimé ({period})</div></div>'
            f'<div class="info-block"><div class="big-num grey">{inp_tot:,}</div><div class="sub-num">tokens in</div></div>'
            f'<div class="info-block"><div class="big-num grey">{out_tot:,}</div><div class="sub-num">tokens out</div></div>'
            f'</div>'
        )
        model_divider = '<hr class="divider">' if model_rows else ""
        note = "" if b_status == "ok" else '<div class="sub-num grey" style="margin-top:6px">⚠️ Billing API non configuré — estimation depuis logs OpenClaw</div>'

        st.markdown(
            f'<div class="card">'
            f'<div class="card-header">🧠 Anthropic</div>'
            f'{billing_section}'
            f'{usage_section}'
            f'{model_divider}{model_rows}'
            f'{note}'
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

    health = {}
    if os.path.exists(HEALTH_CACHE):
        try:
            with open(HEALTH_CACHE) as f:
                health = json.load(f)
        except Exception:
            pass

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
                f'<div class="model-row"><span>Mémoire</span><span>{health.get("memory","")}</span></div>'
                f'<div class="model-row"><span>Disque</span><span>{health.get("disk","")}</span></div>'
            )
            st.markdown(
                f'<div class="card"><div class="card-header">🖥️ Système</div>{sys_rows}</div>',
                unsafe_allow_html=True,
            )

            # Apt updates card
            pkgs            = health.get("apt_updates", [])
            upgradable_cnt  = health.get("apt_upgradable_count", 0)
            apt_clr         = "yellow" if upgradable_cnt > 10 else ("yellow" if pkgs else "green")
            apt_rows = "".join(
                f'<div class="sub-num" style="margin-top:2px">{x[-80:]}</div>'
                for x in pkgs[-5:]
            )
            upgradable_badge = (
                f'<span class="badge {"badge" if upgradable_cnt <= 10 else ""}" '
                f'style="{"color:#fc8181" if upgradable_cnt > 10 else ""}">'
                f'{upgradable_cnt} upgradable</span>'
                if upgradable_cnt else ""
            )
            st.markdown(
                f'<div class="card">'
                f'<div class="card-header">📦 Apt updates (24h) {upgradable_badge}</div>'
                f'<div class="big-num {apt_clr}">{len(pkgs)}</div>'
                f'<div class="sub-num">package(s) mis à jour récemment</div>'
                f'{apt_rows}'
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
                container_rows = "".join(
                    f'<div class="model-row">'
                    f'<span>{"🟢" if c.get("state","") == "running" or "Up" in c.get("status","") else "🔴"} {c.get("name","?")}</span>'
                    f'<span class="sub-num">{str(c.get("status",""))[:40]}</span>'
                    f'</div>'
                    for c in containers
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
            wt_color   = "red" if wt_errors else ("yellow" if wt else "green")
            src_badge  = (
                '<span class="badge badge-green">API</span>'
                if wt_source == "api"
                else '<span class="badge">sidecar</span>'
            )
            err_badge  = (
                f'<span class="badge" style="color:#fc8181">⚠️ {len(wt_errors)} erreur(s)</span>'
                if wt_errors else ""
            )
            wt_rows = "".join(
                f'<div class="sub-num" style="margin-top:2px">{x[-80:]}</div>'
                for x in wt[-5:]
            )
            err_rows = "".join(
                f'<div class="sub-num" style="color:#fc8181;margin-top:2px">{x[-80:]}</div>'
                for x in wt_errors[-3:]
            )
            st.markdown(
                f'<div class="card">'
                f'<div class="card-header">🔄 Watchtower {src_badge}{err_badge}</div>'
                f'<div class="big-num {wt_color}">{len(wt)}</div>'
                f'<div class="sub-num">image(s) mise(s) à jour</div>'
                f'{wt_rows}{err_rows}'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Services card
            services = health.get("services", {})
            if services:
                service_rows = "".join(
                    f'<div class="model-row">'
                    f'<span>{svc}</span>'
                    f'<span class="badge {"badge-green" if st_ == "active" else "badge"}" '
                    f'style="{"" if st_ == "active" else "color:#fc8181"}">{st_}</span>'
                    f'</div>'
                    for svc, st_ in services.items()
                )
                st.markdown(
                    f'<div class="card"><div class="card-header">⚙️ Services</div>{service_rows}</div>',
                    unsafe_allow_html=True,
                )

        # OpenClaw doctor / audit — depuis daily-health-check.py (sidecar JSON)
        doctor = health.get("doctor", "")
        audit  = health.get("security_audit", "")
        if doctor and "daily-health-check" not in doctor:
            with st.expander("🔒 OpenClaw Doctor"):
                st.code(doctor, language=None)
        if audit and "daily-health-check" not in audit:
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
