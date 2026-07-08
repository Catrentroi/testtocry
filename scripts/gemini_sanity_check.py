import json
import os
import re
import argparse
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

SYSTEM_PROMPT = """You are OptiBot, the customer-support bot for OptiSigns.com.

• Tone: helpful, factual, concise.

• Only answer using the uploaded docs.

• Max 5 bullet points; else link to the doc.

• Cite up to 3 \"Article URL:\" lines per reply."""

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"
SUMMARY_PATH = Path(__file__).resolve().parents[1] / "logs" / "gemini_upload_summary.json"


def tokenize(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def rank_docs_by_question(question, max_docs=4):
    query_tokens = set(tokenize(question))
    scored = []

    for path in DOCS_DIR.glob("*.md"):
        content = path.read_text(encoding="utf-8", errors="ignore")
        head = content[:6000].lower()
        score = sum(1 for token in query_tokens if token in head)
        if score > 0:
            scored.append((score, path.name))

    scored.sort(reverse=True)
    return [name for _, name in scored[:max_docs]]


def build_file_parts(summary_data, candidate_names):
    uploaded_by_name = {
        item.get("file_name"): item
        for item in summary_data.get("results", [])
        if item.get("status") == "uploaded"
    }

    parts = []
    used = []

    for name in candidate_names:
        item = uploaded_by_name.get(name)
        if not item:
            continue
        uri = item.get("uri")
        mime_type = item.get("mime_type") or "text/markdown"
        if not uri:
            continue
        parts.append(types.Part.from_uri(file_uri=uri, mime_type=mime_type))
        used.append(name)

    if not parts:
        # Fallback: use the first uploaded files when keyword ranking has no overlap.
        for name, item in uploaded_by_name.items():
            uri = item.get("uri")
            mime_type = item.get("mime_type") or "text/markdown"
            if not uri:
                continue
            parts.append(types.Part.from_uri(file_uri=uri, mime_type=mime_type))
            used.append(name)
            if len(parts) >= 4:
                break

    return parts, used


def print_cited_urls(answer):
    urls = re.findall(r"Article URL:\s*(\S+)", answer)
    seen = set()
    count = 0

    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        print(f"Article URL: {url}")
        count += 1
        if count >= 3:
            break


def main():
    parser = argparse.ArgumentParser(description="Ask Gemini a question using uploaded docs.")
    parser.add_argument(
        "--question",
        type=str,
        default="",
        help="Question to ask Gemini. If omitted, you will be prompted interactively.",
    )
    args = parser.parse_args()

    load_dotenv()

    api_key = os.getenv("API_KEY", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-pro").strip()
    cache_name = os.getenv("GEMINI_CACHE_NAME", "").strip()

    question = args.question.strip()
    if not question:
        question = input("Enter your question: ").strip()

    if not api_key:
        raise SystemExit("GEMINI_API_KEY is missing. Set it in .env")

    if not SUMMARY_PATH.exists():
        raise SystemExit("Missing logs/gemini_upload_summary.json. Run upload_gemini_kb.py first.")

    summary_data = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    candidate_names = rank_docs_by_question(question, max_docs=4)

    client = genai.Client(api_key=api_key)

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0.2,
    )

    contents = [question]

    effective_cache_name = cache_name or (summary_data.get("cache_name") or "")
    if effective_cache_name:
        config.cached_content = effective_cache_name
    else:
        file_parts, used_names = build_file_parts(summary_data, candidate_names)
        if not file_parts:
            raise SystemExit("No uploaded file URIs available for sanity check.")
        contents = file_parts + [question]
        print("Using uploaded files:")
        for name in used_names:
            print(f"- {name}")

    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )

    answer = response.text or ""

    print("\nQuestion:")
    print(question)
    print("\nAnswer:\n")
    print(answer.strip())
    print("\nCitations (up to 3):")
    print_cited_urls(answer)


if __name__ == "__main__":
    main()
