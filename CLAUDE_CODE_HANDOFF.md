# Project Handoff: Context List Validation Engine
# For: Claude Code
# Company: Silverpush | Product: YouTube Mirror Campaigns

---

## WHAT THIS PROJECT IS

Silverpush runs YouTube Mirror campaigns. For each campaign, a human creates a
"Context List" — a structured targeting document with:
- Tactics (main content categories, e.g. "Toys & Games")
- Sub-Tactics (sub-categories under each Tactic, e.g. "Educational Toys")
- Signals (YouTube keywords under each Sub-Tactic, e.g. "STEM Learning Toys")
- Exclusions (brand safety categories and keywords to block)

These context lists are created based on campaign inputs:
brand, geo, vertical, target audience, campaign brief, budget, age group.

---

## THE PROBLEM BEING SOLVED

Silverpush is training an AI model to auto-generate context lists.
To train it well, they need high-quality past context lists as training data.
But before any context list enters the training database, it must be validated.

This project builds that validation layer.

---

## EXISTING SYSTEM (already built)

A Python pipeline that:
- Pulls campaign input data + context lists from Monday.com
- Saves them to a Supabase database

This pipeline already exists, lives on GitHub, and executes on GitHub.
Do NOT rebuild it. The new validation layer sits ON TOP of this existing pipeline.

IMPORTANT — GitHub specific instructions:
- All new code must be committed to the existing GitHub repo
- Store the OpenAI API key as a GitHub Secret (Settings → Secrets → Actions)
  and reference it in code as: os.getenv("OPENAI_API_KEY")
- Commit both reference files to the repo so the pipeline can read them at runtime:
  context_list_validation_instructions.json
  context_list_validation_system_prompt.md
- Do NOT put the API key in any code file or commit it to the repo

---

## WHAT NEEDS TO BE BUILT

A validation pipeline that:
1. Reads a context list + campaign input from Supabase
2. Calls OpenAI API (gpt-4o) using the system prompt in: context_list_validation_system_prompt.md
3. Parses the structured JSON validation output returned by gpt-4o
4. Saves the result back to Supabase with these fields:
   - overall_status (PASS / PASS_WITH_WARNINGS / FAIL_MINOR / FAIL_MAJOR)
   - training_label (POSITIVE_EXAMPLE / NEGATIVE_EXAMPLE / DO_NOT_STORE)
   - store_in_training_db (boolean)
   - errors_count, warnings_count, recommendations_count
   - full_validation_report (complete JSON from OpenAI)
   - validated_at (timestamp)

---

## THE TWO REFERENCE FILES IN THIS REPO

### 1. context_list_validation_instructions.json
The full rule set — 3 checks, rules, severity levels, training label logic, playbook data.
Use this to understand the validation logic.

### 2. context_list_validation_system_prompt.md
The AI system prompt passed to OpenAI as the system message.
gpt-4o evaluates the context list against this prompt and returns structured JSON.

---

## VALIDATION LOGIC SUMMARY

### CHECK 1 — Layout & Signal Quality
- NO errors. Only warnings and info.
- 3 rules always shown (signal length > 3 words, exact duplicates, proper noun duplicates)
- Never affects training label

### CHECK 2 — Targeting & Brief Alignment
- THE ONLY CHECK WITH ERRORS
- 3 rules, all severity: error
- Error count here = determines training label

### CHECK 3 — Thematic Alignment
- ALWAYS recommendations only, never errors
- Triggers when vertical is sensitive OR targeting is niche
- Suggests broader Tactics from vertical playbook to improve delivery scale

---

## TRAINING LABEL LOGIC

| Errors | Status | Training Label | Store? |
|--------|--------|---------------|--------|
| 0, no warnings | PASS | POSITIVE_EXAMPLE | YES |
| 0, warnings exist | PASS_WITH_WARNINGS | POSITIVE_EXAMPLE | YES |
| 1 error | FAIL_MINOR | NEGATIVE_EXAMPLE | YES |
| 2+ errors | FAIL_MAJOR | DO_NOT_STORE | NO |

IMPORTANT: Warnings are NEVER counted as errors.
Context lists have large signal volumes — warning counts will naturally be high.
A list with 50 warnings and 0 errors = PASS_WITH_WARNINGS = POSITIVE_EXAMPLE.

Both POSITIVE and NEGATIVE examples are stored (where store=true).
The model learns from correct examples AND from labelled mistakes with reasoning.
Only FAIL_MAJOR (2+ errors) is excluded — too broken to be useful for training.

---

## TECH STACK

- Language: Python
- Database: Supabase (PostgreSQL)
- AI: OpenAI API (use model: gpt-4o)
- Version control: GitHub (pipeline runs on GitHub)
- Existing pipeline: Monday.com → Supabase (already working, on GitHub)

## API SETUP

Use OpenAI Python SDK:
```python
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message}
    ],
    response_format={"type": "json_object"}
)

result = json.loads(response.choices[0].message.content)
```

IMPORTANT: Use response_format={"type": "json_object"} to ensure
OpenAI always returns valid JSON — critical for parsing the validation report.

SECRETS MANAGEMENT — READ CAREFULLY:
- Claude Code must NEVER access, read, edit or touch Streamlit secrets
- Claude Code must NEVER hardcode any API keys in any file
- All secrets (OpenAI, Supabase) are stored manually by the user in Streamlit Cloud
- In code, access them using st.secrets like this:

```python
import streamlit as st
openai_api_key = st.secrets["OPENAI_API_KEY"]
client = OpenAI(api_key=openai_api_key)
```

```python
# Supabase (already configured the same way in existing code)
supabase_url = st.secrets["SUPABASE_URL"]
supabase_key = st.secrets["SUPABASE_KEY"]
```

- Claude Code only writes the code that references st.secrets
- The actual secret values are added manually by the user in Streamlit dashboard
- Claude Code pushes all code files to GitHub — nothing else

---

## FIRST STEPS FOR CLAUDE CODE

1. Read context_list_validation_instructions.json
2. Read context_list_validation_system_prompt.md
3. Look at existing GitHub repo structure and Supabase schema
4. Build the validation pipeline as described above
5. Add new columns or table to Supabase for validation output fields
6. Commit all new files to the GitHub repo
