# Create the MDB-only GitHub repo (first time)

## 1. Create an empty repository on GitHub

1. Open [https://github.com/new](https://github.com/new)
2. **Repository name:** e.g. `presales-mdb` (any name you like)
3. **Private** recommended (IT + internal use)
4. Do **not** add a README, `.gitignore`, or license (this folder already has files)
5. Click **Create repository**

## 2. Push this folder from your Mac

In Terminal:

```bash
cd ~/Desktop/presales-mdb

git init
git add .
git commit -m "Initial commit: MDB updater and monitor dashboard"

git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
git push -u origin main
```

Replace `YOUR_USERNAME` and `YOUR_REPO_NAME` with what GitHub shows after step 1.

If GitHub asks for authentication, use a **Personal Access Token** (not your account password) as the password, or use SSH:

```bash
git remote add origin git@github.com:YOUR_USERNAME/YOUR_REPO_NAME.git
```

## 3. GitHub Actions secrets

In the **new** repo: **Settings → Secrets and variables → Actions**

Add:

| Name | Value |
|------|--------|
| `MONDAY_API_KEY` | Your Monday API token |
| `DATABASE_URL` | PostgreSQL connection string |

Same names as before so the workflow keeps working.

## 4. Streamlit Cloud (monitor dashboard)

Deploy a new app from this repo; main file: `monitor_dashboard.py` (root of repo).

---

After this, you can remove IT’s access from the old combined repo if you only want them on `presales-mdb`.
