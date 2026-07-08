from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
import requests
from markdownify import markdownify

BASE_URL = "https://support.optisigns.com"
ARTICLES_API = f"{BASE_URL}/api/v2/help_center/articles.json"
DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"
MANIFEST_PATH = Path(__file__).resolve().parents[1] / "manifest.json"


def fetch_articles():
    articles = []
    next_page = ARTICLES_API
    session = requests.Session()

    while next_page:
        response = session.get(next_page, timeout=30)
        response.raise_for_status()

        data = response.json()
        articles.extend(data.get("articles", []))
        next_page = data.get("next_page")

    return articles


def slugify(title):
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "article"


def load_previous_manifest():
    if not MANIFEST_PATH.exists():
        return {"articles": {}, "deleted_articles": []}

    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(data.get("articles"), dict):
        data["articles"] = {}
    if not isinstance(data.get("deleted_articles"), list):
        data["deleted_articles"] = []
    return data


def md5_text(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def convert_to_markdown(article):
    title = article["title"].strip()
    source_url = article.get("html_url", "")
    body_html = article.get("body", "")
    body_markdown = markdownify(body_html, heading_style="ATX").strip()

    parts = [f"# {title}"]

    if source_url:
        parts.append(f"Article URL: {source_url}")

    if body_markdown:
        parts.append("")
        parts.append(body_markdown)

    return "\n".join(parts).rstrip() + "\n"


def save_markdown(article, markdown_content):
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    slug = slugify(article["title"])
    file_path = DOCS_DIR / f"{slug}.md"

    file_path.write_text(markdown_content, encoding="utf-8")
    return file_path


def build_article_record(article, markdown_content, previous_record):
    article_id = str(article["id"])
    slug = slugify(article["title"])
    file_name = f"{slug}.md"
    content_md5 = md5_text(markdown_content)

    if not previous_record:
        status = "new"
    elif previous_record.get("md5") == content_md5 and previous_record.get("file_name") == file_name:
        status = "unchanged"
    else:
        status = "modified"

    record = {
        "article_id": article_id,
        "title": article["title"].strip(),
        "slug": slug,
        "file_name": file_name,
        "source_url": article.get("html_url", ""),
        "md5": content_md5,
        "status": status,
    }

    previous_gemini_file_name = previous_record.get("gemini_file_name")
    if previous_gemini_file_name:
        if status == "unchanged":
            record["gemini_file_name"] = previous_gemini_file_name
            if previous_record.get("gemini_uploaded_at"):
                record["gemini_uploaded_at"] = previous_record.get("gemini_uploaded_at")
            if previous_record.get("upload_status"):
                record["upload_status"] = previous_record.get("upload_status")
        else:
            record["previous_gemini_file_name"] = previous_gemini_file_name
            if previous_record.get("gemini_uploaded_at"):
                record["previous_gemini_uploaded_at"] = previous_record.get("gemini_uploaded_at")

    return record


def main():
    articles = fetch_articles()
    previous_manifest = load_previous_manifest()
    previous_articles = previous_manifest.get("articles", {})

    print(f"Total articles fetched: {len(articles)}")

    saved_files = []
    current_articles = {}
    current_ids = set()
    deleted_articles = []
    stats = {"new": 0, "modified": 0, "unchanged": 0, "deleted": 0}

    for article in articles:
        markdown_content = convert_to_markdown(article)
        article_id = str(article["id"])
        previous_record = previous_articles.get(article_id, {})
        record = build_article_record(article, markdown_content, previous_record)
        current_articles[article_id] = record
        current_ids.add(article_id)

        previous_file_name = previous_record.get("file_name")
        if previous_file_name and previous_file_name != record["file_name"]:
            stale_path = DOCS_DIR / previous_file_name
            if stale_path.exists():
                stale_path.unlink()

        if record["status"] != "unchanged" or not (DOCS_DIR / record["file_name"]).exists():
            file_path = save_markdown(article, markdown_content)
            saved_files.append(file_path)

        stats[record["status"]] = stats.get(record["status"], 0) + 1

    for article_id, previous_record in previous_articles.items():
        if article_id in current_ids:
            continue

        deleted_articles.append(previous_record)
        stats["deleted"] += 1
        stale_path = DOCS_DIR / previous_record.get("file_name", "")
        if stale_path.exists():
            stale_path.unlink()

    print(f"Saved {len(saved_files)} markdown files to {DOCS_DIR}")

    for file_path in saved_files[:3]:
        print(f"- {file_path.name}")

    manifest = {
        "source": BASE_URL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "articles": current_articles,
        "deleted_articles": deleted_articles,
        "stats": stats,
    }

    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote manifest: {MANIFEST_PATH}")
    print(
        f"Manifest stats: new={stats['new']}, modified={stats['modified']}, unchanged={stats['unchanged']}, deleted={stats['deleted']}"
    )


if __name__ == "__main__":
    main()