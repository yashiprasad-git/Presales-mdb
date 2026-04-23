"""
feedback_synthesizer.py — Human feedback loop for validation system prompt.

Stores per-campaign feedback in Supabase, synthesizes it into system prompt
updates via GPT-4o, and persists approved changes back to Supabase so they
survive redeploys.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

def init_feedback_schema(conn) -> None:
    """Create feedback and system prompt override tables if they don't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS validation_feedback (
                id                SERIAL PRIMARY KEY,
                monday_item_id    TEXT UNIQUE NOT NULL,
                campaign_name     TEXT,
                feedback_text     TEXT NOT NULL,
                submitted_at_utc  TEXT NOT NULL,
                updated_at_utc    TEXT,
                is_processed      BOOLEAN DEFAULT FALSE
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS system_prompt_override (
                id                  SERIAL PRIMARY KEY,
                prompt_text         TEXT NOT NULL,
                summary_of_changes  TEXT,
                feedback_snapshot   TEXT,
                synthesized_at_utc  TEXT NOT NULL
            );
        """)
    conn.commit()

    # Migrate existing table: add is_processed column if it doesn't exist yet.
    # Run in a separate block so a duplicate-column error doesn't roll back the above.
    try:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE validation_feedback
                ADD COLUMN IF NOT EXISTS is_processed BOOLEAN DEFAULT FALSE;
            """)
        conn.commit()
    except Exception:
        conn.rollback()


# ---------------------------------------------------------------------------
# Feedback CRUD
# ---------------------------------------------------------------------------

def save_feedback(conn, monday_item_id: str, campaign_name: str, feedback_text: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO validation_feedback
                (monday_item_id, campaign_name, feedback_text, submitted_at_utc, updated_at_utc, is_processed)
            VALUES (%s, %s, %s, %s, %s, FALSE)
            ON CONFLICT (monday_item_id) DO UPDATE SET
                campaign_name    = EXCLUDED.campaign_name,
                feedback_text    = EXCLUDED.feedback_text,
                updated_at_utc   = EXCLUDED.updated_at_utc,
                is_processed     = FALSE
        """, (monday_item_id, campaign_name, feedback_text.strip(), now, now))
    conn.commit()


def get_feedback(conn, monday_item_id: str) -> str:
    """Return feedback text for a campaign, or empty string if none."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT feedback_text FROM validation_feedback WHERE monday_item_id = %s",
                (monday_item_id,)
            )
            row = cur.fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def get_feedback_status(conn, monday_item_id: str) -> str:
    """Return 'processed', 'pending', or '' (no feedback) for a campaign."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_processed FROM validation_feedback WHERE monday_item_id = %s",
                (monday_item_id,)
            )
            row = cur.fetchone()
        if row is None:
            return ""
        return "processed" if row[0] else "pending"
    except Exception:
        return ""


def delete_feedback(conn, monday_item_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM validation_feedback WHERE monday_item_id = %s", (monday_item_id,))
    conn.commit()


def mark_feedback_processed(conn) -> None:
    """Mark all pending feedback as processed — called after a synthesis is applied."""
    with conn.cursor() as cur:
        cur.execute("UPDATE validation_feedback SET is_processed = TRUE WHERE is_processed = FALSE")
    conn.commit()


def get_all_feedback(conn) -> List[Dict]:
    """Return only unprocessed (pending) feedback entries ordered by submission time."""
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT monday_item_id, campaign_name, feedback_text, submitted_at_utc
                FROM validation_feedback
                WHERE is_processed = FALSE
                ORDER BY submitted_at_utc ASC
            """)
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def feedback_count(conn) -> int:
    """Count only pending (unprocessed) feedback entries."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM validation_feedback WHERE is_processed = FALSE")
            row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# System prompt: DB override + file fallback
# ---------------------------------------------------------------------------

def get_active_system_prompt(conn) -> str:
    """
    Return the active system prompt.
    Checks DB for an approved override first; falls back to the file on disk.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT prompt_text FROM system_prompt_override ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        if row:
            return row[0]
    except Exception:
        pass
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def get_prompt_override_info(conn) -> Optional[Dict]:
    """Return metadata about the current DB override, or None if using file."""
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, summary_of_changes, synthesized_at_utc
                FROM system_prompt_override
                ORDER BY id DESC LIMIT 1
            """)
            row = cur.fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def apply_prompt_override(conn, new_prompt: str, summary: str, feedback_snapshot: List[Dict]) -> None:
    """Save an approved synthesized prompt to DB."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO system_prompt_override
                (prompt_text, summary_of_changes, feedback_snapshot, synthesized_at_utc)
            VALUES (%s, %s, %s, %s)
        """, (new_prompt, summary, json.dumps(feedback_snapshot, ensure_ascii=False), now))
    conn.commit()


def revert_prompt_override(conn) -> None:
    """Delete all DB overrides — reverts to file-based system prompt."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM system_prompt_override")
    conn.commit()


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------

def synthesize_feedback(conn, openai_api_key: str) -> Tuple[str, str, List[str]]:
    """
    Send current system prompt + all feedback to GPT-4o.
    Returns (new_prompt, summary, list_of_change_bullets).
    """
    if _OpenAI is None:
        raise RuntimeError("openai package not installed.")

    all_feedback = get_all_feedback(conn)
    if not all_feedback:
        raise ValueError("No feedback to synthesize.")

    current_prompt = get_active_system_prompt(conn)

    feedback_text = "\n\n".join([
        f"Campaign: {fb['campaign_name']}\nFeedback: {fb['feedback_text']}"
        for fb in all_feedback
    ])

    synthesis_prompt = f"""You are updating a validation system prompt based on human feedback from campaign reviewers.

CURRENT SYSTEM PROMPT:
---
{current_prompt}
---

HUMAN FEEDBACK ON RECENT VALIDATIONS:
---
{feedback_text}
---

Your task:
1. Read the feedback carefully and identify which rules produced incorrect results (false positives, false negatives, wrong reasoning)
2. Make the minimum necessary changes to the system prompt rules to address the feedback
3. Do NOT make speculative or unnecessary changes — only change what the feedback clearly supports
4. Do NOT change the overall structure, training label logic, or output format
5. Return a JSON object with:
   - "summary_of_changes": a brief 1-2 sentence summary of what changed overall
   - "change_bullets": a list of specific changes made (one bullet per rule changed)
   - "updated_system_prompt": the complete updated system prompt text

Return only valid JSON."""

    client = _OpenAI(api_key=openai_api_key)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": synthesis_prompt}],
        response_format={"type": "json_object"},
    )
    result = json.loads(response.choices[0].message.content)

    new_prompt     = result.get("updated_system_prompt", "")
    summary        = result.get("summary_of_changes", "")
    change_bullets = result.get("change_bullets", [])

    if not new_prompt:
        raise ValueError("GPT-4o returned empty system prompt.")

    return new_prompt, summary, change_bullets
