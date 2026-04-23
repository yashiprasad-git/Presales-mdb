"""
context_validator.py — Context list validation engine for YouTube Mirror campaigns.

Reads campaigns + context_rows from Supabase, calls OpenAI gpt-4o to validate
each context list against the system prompt, and saves results back to Supabase.

Secrets (via st.secrets or env): OPENAI_API_KEY, DATABASE_URL
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None  # type: ignore

try:
    from openai import OpenAI as _OpenAI
except ImportError:
    _OpenAI = None  # type: ignore

MDB_DIR = Path(__file__).resolve().parent
SYSTEM_PROMPT_PATH = MDB_DIR / "context_list_validation_system_prompt.md"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_validation_schema(conn) -> None:
    """Create validation_results table if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS validation_results (
              id                        SERIAL PRIMARY KEY,
              monday_item_id            TEXT UNIQUE NOT NULL,
              campaign_name             TEXT,
              brand_name                TEXT,
              region                    TEXT,
              validated_at              TEXT NOT NULL,
              overall_status            TEXT,
              training_label            TEXT,
              store_in_training_db      BOOLEAN,
              errors_count              INTEGER,
              warnings_count            INTEGER,
              recommendations_count     INTEGER,
              full_validation_report    TEXT,
              error_log                 TEXT,
              inserted_at_utc           TEXT NOT NULL
            );
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_validation_item   ON validation_results(monday_item_id);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_validation_status ON validation_results(overall_status);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_validation_label  ON validation_results(training_label);"
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def _get_campaigns_needing_validation(conn) -> List[Dict]:
    """
    Return campaigns that:
    - Have at least one context_row saved (successful context extraction)
    - Have NOT been validated yet (no entry in validation_results)
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT
                   c.monday_item_id, c.monday_url, c.region,
                   c.campaign_name, c.brand_name, c.vertical, c.country,
                   c.rfp_summary, c.targeting, c.any_other_details,
                   c.products_to_pitch, c.run_dates
            FROM campaigns c
            INNER JOIN context_rows cr ON cr.monday_item_id = c.monday_item_id
            LEFT  JOIN validation_results vr ON vr.monday_item_id = c.monday_item_id
            WHERE vr.monday_item_id IS NULL
               OR vr.overall_status IS NULL
            ORDER BY c.monday_item_id
        """)
        return [dict(r) for r in cur.fetchall()]


def cleanup_invalid_context_rows(conn) -> List[str]:
    """
    Find campaigns where context_rows are missing tactic_en OR missing signal_en
    across ALL rows (non-standard format).
    For each:
      - Delete the unusable context_rows
      - Reset context_status to '❌ Context list not in standard format'
      - Remove any stale validation_results entry
    Returns a list of log lines describing what was cleaned up.
    """
    lines: List[str] = []

    with conn.cursor() as cur:
        # Campaigns where ALL rows have empty tactic_en
        cur.execute("""
            SELECT DISTINCT monday_item_id
            FROM context_rows
            WHERE monday_item_id NOT IN (
                SELECT DISTINCT monday_item_id
                FROM context_rows
                WHERE tactic_en IS NOT NULL AND BTRIM(tactic_en) != ''
            )
        """)
        bad_tactic = [row[0] for row in cur.fetchall()]

        # Campaigns where ALL rows have empty signal_en
        cur.execute("""
            SELECT DISTINCT monday_item_id
            FROM context_rows
            WHERE monday_item_id NOT IN (
                SELECT DISTINCT monday_item_id
                FROM context_rows
                WHERE signal_en IS NOT NULL AND BTRIM(signal_en) != ''
            )
        """)
        bad_signal = [row[0] for row in cur.fetchall()]

    bad_ids = list(set(bad_tactic + bad_signal))

    if not bad_ids:
        return lines

    for item_id in bad_ids:
        with conn.cursor() as cur:
            # Delete the invalid context rows
            cur.execute("DELETE FROM context_rows WHERE monday_item_id = %s", (item_id,))
            deleted = cur.rowcount
            # Correct the context_status on the campaign
            cur.execute("""
                UPDATE campaigns
                SET context_status = '❌ Context list not in standard format',
                    updated_at_utc = %s
                WHERE monday_item_id = %s
            """, (datetime.now(timezone.utc).isoformat(timespec="seconds"), item_id))
            # Remove any stale validation result so it won't appear as validated
            cur.execute("DELETE FROM validation_results WHERE monday_item_id = %s", (item_id,))
        conn.commit()
        lines.append(
            f"  🧹 Cleaned up {deleted} invalid row(s) for campaign {item_id} "
            f"→ status reset to '❌ Context list not in standard format'"
        )

    return lines


def pending_validation_count(conn) -> int:
    """Number of campaigns with context rows not yet validated."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(DISTINCT c.monday_item_id)
                FROM campaigns c
                INNER JOIN context_rows cr ON cr.monday_item_id = c.monday_item_id
                LEFT  JOIN validation_results vr ON vr.monday_item_id = c.monday_item_id
                WHERE vr.monday_item_id IS NULL
            """)
            row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _reconstruct_context_list(conn, item_id: str) -> Dict:
    """
    Rebuild the nested tactics → sub-tactics → signals structure from flat context_rows.
    Preserves insertion order.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT tactic_en, subtactic_en, signal_en
            FROM context_rows
            WHERE monday_item_id = %s
            ORDER BY id
        """, (item_id,))
        rows = [dict(r) for r in cur.fetchall()]

    # Build ordered nested dict: tactic → {subtactic → [(signal, location)]}
    tactics_map: Dict[str, Dict[str, List[str]]] = {}
    all_signals: List[tuple] = []  # (signal_lower, tactic, subtactic, signal_original)

    for row in rows:
        tactic    = (row.get("tactic_en")    or "").strip()
        subtactic = (row.get("subtactic_en") or "").strip()
        signal    = (row.get("signal_en")    or "").strip()
        if not tactic:
            continue
        if tactic not in tactics_map:
            tactics_map[tactic] = {}
        st_key = subtactic or "All"
        if st_key not in tactics_map[tactic]:
            tactics_map[tactic][st_key] = []
        if signal:
            tactics_map[tactic][st_key].append(signal)
            all_signals.append((signal.lower(), tactic, st_key, signal))

    # Pre-compute exact duplicates (case-insensitive) with locations
    from collections import defaultdict
    signal_locations: Dict[str, List[str]] = defaultdict(list)
    for sig_lower, tactic, st_key, sig_orig in all_signals:
        loc = f"'{st_key}' under '{tactic}'"
        if loc not in signal_locations[sig_lower]:
            signal_locations[sig_lower].append(loc)

    exact_duplicates = {
        sig: locs for sig, locs in signal_locations.items()
        if len(locs) > 1
    }

    tactics_list = [
        {
            "tactic_name": tactic_name,
            "sub_tactics": [
                {
                    "sub_tactic_name": st_name,
                    "signals": [
                        {
                            "text": s,
                            "word_count": len(s.split()),
                            "is_exact_duplicate": s.lower() in exact_duplicates,
                            "duplicate_locations": exact_duplicates.get(s.lower(), []),
                        }
                        for s in signals
                    ],
                }
                for st_name, signals in subtactics.items()
            ],
        }
        for tactic_name, subtactics in tactics_map.items()
    ]

    return {
        "exclusions": [],   # Exclusions are not stored in the current schema
        "tactics": tactics_list,
    }


# ---------------------------------------------------------------------------
# OpenAI call
# ---------------------------------------------------------------------------

def _recompute_status(result: Dict) -> Dict:
    """
    Recompute errors_count, warnings_count, overall_status and training_label
    from the actual triggered_rules — overrides whatever the AI self-reported.
    This prevents mismatches where GPT miscounts or contradicts its own rules.
    """
    errors = warnings = recs = 0
    for check in result.get("check_results", []):
        for rule in check.get("triggered_rules", []):
            sev = rule.get("severity", "")
            if sev == "error":
                errors += 1
            elif sev in ("warning", "info"):
                warnings += 1
            elif sev == "recommendation":
                recs += 1

    if errors >= 2:
        status, label, store = "FAIL_MAJOR",         "DO_NOT_STORE",     False
    elif errors == 1:
        status, label, store = "FAIL_MINOR",         "NEGATIVE_EXAMPLE", True
    elif warnings > 0:
        status, label, store = "PASS_WITH_WARNINGS", "POSITIVE_EXAMPLE", True
    else:
        status, label, store = "PASS",               "POSITIVE_EXAMPLE", True

    result["errors_count"]          = errors
    result["warnings_count"]        = warnings
    result["recommendations_count"] = recs
    result["overall_status"]        = status
    result["training_label"]        = label
    result["store_in_training_db"]  = store
    return result


def _load_system_prompt() -> str:
    if not SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"Validation system prompt not found at: {SYSTEM_PROMPT_PATH}\n"
            "Ensure context_list_validation_system_prompt.md is in the same directory."
        )
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _call_openai_validator(
    campaign: Dict, context_list: Dict, api_key: str, system_prompt: str = ""
) -> Dict:
    if _OpenAI is None:
        raise RuntimeError("openai package not installed. Run: pip install openai")
    if not system_prompt:
        system_prompt = _load_system_prompt()
    client = _OpenAI(api_key=api_key)
    campaign_input = {
        "brand":           campaign.get("brand_name")        or "",
        "geo":             campaign.get("country")           or "",
        "vertical":        campaign.get("vertical")          or "",
        "target_audience": campaign.get("targeting")         or "",
        "campaign_brief":  campaign.get("rfp_summary")       or "",
        "budget":          "",
        "age_group":       campaign.get("any_other_details") or "",
        "dma_targeting":   False,
    }
    user_message = json.dumps(
        {"campaign_input": campaign_input, "context_list": context_list},
        ensure_ascii=False,
    )
    for model in ["gpt-4o", "gpt-4o-mini"]:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_message},
                ],
                response_format={"type": "json_object"},
            )
            result = json.loads(response.choices[0].message.content)
            return _recompute_status(result)
        except Exception as e:
            if "429" in str(e) and model == "gpt-4o":
                # Any 429 on gpt-4o (too large or rate limit) — fall back to gpt-4o-mini
                continue
            raise


# ---------------------------------------------------------------------------
# Save result
# ---------------------------------------------------------------------------

def _save_validation_result(
    conn,
    item_id: str,
    campaign: Dict,
    result: Dict,
    error: str = "",
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    validated_at = result.get("validated_at") or now

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO validation_results (
              monday_item_id, campaign_name, brand_name, region,
              validated_at, overall_status, training_label, store_in_training_db,
              errors_count, warnings_count, recommendations_count,
              full_validation_report, error_log, inserted_at_utc
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (monday_item_id) DO UPDATE SET
              validated_at           = EXCLUDED.validated_at,
              overall_status         = EXCLUDED.overall_status,
              training_label         = EXCLUDED.training_label,
              store_in_training_db   = EXCLUDED.store_in_training_db,
              errors_count           = EXCLUDED.errors_count,
              warnings_count         = EXCLUDED.warnings_count,
              recommendations_count  = EXCLUDED.recommendations_count,
              full_validation_report = EXCLUDED.full_validation_report,
              error_log              = EXCLUDED.error_log
        """, (
            item_id,
            campaign.get("campaign_name"),
            campaign.get("brand_name"),
            campaign.get("region"),
            validated_at,
            result.get("overall_status"),
            result.get("training_label"),
            result.get("store_in_training_db"),
            result.get("errors_count", 0),
            result.get("warnings_count", 0),
            result.get("recommendations_count", 0),
            json.dumps(result, ensure_ascii=False) if result else None,
            error or None,
            now,
        ))
    conn.commit()


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_validation(conn, openai_api_key: str) -> str:
    """
    Validate all campaigns that have context rows but haven't been validated yet.
    Returns a human-readable summary string (for display in the dashboard).
    """
    init_validation_schema(conn)

    # Step 0: clean up context rows missing tactic data and correct their status
    cleanup_lines = cleanup_invalid_context_rows(conn)
    lines: List[str] = []
    if cleanup_lines:
        lines.append("🧹 Cleaning up non-standard context rows...")
        lines.extend(cleanup_lines)
        lines.append("")

    campaigns = _get_campaigns_needing_validation(conn)
    if not campaigns:
        lines.append("Nothing to validate — all campaigns with context lists have already been validated.")
        return "\n".join(lines)

    # Use DB prompt override if available; fall back to file
    active_system_prompt = ""
    try:
        from feedback_synthesizer import get_active_system_prompt
        active_system_prompt = get_active_system_prompt(conn)
    except Exception:
        active_system_prompt = _load_system_prompt()

    lines.append(f"Validating {len(campaigns)} campaign(s) using OpenAI (gpt-4o)...\n")
    validated = 0
    errors = 0
    skipped = 0

    for campaign in campaigns:
        item_id = campaign["monday_item_id"]
        name    = campaign.get("campaign_name") or item_id

        try:
            context_list = _reconstruct_context_list(conn, item_id)
            if not context_list["tactics"]:
                # Shouldn't happen after cleanup, but guard anyway
                skipped += 1
                lines.append(f"  ⏭  Skipped (no tactics after cleanup): {name}")
                continue

            result = _call_openai_validator(campaign, context_list, openai_api_key,
                                            system_prompt=active_system_prompt)
            _save_validation_result(conn, item_id, campaign, result)
            validated += 1
            if validated < len(campaigns):
                time.sleep(5)   # avoid hitting TPM limits on back-to-back requests

            status = result.get("overall_status", "?")
            label  = result.get("training_label", "?")
            errs   = result.get("errors_count", 0)
            warns  = result.get("warnings_count", 0)
            lines.append(
                f"  ✅ {name}  →  {status} / {label}  "
                f"(errors: {errs}, warnings: {warns})"
            )

        except Exception as e:
            errors += 1
            err_msg = str(e)
            lines.append(f"  ❌ {name}  →  Error: {err_msg}")
            try:
                _save_validation_result(conn, item_id, campaign, {}, error=err_msg)
            except Exception:
                pass

    lines.append(
        f"\nDone — validated: {validated}, non-standard format: {skipped}, errors: {errors}"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fetch results (for dashboard display)
# ---------------------------------------------------------------------------

def fetch_validation_results(conn) -> "Any":
    """Return all validation_results rows as a DataFrame."""
    try:
        import pandas as pd
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT monday_item_id, region, campaign_name, brand_name,
                       overall_status, training_label,
                       validated_at, error_log, full_validation_report
                FROM validation_results
                ORDER BY validated_at DESC
            """)
            rows = [dict(r) for r in cur.fetchall()]
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    except Exception:
        import pandas as pd
        return pd.DataFrame()
