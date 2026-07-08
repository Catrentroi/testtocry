# OptiBot KB Loader

Scrapes OptiSigns help-center articles, tracks them in `manifest.json`, uploads new/updated Markdown to Gemini, and runs as a daily job.

## Setup

```powershell
cd C:\Users\Admin\Desktop\code\testtocry
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.sample .env
```

Set `API_KEY` in `.env`.

## Run locally

```powershell
.\venv\Scripts\python.exe main.py
```

Artifacts/logs:
- `logs/daily_job_latest.json`
- `logs/daily_job_YYYYMMDD_HHMMSS.json`
- `logs/gemini_upload_summary.json`

## Docker

```powershell
docker build -t optibot-job .
docker run --rm --env-file .env optibot-job
```

## Sample assistant screenshot

Ask in Gemini AI Studio:

```text
How do I add a YouTube video?
```

Use the answer with cited `Article URL:` lines for the screenshot.

## Notes

- `manifest.json` stores `article_id`, `slug`, `md5`, and upload metadata.
- Chunking is logged as a deterministic estimate (`800` tokens with `200` overlap).
- Daily job counts: `added`, `updated`, `skipped`, `deleted_remote`.
