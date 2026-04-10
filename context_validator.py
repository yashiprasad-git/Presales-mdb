"""
context_validator.py — Context list validation engine for YouTube Mirror campaigns.

Reads campaigns + context_rows from Supabase, calls OpenAI gpt-4o to validate
each context list against the system prompt, and saves results back to Supabase.

Secrets (via st.secrets or env): OPENAI_API_KEY, DATABASE_URL
"""

import json
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

try:
    import anthropic as _anthropic
except ImportError:
    _anthropic = None  # type: ignore

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
               OR vr.error_log LIKE '%non-standard format%'
            ORDER BY c.monday_item_id
        """)
        return [dict(r) for r in cur.fetchall()]


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

    # Build ordered nested dict: tactic → {subtactic → [signals]}
    tactics_map: Dict[str, Dict[str, List[str]]] = {}
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

    tactics_list = [
        {
            "tactic_name": tactic_name,
            "sub_tactics": [
                {"sub_tactic_name": st_name, "signals": signals}
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
# AI call (supports OpenAI gpt-4o and Anthropic claude-sonnet)
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    if not SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"Validation system prompt not found at: {SYSTEM_PROMPT_PATH}\n"
            "Ensure context_list_validation_system_prompt.md is in the same directory."
        )
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _build_inputs(campaign: Dict, context_list: Dict):
    campaign_input = {
        "brand":           campaign.get("brand_name")       or "",
        "geo":             campaign.get("country")          or "",
        "vertical":        campaign.get("vertical")         or "",
        "target_audience": campaign.get("targeting")        or "",
        "campaign_brief":  campaign.get("rfp_summary")      or "",
        "budget":          "",
        "age_group":       campaign.get("any_other_details") or "",
        "dma_targeting":   False,
    }
    user_message = json.dumps(
        {"campaign_input": campaign_input, "context_list": context_list},
        ensure_ascii=False,
    )
    return user_message


def _call_openai_validator(campaign: Dict, context_list: Dict, api_key: str) -> Dict:
    if _OpenAI is None:
        raise RuntimeError("openai package not installed. Run: pip install openai")
    system_prompt = _load_system_prompt()
    client = _OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": _build_inputs(campaign, context_list)},
        ],
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


def _call_anthropic_validator(campaign: Dict, context_list: Dict, api_key: str) -> Dict:
    if _anthropic is None:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
    system_prompt = _load_system_prompt()
    client = _anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=system_prompt,
        messages=[
            {"role": "user", "content": _build_inputs(campaign, context_list)},
        ],
    )
    return json.loads(message.content[0].text)


def _call_ai_validator(campaign: Dict, context_list: Dict, openai_api_key: str = "", anthropic_api_key: str = "") -> Dict:
    """Call whichever AI provider has a key configured. OpenAI takes priority."""
    if openai_api_key:
        return _call_openai_validator(campaign, context_list, openai_api_key)
    if anthropic_api_key:
        return _call_anthropic_validator(campaign, context_list, anthropic_api_key)
    raise RuntimeError("No AI API key configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY in Streamlit secrets.")


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

def run_validation(conn, openai_api_key: str = "", anthropic_api_key: str = "") -> str:
    """
    Validate all campaigns that have context rows but haven't been validated yet.
    Uses OpenAI if OPENAI_API_KEY is set, otherwise falls back to Anthropic.
    Returns a human-readable summary string (for display in the dashboard).
    """
    init_validation_schema(conn)

    campaigns = _get_campaigns_needing_validation(conn)
    if not campaigns:
        return "Nothing to validate — all campaigns with context lists have already been validated."

    provider = "OpenAI (gpt-4o)" if openai_api_key else "Anthropic (claude-sonnet-4-6)"
    lines: List[str] = [f"Validating {len(campaigns)} campaign(s) using {provider}...\n"]
    validated = 0
    errors = 0
    skipped = 0

    for campaign in campaigns:
        item_id = campaign["monday_item_id"]
        name    = campaign.get("campaign_name") or item_id

        try:
            context_list = _reconstruct_context_list(conn, item_id)
            if not context_list["tactics"]:
                skipped += 1
                lines.append(f"  ❌ {name}  →  No tactics found in context rows (non-standard format)")
                _save_validation_result(conn, item_id, campaign, {
                    "overall_status":         "FAIL_MAJOR",
                    "training_label":         "DO_NOT_STORE",
                    "store_in_training_db":   False,
                    "errors_count":           1,
                    "warnings_count":         0,
                    "recommendations_count":  0,
                    "validated_at":           datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }, error="Context rows exist but contain no tactic data — context list is not in standard format.")
                continue

            result = _call_ai_validator(
                campaign, context_list,
                openai_api_key=openai_api_key,
                anthropic_api_key=anthropic_api_key,
            )
            _save_validation_result(conn, item_id, campaign, result)
            validated += 1

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
                       overall_status, training_label, store_in_training_db,
                       errors_count, warnings_count, recommendations_count,
                       validated_at, error_log
                FROM validation_results
                ORDER BY validated_at DESC
            """)
            rows = [dict(r) for r in cur.fetchall()]
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    except Exception:
        import pandas as pd
        return pd.DataFrame()
