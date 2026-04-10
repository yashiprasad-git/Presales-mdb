"""
monitor_dashboard.py — DB Update Monitor (compat entry point).

Prefer: streamlit run mdb/monitor_dashboard.py
Canonical copy: mdb/monitor_dashboard.py
"""

import os
import sys
from pathlib import Path

# Resolve imports: mdb/db_updater.py (retry_blocked) vs root db_updater wrapper
_pkg = Path(__file__).resolve().parent
if (_pkg / "db_updater.py").is_file() and (_pkg / "mdb_core.py").is_file():
    sys.path.insert(0, str(_pkg))
else:
    sys.path.insert(0, str(_pkg / "mdb"))
import subprocess
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras
import streamlit as st

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Presales DB Monitor",
    page_icon="🗄️",
    layout="wide",
)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_db_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        try:
            url = st.secrets.get("DATABASE_URL", "")
        except Exception:
            pass
    return url or ""


def _impersonate_hint() -> str:
    """Email shown in access-issue help text (same as pipeline GOOGLE_IMPERSONATE_USER)."""
    v = os.getenv("GOOGLE_IMPERSONATE_USER", "").strip()
    if v:
        return v
    try:
        return (st.secrets.get("GOOGLE_IMPERSONATE_USER", "") or "").strip()
    except Exception:
        return ""


def get_conn():
    return psycopg2.connect(_get_db_url())


def _df(conn, sql: str, params: tuple = ()) -> pd.DataFrame:
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()
    except Exception as e:
        st.error(f"DB query failed: {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def fetch_pipeline_runs(conn) -> pd.DataFrame:
    return _df(conn, """
        SELECT run_id, started_at_utc, finished_at_utc, status
        FROM pipeline_runs
        ORDER BY started_at_utc DESC
        LIMIT 50
    """).fillna("")


def fetch_run_summary(conn) -> pd.DataFrame:
    """Per-run campaign stats derived from campaigns table."""
    try:
        return _df(conn, """
            SELECT
                run_id,
                COUNT(*)                                                      AS total_campaigns,
                SUM(CASE WHEN context_status LIKE '✅%%' THEN 1 ELSE 0 END)  AS context_ok,
                SUM(CASE WHEN context_status LIKE '❌%%' THEN 1 ELSE 0 END)  AS context_failed,
                SUM(CASE WHEN context_status IS NULL     THEN 1 ELSE 0 END)  AS context_pending
            FROM campaigns
            GROUP BY run_id
            ORDER BY MIN(inserted_at_utc) DESC
        """).fillna(0)
    except Exception:
        return pd.DataFrame(columns=["run_id", "total_campaigns",
                                     "context_ok", "context_failed", "context_pending"])


def fetch_campaign_summary(conn) -> pd.DataFrame:
    return _df(conn, """
        SELECT region,
               COUNT(*)                                                      AS total,
               SUM(CASE WHEN context_status LIKE '✅%' THEN 1 ELSE 0 END)   AS context_ok,
               SUM(CASE WHEN context_status LIKE '❌%' THEN 1 ELSE 0 END)   AS context_blocked,
               SUM(CASE WHEN context_status IS NULL    THEN 1 ELSE 0 END)   AS context_pending,
               SUM(CASE WHEN derived_language IS NOT NULL THEN 1 ELSE 0 END) AS analysed
        FROM campaigns
        GROUP BY region
        ORDER BY region
    """).fillna(0)


def fetch_blocked(conn) -> pd.DataFrame:
    return _df(conn, """
        SELECT region, campaign_name, brand, country,
               media_plan_url, error_message, date_flagged_utc, monday_url
        FROM access_blocked
        WHERE resolved_at_utc IS NULL
        ORDER BY date_flagged_utc DESC
    """).fillna("")


def fetch_context_status(conn) -> pd.DataFrame:
    return _df(conn, """
        SELECT region, campaign_name, brand_name, country,
               context_status, media_plan_url, monday_url, inserted_at_utc
        FROM campaigns
        ORDER BY inserted_at_utc DESC
    """).fillna("")


# ---------------------------------------------------------------------------
# Pipeline runner (manual trigger)
# ---------------------------------------------------------------------------

def run_db_updater(since_date: Optional[str] = None) -> str:
    monday_key = os.getenv("MONDAY_API_KEY") or st.secrets.get("MONDAY_API_KEY", "")
    db_url     = os.getenv("DATABASE_URL")    or st.secrets.get("DATABASE_URL", "")

    root = Path(__file__).resolve().parent
    mdb_dir = root if (root / "mdb_core.py").is_file() else root / "mdb"
    script = mdb_dir / "db_updater.py"
    config = mdb_dir / "monday_config.json"

    cmd = [sys.executable, str(script), str(config)]
    if since_date:
        cmd += ["--since", since_date]

    env = os.environ.copy()
    env["MONDAY_API_KEY"] = monday_key
    env["DATABASE_URL"]   = db_url
    # Pass Google auth settings through to the updater (service account + optional impersonation)
    for k in ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_IMPERSONATE_USER"):
        v = os.getenv(k) or st.secrets.get(k, "")
        if v:
            env[k] = v

    result = subprocess.run(
        cmd, capture_output=True, text=True, env=env, cwd=str(mdb_dir),
    )
    return result.stdout + ("\n\nSTDERR:\n" + result.stderr if result.stderr else "")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def main():
    st.title("🗄️ MDB Update Dashboard")
    st.caption("Monitors daily DB updates (Monday.com → PostgreSQL). "
               "Independent of the analysis dashboard.")

    conn = get_conn()
    st.session_state.setdefault("last_db_update_output", "")
    st.session_state.setdefault("last_retry_output", "")
    st.session_state.setdefault("last_error", "")
    st.session_state.setdefault("last_validation_output", "")
    st.session_state.setdefault("last_validation_error", "")

    # ── Sidebar — manual run ────────────────────────────────────────────
    with st.sidebar:
        st.header("▶ Run DB Update")
        # Show last successful run so users avoid old backfills
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT started_at_utc FROM pipeline_runs "
                    "WHERE status='success' ORDER BY started_at_utc DESC LIMIT 1"
                )
                row = cur.fetchone()
            last_ok = str(row[0]) if row else ""
        except Exception:
            last_ok = ""

        if last_ok:
            # Format example: 26-mar-2026 08:00 IST
            last_date = last_ok
            try:
                from datetime import datetime
                try:
                    from zoneinfo import ZoneInfo
                    ist = ZoneInfo("Asia/Kolkata")
                except Exception:
                    ist = None

                dt = datetime.fromisoformat(last_ok.replace("Z", "+00:00"))
                if ist is not None:
                    dt = dt.astimezone(ist)
                last_date = dt.strftime("%d-%b-%Y %H:%M").lower() + (" IST" if ist is not None else "")
            except Exception:
                pass
            st.caption(f"Manually update the MDB since last run.  \n**Last successful run (IST):** {last_date}")
        else:
            st.caption("Manually update the MDB since last run.  \n**Last successful run (IST):** none yet")
        if st.button("▶ Run DB Update Now", use_container_width=True, type="primary"):
            with st.spinner("Running DB update…"):
                output = run_db_updater(None)
            st.session_state["last_db_update_output"] = output
            st.session_state["last_error"] = ""
            st.success("Run complete")

        if st.session_state.get("last_db_update_output"):
            with st.expander("📄 View last DB update logs", expanded=False):
                st.text_area(
                    "Last DB Update Output",
                    st.session_state["last_db_update_output"],
                    height=300,
                )

        st.divider()
        st.subheader("🔄 Retry Failed Campaigns")
        st.caption("Re-attempts media plan reading for all ❌ campaigns.")
        if st.button("🔄 Retry Now", use_container_width=True):
            with st.spinner("Re-attempting blocked/failed media plans…"):
                try:
                    from db_updater import retry_blocked  # lazy import — avoid slow startup
                    monday_key = os.getenv("MONDAY_API_KEY") or st.secrets.get("MONDAY_API_KEY", "")
                    # Ensure retry_blocked sees Google settings (it reads from env)
                    for k in ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_IMPERSONATE_USER"):
                        v = os.getenv(k) or st.secrets.get(k, "")
                        if v:
                            os.environ[k] = v
                    summary = retry_blocked(conn, monday_key=monday_key)
                    st.session_state["last_retry_output"] = summary
                    st.session_state["last_error"] = ""
                    st.success("Done (result saved below)")
                except Exception as e:
                    st.session_state["last_error"] = str(e)
                    st.session_state["last_retry_output"] = ""
                    st.error(f"Error: {e}")

        if st.session_state.get("last_error") or st.session_state.get("last_retry_output"):
            with st.expander("📄 View last retry logs", expanded=False):
                if st.session_state.get("last_error"):
                    st.text_area("Last Error", st.session_state["last_error"], height=140)
                if st.session_state.get("last_retry_output"):
                    st.text_area("Last Retry Result", st.session_state["last_retry_output"], height=250)

        st.divider()
        st.subheader("🧪 Validate Context Lists")
        st.caption("Validates context lists against the AI quality rules (gpt-4o).")

        try:
            from context_validator import pending_validation_count, init_validation_schema
            init_validation_schema(conn)
            pending_val = pending_validation_count(conn)
        except Exception:
            pending_val = None

        if pending_val is not None:
            st.caption(f"**Pending validation:** {pending_val} campaign(s)")

        if st.button("🧪 Run Validation Now", use_container_width=True):
            try:
                openai_key = st.secrets["OPENAI_API_KEY"]
            except (KeyError, Exception):
                openai_key = ""
            if not openai_key:
                st.error("OPENAI_API_KEY not set in Streamlit secrets.")
            else:
                with st.spinner("Validating context lists…"):
                    try:
                        from context_validator import run_validation
                        summary = run_validation(conn, openai_api_key=openai_key)
                        st.session_state["last_validation_output"] = summary
                        st.session_state["last_validation_error"] = ""
                        st.success("Validation complete")
                    except Exception as e:
                        st.session_state["last_validation_error"] = str(e)
                        st.session_state["last_validation_output"] = ""
                        st.error(f"Error: {e}")

        if st.session_state.get("last_validation_output") or st.session_state.get("last_validation_error"):
            with st.expander("📄 View last validation logs", expanded=False):
                if st.session_state.get("last_validation_error"):
                    st.text_area("Last Error", st.session_state["last_validation_error"], height=140)
                if st.session_state.get("last_validation_output"):
                    st.text_area("Last Validation Result", st.session_state["last_validation_output"], height=300)

        st.divider()
        st.caption("Analysis dashboard → [Open](https://silverpush-presales-dashboard.streamlit.app)")

    # ── Tabs ────────────────────────────────────────────────────────────
    tab_runs, tab_ctx, tab_val = st.tabs([
        "📋 Run History",
        "🔍 Run Results",
        "🧪 Validation Results",
    ])

    # ── Run History ─────────────────────────────────────────────────────
    with tab_runs:
        st.subheader("Recent Pipeline Runs")
        df_runs    = fetch_pipeline_runs(conn)
        df_summary = fetch_run_summary(conn)

        if df_runs.empty:
            st.info("No pipeline runs found. Click **▶ Run DB Update Now** to start.")
        else:
            if not df_summary.empty:
                merged = df_runs.merge(df_summary, on="run_id", how="left").fillna(0)
            else:
                merged = df_runs.copy()
                for col in ["total_campaigns", "context_ok", "context_failed", "context_pending"]:
                    merged[col] = 0

            def _status_icon(s):
                return "✅" if s == "success" else ("⏳" if s == "running" else "❌")

            merged[""] = merged["status"].apply(_status_icon)
            display_cols = ["", "run_id", "started_at_utc", "finished_at_utc",
                            "status", "total_campaigns", "context_ok",
                            "context_failed", "context_pending"]
            merged = merged[[c for c in display_cols if c in merged.columns]]
            merged.columns = ["", "Run ID", "Started (UTC)", "Finished (UTC)",
                               "Status", "Campaigns", "Context OK",
                               "Context Failed", "Context Pending"][:len(merged.columns)]
            st.dataframe(merged, use_container_width=True, hide_index=True)

    # ── Run Results ─────────────────────────────────────────────────────
    with tab_ctx:
        st.subheader("Run Results")
        df_ctx = fetch_context_status(conn)
        if df_ctx.empty:
            st.info("No campaigns yet.")
        else:
            ctx_col = df_ctx["context_status"].fillna("")

            # Known failure patterns → short display labels shown in the filter.
            # Add new entries here whenever a new failure reason is introduced.
            KNOWN_FAILURE_LABELS = {
                "media plan link not set": "❌ Media plan missing",
                "access blocked":          "❌ Access blocked",
                "access issue":            "❌ Access issue",
                "domain-only":             "❌ Domain-only / sign-in required",
                "sign-in page":            "❌ Domain-only / sign-in required",
                "service account":         "❌ Access issue",
                "non-google":              "❌ Not a Google link",
                "sharepoint":              "❌ Not a Google link",
                "context list not in standard format": "❌ Context list not in standard format",
                "context tab found but":   "❌ Context list not in standard format",
                "context tab not found":   "❌ Context tab not found",
            }

            def _short_label(raw: str) -> str:
                """Return a short display name for a raw ❌ context_status value."""
                lower = raw.lower()
                for keyword, label in KNOWN_FAILURE_LABELS.items():
                    if keyword in lower:
                        return label
                # Fallback: strip the leading ❌ and trim
                return raw.lstrip("❌").strip()

            # Map unique ❌ values → short labels (deduped by label)
            seen_labels: dict[str, str] = {}   # label → raw value (first match wins)
            for raw in sorted(ctx_col[ctx_col.str.startswith("❌")].unique()):
                label = _short_label(raw)
                if label not in seen_labels:
                    seen_labels[label] = raw

            status_options = (
                ["All", "✅ Success", "❌ All Failed", "⏳ Not yet processed"]
                + list(seen_labels.keys())
            )

            f1, f2, f3 = st.columns([1, 1, 2])
            reg  = f1.selectbox("Region",
                                ["All"] + sorted([r for r in df_ctx["region"].unique() if r]),
                                key="ctx_reg")
            stat = f2.selectbox("Status", status_options, key="ctx_stat")
            srch = f3.text_input("Search campaign / brand", key="ctx_srch").strip().lower()

            filt = df_ctx.copy()
            if reg != "All":
                filt = filt[filt["region"] == reg]

            if stat == "✅ Success":
                filt = filt[ctx_col.reindex(filt.index).str.startswith("✅", na=False)]
            elif stat == "❌ All Failed":
                filt = filt[ctx_col.reindex(filt.index).str.startswith("❌", na=False)]
            elif stat == "⏳ Not yet processed":
                filt = filt[ctx_col.reindex(filt.index) == ""]
            elif stat in seen_labels:
                # Match all ❌ rows whose short label equals the selected option
                matching_raws = [r for r, lbl in
                                 {v: _short_label(v) for v in
                                  ctx_col[ctx_col.str.startswith("❌")].unique()}.items()
                                 if lbl == stat]
                filt = filt[ctx_col.reindex(filt.index).isin(matching_raws)]

            if srch:
                hay = filt[["campaign_name", "brand_name"]].fillna("").astype(str)\
                          .agg(" ".join, axis=1).str.lower()
                filt = filt[hay.str.contains(srch, na=False)]

            # Drop media_plan_url — Monday link is sufficient
            filt = filt[[c for c in filt.columns if c != "media_plan_url"]]

            st.write(f"Showing **{len(filt)}** / {len(df_ctx)} campaigns")

            if stat in ("❌ Access blocked", "❌ Access issue", "❌ All Failed") and len(filt) > 0:
                _imp = _impersonate_hint()
                if _imp:
                    st.caption(
                        f"**Access issue:** grant **View** on the media plan to **{_imp}**, "
                        "then **Retry** or **Run DB Update**."
                    )
                else:
                    st.caption(
                        "Set **GOOGLE_IMPERSONATE_USER** in Streamlit secrets and grant that account "
                        "**View** on the file, then **Retry**."
                    )

            col_cfg = {}
            if "monday_url" in filt.columns:
                col_cfg["monday_url"] = st.column_config.LinkColumn(
                    "Monday Link", display_text="Open")
            st.dataframe(filt, use_container_width=True, height=500,
                         column_config=col_cfg, hide_index=True)

    # ── Validation Results ───────────────────────────────────────────────
    with tab_val:
        st.subheader("Validation Results")
        st.caption(
            "Context lists validated by gpt-4o. "
            "POSITIVE_EXAMPLE and NEGATIVE_EXAMPLE are stored for model training. "
            "DO_NOT_STORE (2+ errors) is excluded."
        )

        try:
            from context_validator import fetch_validation_results, init_validation_schema
            init_validation_schema(conn)
            df_val = fetch_validation_results(conn)
        except Exception as e:
            st.error(f"Could not load validation results: {e}")
            df_val = None

        if df_val is not None:
            if df_val.empty:
                st.info("No validation results yet. Click **🧪 Run Validation Now** in the sidebar.")
            else:
                STATUS_ICONS = {
                    "PASS":               "✅ PASS",
                    "PASS_WITH_WARNINGS": "⚠️ PASS_WITH_WARNINGS",
                    "FAIL_MINOR":         "🔶 FAIL_MINOR",
                    "FAIL_MAJOR":         "❌ FAIL_MAJOR",
                }
                LABEL_ICONS = {
                    "POSITIVE_EXAMPLE": "🟢 POSITIVE",
                    "NEGATIVE_EXAMPLE": "🟡 NEGATIVE",
                    "DO_NOT_STORE":     "🔴 DO_NOT_STORE",
                }

                v1, v2, v3 = st.columns([1, 1, 2])
                reg_opts  = ["All"] + sorted([r for r in df_val["region"].unique() if r])
                stat_opts = ["All"] + [s for s in STATUS_ICONS if s in df_val["overall_status"].values]
                label_opts = ["All"] + [l for l in LABEL_ICONS if l in df_val["training_label"].fillna("").values]

                val_reg   = v1.selectbox("Region",         reg_opts,   key="val_reg")
                val_stat  = v2.selectbox("Overall Status", stat_opts,  key="val_stat")
                val_label = v3.selectbox("Training Label", label_opts, key="val_label")

                filt_val = df_val.copy()
                if val_reg != "All":
                    filt_val = filt_val[filt_val["region"] == val_reg]
                if val_stat != "All":
                    filt_val = filt_val[filt_val["overall_status"] == val_stat]
                if val_label != "All":
                    filt_val = filt_val[filt_val["training_label"] == val_label]

                # Summary metrics
                total = len(filt_val)
                store_count = int(filt_val["store_in_training_db"].fillna(False).sum())
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Total validated", total)
                m2.metric("Store in training DB", store_count)
                m3.metric("Avg errors",   f"{filt_val['errors_count'].fillna(0).mean():.1f}" if total else "—")
                m4.metric("Avg warnings", f"{filt_val['warnings_count'].fillna(0).mean():.1f}" if total else "—")

                st.write(f"Showing **{total}** / {len(df_val)} results")

                # Friendly labels for display
                display = filt_val.copy()
                display["overall_status"] = display["overall_status"].map(
                    lambda x: STATUS_ICONS.get(x, x) if x else ""
                )
                display["training_label"] = display["training_label"].map(
                    lambda x: LABEL_ICONS.get(x, x) if x else ""
                )
                display["store_in_training_db"] = display["store_in_training_db"].map(
                    lambda x: "Yes" if x else ("No" if x is not None else "")
                )

                col_order = [
                    "region", "campaign_name", "brand_name", "overall_status",
                    "training_label", "store_in_training_db",
                    "errors_count", "warnings_count", "recommendations_count",
                    "validated_at", "error_log",
                ]
                display = display[[c for c in col_order if c in display.columns]]
                display.columns = [
                    c.replace("_", " ").title() for c in display.columns
                ]

                st.dataframe(display, use_container_width=True, height=500, hide_index=True)

    conn.close()


if __name__ == "__main__":
    main()
