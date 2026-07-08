import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
LOGS_DIR = ROOT_DIR / "logs"
LATEST_ARTIFACT_PATH = LOGS_DIR / "daily_job_latest.json"


def run_command(args):
    completed = subprocess.run(args, cwd=ROOT_DIR, check=True, capture_output=True, text=True)
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    return completed


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def delete_remote_file(client, file_name):
    if not file_name:
        return False

    try:
        client.files.delete(name=file_name)
        return True
    except Exception:
        return False


def main():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    print("Running scraper...")
    run_command([sys.executable, "scripts/scrape.py"])

    manifest_path = ROOT_DIR / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit("manifest.json was not created by the scraper.")

    manifest = load_json(manifest_path)
    stats = manifest.get("stats", {})
    added = int(stats.get("new", 0))
    updated = int(stats.get("modified", 0))
    skipped = int(stats.get("unchanged", 0))
    deleted = int(stats.get("deleted", 0))

    deleted_remote = 0
    deleted_records = manifest.get("deleted_articles", [])
    if deleted_records:
        from dotenv import load_dotenv
        from google import genai

        load_dotenv()
        api_key = os.getenv("API_KEY", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
        if api_key:
            client = genai.Client(api_key=api_key)
            for record in deleted_records:
                deleted_remote += int(delete_remote_file(client, record.get("gemini_file_name")))

    print("Running uploader...")
    run_command([sys.executable, "scripts/upload_gemini_kb.py"])

    upload_summary_path = ROOT_DIR / "logs" / "gemini_upload_summary.json"
    upload_summary = load_json(upload_summary_path) if upload_summary_path.exists() else {}

    artifact = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "deleted": deleted,
            "deleted_remote": deleted_remote,
        },
        "manifest_path": str(manifest_path),
        "upload_summary_path": str(upload_summary_path),
        "uploaded_files": upload_summary.get("files_uploaded", 0),
        "failed_uploads": upload_summary.get("files_failed", 0),
        "estimated_chunks": upload_summary.get("estimated_chunks", 0),
        "notes": [
            "added = new articles discovered in the latest scrape",
            "updated = existing articles whose md5 changed",
            "skipped = unchanged articles whose md5 matched the previous manifest",
        ],
    }

    LATEST_ARTIFACT_PATH.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")

    dated_artifact_path = LOGS_DIR / f"daily_job_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    dated_artifact_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\nDaily job artifact")
    print(f"- added:   {added}")
    print(f"- updated: {updated}")
    print(f"- skipped: {skipped}")
    print(f"- deleted_remote: {deleted_remote}")
    print(f"- artifact: {dated_artifact_path}")
    print(f"- latest:   {LATEST_ARTIFACT_PATH}")


if __name__ == "__main__":
    main()