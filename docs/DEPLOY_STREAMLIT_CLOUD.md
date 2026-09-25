# Deploy QuoteIQ free on Streamlit Community Cloud

Streamlit Community Cloud is suitable for the assignment demo because this repository already uses Streamlit, Python 3.12 and `requirements.txt`. It provides a public HTTPS URL and secret management.

## Important demo limitation

Community Cloud's local filesystem is ephemeral. QuoteIQ's SQLite database and preserved source files can reset when the app restarts, sleeps, is redeployed or moves to another machine. Prepare the demo workspace before sharing or recording. This free deployment is not durable procurement storage.

The app is a single shared workspace. A visitor can change or reset the same demo data. Share the URL only with the evaluator during the assignment window, and use only fictional source documents.

## 1. Publish the repository to GitHub

The repository must include:

- `streamlit_app.py`
- `requirements.txt`
- `.python-version`
- `.streamlit/config.toml`
- `quoteiq/`

It must not include `.env`, `.streamlit/secrets.toml`, `data/` or generated local logs. `.gitignore` already excludes those paths.

If using Visual Studio Code:

1. Open Source Control.
2. Choose **Publish to GitHub**.
3. Sign in to GitHub if prompted.
4. Create a repository named `quoteiq-aerchain`.
5. Public is simplest for Community Cloud; private also works if Streamlit receives repository permission.
6. Verify on GitHub that `.env` and `data/` are absent before continuing.

Alternatively, create an empty GitHub repository and run:

```powershell
git remote add origin https://github.com/YOUR_USERNAME/quoteiq-aerchain.git
git branch -M main
git push -u origin main
```

## 2. Deploy on Streamlit Community Cloud

1. Open <https://share.streamlit.io>.
2. Sign in with GitHub and authorize access to the QuoteIQ repository.
3. Select **Create app** → **Yup, I have an app**.
4. Choose the `quoteiq-aerchain` repository and `main` branch.
5. Set the main file path to `streamlit_app.py`.
6. Open **Advanced settings**.
7. Select Python **3.12**.
8. Paste the following into **Secrets**, replacing only the API key:

```toml
AI_PROVIDER = "gemini"
GEMINI_API_KEY = "YOUR_REAL_GEMINI_KEY"
GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_TIMEOUT_SECONDS = "180"
QUOTEIQ_DATA_DIR = "data"
```

9. Click **Deploy** and wait for dependencies to install.

Root-level Streamlit secrets become environment variables, so QuoteIQ reads them without code changes.

## 3. Verify the public build

1. Open the generated `https://…streamlit.app` URL in an incognito window.
2. Confirm the sidebar shows `AI provider: gemini` and `gemini-3.5-flash-lite`.
3. Use **Prepare demo workspace**.
4. Confirm the five original source files appear in Quote inbox.
5. Extract one source first to verify Gemini connectivity and free-tier quota.
6. Extract all five, review the dataset, run an analyst question and download an export.
7. Refresh the page and confirm the current session still loads.

## 4. Finish the submission

1. Copy the public application URL.
2. Replace `[ADD LIVE URL]` in the final presentation.
3. Record the Loom walkthrough against the public app if performance and quota are stable.
4. Add the Loom URL to the presentation and submission email.
5. Verify both links in a signed-out/incognito browser.

## Troubleshooting

- **App cannot import a package:** check the Community Cloud build log and `requirements.txt`.
- **AI key not configured:** ensure the secrets are root-level TOML values and reboot the app after saving them.
- **Gemini 429:** wait for the free-tier project quota shown in Google AI Studio or record after quota resets.
- **Gemini timeout:** keep `GEMINI_TIMEOUT_SECONDS = "180"`; extract one file at a time if the service is busy.
- **Demo data disappeared:** Community Cloud restarted its ephemeral filesystem; prepare and extract the fictional demo again.
- **Repository not visible:** reconnect GitHub in Streamlit settings and grant repository access.
