import argparse
import json
import math
import os
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from google import genai
from google.genai import types

SYSTEM_PROMPT = """You are OptiBot, the customer-support bot for OptiSigns.com.

• Tone: helpful, factual, concise.

• Only answer using the uploaded docs.

• Max 5 bullet points; else link to the doc.

• Cite up to 3 \"Article URL:\" lines per reply."""

CHUNK_SIZE_TOKENS = 800
CHUNK_OVERLAP_TOKENS = 200
DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"
MANIFEST_PATH = Path(__file__).resolve().parents[1] / "manifest.json"
LOGS_DIR = Path(__file__).resolve().parents[1] / "logs"
SUMMARY_PATH = LOGS_DIR / "gemini_upload_summary.json"


def approx_token_count(text):
    return max(1, len(text) // 4)


def estimate_chunks(token_count, chunk_size, overlap):
    if token_count <= chunk_size:
        return 1
    stride = max(1, chunk_size - overlap)
    return 1 + math.ceil((token_count - chunk_size) / stride)


def load_manifest():
    if not MANIFEST_PATH.exists():
        return None

    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(data.get("articles"), dict):
        data["articles"] = {}
    return data


def save_manifest(manifest_data):
    MANIFEST_PATH.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_markdown_files(limit=None, include_keyword=None, prioritize_keyword=False, manifest_data=None):
    if manifest_data and manifest_data.get("articles"):
        records = list(manifest_data["articles"].values())
        files = [
            record
            for record in records
            if record.get("status") in {"new", "modified", "restored"}
        ]

        if include_keyword:
            keyword = include_keyword.lower().strip()
            keyword_records = [
                record
                for record in files
                if keyword in record.get("file_name", "").lower() or keyword in record.get("slug", "").lower()
            ]

            if prioritize_keyword:
                ordered = keyword_records + [record for record in files if record not in keyword_records]
                files = ordered
            else:
                files = keyword_records

        if limit is not None and limit > 0:
            return files[:limit]
        return files

    files = sorted(DOCS_DIR.glob("*.md"))

    if include_keyword:
        keyword = include_keyword.lower().strip()
        keyword_files = [path for path in files if keyword in path.name.lower()]

        if prioritize_keyword:
            ordered = keyword_files + [path for path in files if path not in keyword_files]
            files = ordered
        else:
            files = keyword_files

    if limit is not None and limit > 0:
        return files[:limit]
    return files


def create_cache_if_requested(client, model, kb_name, uploaded_results):
    uploaded_items = [item for item in uploaded_results if item.get("status") == "uploaded"]
    if not uploaded_items:
        return None, "No uploaded files available for cache creation."

    parts = []
    for item in uploaded_items:
        uri = item.get("uri")
        mime_type = item.get("mime_type") or "text/markdown"
        if not uri:
            continue
        parts.append(types.Part.from_uri(file_uri=uri, mime_type=mime_type))

    if not parts:
        return None, "Uploaded files do not expose URI for cache creation."

    try:
        cache = client.caches.create(
            model=model,
            config=types.CreateCachedContentConfig(
                display_name=kb_name,
                system_instruction=SYSTEM_PROMPT,
                contents=[types.Content(role="user", parts=parts)],
            ),
        )
        return cache, None
    except Exception as exc:
        return None, str(exc)


def delete_remote_file_if_present(client, file_name):
    if not file_name:
        return False, None

    try:
        client.files.delete(name=file_name)
        return True, None
    except Exception as exc:
        return False, str(exc)


def main():
    parser = argparse.ArgumentParser(description="Upload markdown docs to Gemini Files API.")
    parser.add_argument("--limit", type=int, default=0, help="Upload only first N markdown files.")
    parser.add_argument(
        "--include-keyword",
        type=str,
        default="",
        help="Upload only files whose name contains this keyword.",
    )
    parser.add_argument(
        "--prioritize-keyword",
        action="store_true",
        help="Place keyword-matching files first, then fill remaining slots to satisfy --limit.",
    )
    parser.add_argument(
        "--create-cache",
        action="store_true",
        help="Attempt to create a Gemini cache from uploaded files.",
    )
    args = parser.parse_args()

    load_dotenv()

    api_key = os.getenv("API_KEY", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    kb_name = os.getenv("GEMINI_KB_NAME", "optibot-kb").strip()

    if not api_key:
        raise SystemExit("GEMINI_API_KEY is missing. Set it in .env")

    if not DOCS_DIR.exists():
        raise SystemExit(f"Docs directory not found: {DOCS_DIR}")

    manifest_data = load_manifest()

    markdown_files = get_markdown_files(
        limit=args.limit if args.limit > 0 else None,
        include_keyword=args.include_keyword,
        prioritize_keyword=args.prioritize_keyword,
        manifest_data=manifest_data,
    )
    if not markdown_files:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        summary = {
            "provider": "gemini",
            "kb_name": kb_name,
            "model": model,
            "chunking_strategy": {
                "type": "static-estimate",
                "max_chunk_size_tokens": CHUNK_SIZE_TOKENS,
                "chunk_overlap_tokens": CHUNK_OVERLAP_TOKENS,
            },
            "files_discovered": 0,
            "files_uploaded": 0,
            "files_failed": 0,
            "estimated_chunks": 0,
            "cache_name": None,
            "cache_error": None,
            "results": [],
        }

        SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print("No new or modified markdown files to upload.")
        print(f"Summary written to: {SUMMARY_PATH}")

        if manifest_data and manifest_data.get("articles"):
            manifest_data["last_upload_summary"] = {
                "files_discovered": 0,
                "files_uploaded": 0,
                "files_failed": 0,
                "estimated_chunks": 0,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            save_manifest(manifest_data)

        return

    client = genai.Client(api_key=api_key)

    uploaded = 0
    failed = 0
    estimated_chunks_total = 0
    results = []

    print(f"Markdown files to upload: {len(markdown_files)}")

    for target in markdown_files:
        if isinstance(target, dict):
            file_name = target.get("file_name", "")
            path = DOCS_DIR / file_name
            previous_remote_file_name = target.get("previous_gemini_file_name", "")
            target_status = target.get("status", "")
        else:
            path = target
            file_name = path.name
            previous_remote_file_name = ""
            target_status = ""

        if target_status == "modified" and previous_remote_file_name:
            deleted, delete_error = delete_remote_file_if_present(client, previous_remote_file_name)
            if deleted:
                print(f"Deleted previous Gemini file: {previous_remote_file_name}")
            elif delete_error:
                print(f"Failed to delete previous Gemini file {previous_remote_file_name}: {delete_error}")

        text = path.read_text(encoding="utf-8")
        token_count = approx_token_count(text)
        estimated_chunks = estimate_chunks(token_count, CHUNK_SIZE_TOKENS, CHUNK_OVERLAP_TOKENS)

        try:
            gemini_file = client.files.upload(file=str(path))
            uploaded += 1
            estimated_chunks_total += estimated_chunks

            gemini_file_name = getattr(gemini_file, "name", None)
            item = {
                "file_name": file_name,
                "status": "uploaded",
                "gemini_file_name": gemini_file_name,
                "display_name": getattr(gemini_file, "display_name", file_name),
                "uri": getattr(gemini_file, "uri", None),
                "mime_type": getattr(gemini_file, "mime_type", "text/markdown"),
                "estimated_chunks": estimated_chunks,
                "approx_tokens": token_count,
            }
            results.append(item)
            print(f"Uploaded: {file_name}")

            if manifest_data and manifest_data.get("articles"):
                for article_id, record in manifest_data["articles"].items():
                    if record.get("file_name") == file_name:
                        record["gemini_file_name"] = gemini_file_name
                        record["gemini_uploaded_at"] = datetime.now(timezone.utc).isoformat()
                        record["upload_status"] = "uploaded"
                        break
        except Exception as exc:
            failed += 1
            results.append(
                {
                    "file_name": file_name,
                    "status": "failed",
                    "error": str(exc),
                    "estimated_chunks": estimated_chunks,
                    "approx_tokens": token_count,
                }
            )
            print(f"Failed: {file_name} -> {exc}")

            if manifest_data and manifest_data.get("articles"):
                for article_id, record in manifest_data["articles"].items():
                    if record.get("file_name") == file_name:
                        record["upload_status"] = "failed"
                        record["upload_error"] = str(exc)
                        break

    cache_name = None
    cache_error = None

    if args.create_cache:
        cache, cache_error = create_cache_if_requested(client, model, kb_name, results)
        if cache is not None:
            cache_name = getattr(cache, "name", None)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    summary = {
        "provider": "gemini",
        "kb_name": kb_name,
        "model": model,
        "chunking_strategy": {
            "type": "static-estimate",
            "max_chunk_size_tokens": CHUNK_SIZE_TOKENS,
            "chunk_overlap_tokens": CHUNK_OVERLAP_TOKENS,
        },
        "files_discovered": len(markdown_files),
        "files_uploaded": uploaded,
        "files_failed": failed,
        "estimated_chunks": estimated_chunks_total,
        "cache_name": cache_name,
        "cache_error": cache_error,
        "results": results,
    }

    SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if manifest_data and manifest_data.get("articles"):
        manifest_data["last_upload_summary"] = {
            "files_discovered": len(markdown_files),
            "files_uploaded": uploaded,
            "files_failed": failed,
            "estimated_chunks": estimated_chunks_total,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        save_manifest(manifest_data)

    print("\nUpload summary")
    print(f"- files_discovered: {summary['files_discovered']}")
    print(f"- files_uploaded:   {summary['files_uploaded']}")
    print(f"- files_failed:     {summary['files_failed']}")
    print(f"- estimated_chunks: {summary['estimated_chunks']}")
    print(f"- cache_name:       {summary['cache_name']}")
    print(f"- summary_path:     {SUMMARY_PATH}")


if __name__ == "__main__":
    main()
