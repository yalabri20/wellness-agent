"""Shared visual theme (CSS + small HTML components) for both Streamlit apps."""

from __future__ import annotations

import html

import streamlit as st

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

html, body, [class*="css"], .stMarkdown, .stText, button, input, textarea {
  font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif !important;
}
.block-container { padding-top: 1.4rem; padding-bottom: 2rem; max-width: 1320px; }
header[data-testid="stHeader"] { background: transparent; }
#MainMenu, footer[class] { visibility: hidden; }

/* Hero banner */
.hero {
  background: linear-gradient(120deg, #064E3B 0%, #0E9F6E 55%, #34D399 100%);
  border-radius: 20px; padding: 1.5rem 1.8rem; color: #fff; margin-bottom: 1.2rem;
  box-shadow: 0 10px 30px rgba(6, 78, 59, 0.18);
}
.hero h1 { font-size: 1.75rem; font-weight: 800; margin: 0; color: #fff; letter-spacing: -0.02em; }
.hero p { margin: 0.35rem 0 0; opacity: 0.9; font-size: 0.95rem; }
.hero .pills { margin-top: 0.9rem; display: flex; flex-wrap: wrap; gap: 0.45rem; }
.pill {
  display: inline-flex; align-items: center; gap: 0.35rem; padding: 0.28rem 0.7rem;
  border-radius: 999px; font-size: 0.78rem; font-weight: 600;
  background: rgba(255,255,255,0.16); color: #fff; border: 1px solid rgba(255,255,255,0.28);
}
.pill.warn { background: #FEF3C7; color: #92400E; border-color: #FDE68A; }
.pill.ok { background: #D1FAE5; color: #065F46; border-color: #A7F3D0; }
.pill.muted { background: #F1F5F9; color: #334155; border-color: #E2E8F0; }

/* KPI cards */
.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 0.9rem;
  margin-bottom: 1.1rem; }
.kpi {
  background: #fff; border: 1px solid #E5E9F0; border-radius: 16px; padding: 1rem 1.1rem;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}
.kpi .label { font-size: 0.78rem; color: #64748B; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.04em; }
.kpi .value { font-size: 1.7rem; font-weight: 800; color: #0F172A; margin-top: 0.2rem;
  letter-spacing: -0.02em; }
.kpi .sub { font-size: 0.8rem; margin-top: 0.15rem; color: #64748B; }
.kpi .sub.up { color: #059669; font-weight: 600; }
.kpi .sub.down { color: #DC2626; font-weight: 600; }
.kpi .icon { font-size: 1.15rem; float: right; opacity: 0.85; }

/* Section titles */
.section-title { font-size: 1.05rem; font-weight: 700; color: #0F172A; margin: 0.4rem 0 0.6rem; }
.section-sub { font-size: 0.82rem; color: #64748B; margin: -0.4rem 0 0.7rem; }

/* Insight cards */
.insight-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 0.8rem;
  margin-bottom: 1.2rem; }
.insight {
  background: #fff; border: 1px solid #E5E9F0; border-left: 4px solid #0E9F6E; border-radius: 14px;
  padding: 0.85rem 1rem; color: #1E293B; font-size: 0.9rem; line-height: 1.45;
}
.insight .tag { display: block; font-size: 0.7rem; font-weight: 700; color: #0E9F6E;
  text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.25rem; }

/* Bordered containers look like cards */
div[data-testid="stVerticalBlockBorderWrapper"] {
  background: #fff; border-radius: 16px !important; border-color: #E5E9F0 !important;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

/* Tabs */
.stTabs [data-baseweb="tab-list"] { gap: 0.4rem; border-bottom: none; }
.stTabs [data-baseweb="tab"] {
  background: #fff; border: 1px solid #E5E9F0; border-radius: 999px; padding: 0.45rem 1.1rem;
  height: auto; font-weight: 600;
}
.stTabs [aria-selected="true"] { background: #0E9F6E !important; color: #fff !important;
  border-color: #0E9F6E !important; }
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] { display: none; }

/* Buttons */
.stButton > button, .stFormSubmitButton > button, .stLinkButton > a {
  border-radius: 12px; font-weight: 600; border: 1px solid #E2E8F0; padding: 0.45rem 0.9rem;
  transition: all .15s ease;
}
.stButton > button:hover { border-color: #0E9F6E; color: #0E9F6E; transform: translateY(-1px); }
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
  background: #0E9F6E; border-color: #0E9F6E; color: #fff;
}
.stButton > button[kind="primary"]:hover { background: #047857; color: #fff; }

/* Chat */
div[data-testid="stChatMessage"] {
  background: #fff; border: 1px solid #E5E9F0; border-radius: 16px; padding: 0.8rem 1rem;
  margin-bottom: 0.6rem; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}
div[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]),
div[data-testid="stChatMessage"]:has([aria-label="Chat message from user"]) {
  background: #ECFDF5; border-color: #A7F3D0;
}

/* Calendar timeline */
.ev { display: flex; gap: 0.8rem; align-items: center; background: #fff; border: 1px solid #E5E9F0;
  border-radius: 14px; padding: 0.7rem 0.9rem; margin-bottom: 0.5rem; }
.ev .time { font-weight: 800; color: #0F172A; min-width: 104px; font-variant-numeric: tabular-nums; }
.ev .what { flex: 1; color: #334155; font-size: 0.9rem; }
.ev.high { border-left: 5px solid #DC2626; }
.ev.medium { border-left: 5px solid #F59E0B; }
.ev.low { border-left: 5px solid #94A3B8; }
.ev.wellness { border-left: 5px solid #0E9F6E; background: #F0FDF4; }
.ev.past { opacity: 0.5; }
.chip { font-size: 0.7rem; font-weight: 700; padding: 0.15rem 0.5rem; border-radius: 999px;
  text-transform: uppercase; letter-spacing: 0.04em; }
.chip.high { background: #FEE2E2; color: #991B1B; }
.chip.medium { background: #FEF3C7; color: #92400E; }
.chip.low { background: #F1F5F9; color: #475569; }
.chip.wellness { background: #D1FAE5; color: #065F46; }

/* Meal rows */
.meal { display: flex; justify-content: space-between; align-items: center; padding: 0.5rem 0;
  border-bottom: 1px dashed #E5E9F0; font-size: 0.88rem; }
.meal:last-child { border-bottom: none; }
.meal .dish { font-weight: 600; color: #0F172A; }
.meal .meta { color: #64748B; font-size: 0.78rem; }

/* Order steps */
.steps { display: flex; gap: 0.35rem; margin-top: 0.4rem; }
.steps .s { flex: 1; text-align: center; font-size: 0.72rem; font-weight: 700; padding: 0.35rem 0;
  border-radius: 8px; background: #F1F5F9; color: #94A3B8; }
.steps .s.on { background: #0E9F6E; color: #fff; }

.app-footer { color: #94A3B8; font-size: 0.78rem; text-align: center; margin-top: 2rem; }
section[data-testid="stSidebar"] { background: #fff; border-right: 1px solid #E5E9F0; }
</style>
"""


def inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def esc(value: object) -> str:
    return html.escape(str(value))


def hero(title: str, subtitle: str, pills: list[tuple[str, str]] | None = None) -> None:
    """pills: list of (text, css_class) where class is '' | 'ok' | 'warn' | 'muted'."""
    pill_html = "".join(f'<span class="pill {cls}">{esc(text)}</span>' for text, cls in (pills or []))
    st.markdown(
        f'<div class="hero"><h1>{esc(title)}</h1><p>{esc(subtitle)}</p>'
        f'<div class="pills">{pill_html}</div></div>',
        unsafe_allow_html=True,
    )


def kpi_cards(items: list[dict]) -> None:
    """items: {label, value, sub?, trend? ('up'|'down'), icon?}"""
    cards = []
    for it in items:
        sub = it.get("sub")
        trend = it.get("trend") or ""
        cards.append(
            '<div class="kpi">'
            f'<span class="icon">{esc(it.get("icon", ""))}</span>'
            f'<div class="label">{esc(it["label"])}</div>'
            f'<div class="value">{esc(it["value"])}</div>'
            + (f'<div class="sub {trend}">{esc(sub)}</div>' if sub else "")
            + "</div>"
        )
    st.markdown(f'<div class="kpi-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


def section(title: str, subtitle: str | None = None) -> None:
    st.markdown(f'<div class="section-title">{esc(title)}</div>', unsafe_allow_html=True)
    if subtitle:
        st.markdown(f'<div class="section-sub">{esc(subtitle)}</div>', unsafe_allow_html=True)


def insight_cards(items: list[tuple[str, str]]) -> None:
    """items: list of (tag, text)."""
    cards = "".join(
        f'<div class="insight"><span class="tag">{esc(tag)}</span>{esc(text)}</div>' for tag, text in items
    )
    st.markdown(f'<div class="insight-grid">{cards}</div>', unsafe_allow_html=True)


def footer(text: str) -> None:
    st.markdown(f'<div class="app-footer">{esc(text)}</div>', unsafe_allow_html=True)
