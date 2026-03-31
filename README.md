# Presales MDB updater

Monday.com → PostgreSQL: campaign ingest + media plan context extraction.  
No OpenAI. See `MDB_SCHEMA.md` for the database layout.

## Run locally

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then fill values
python db_updater.py monday_config.json
streamlit run monitor_dashboard.py
```

## Secrets

- `DATABASE_URL`, `MONDAY_API_KEY` — GitHub Actions, Streamlit Cloud, or `.streamlit/secrets.toml`.

## First push to GitHub

See **GITHUB_SETUP.md** in this folder.
