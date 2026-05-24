import json
import os
import re
from io import BytesIO
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None


load_dotenv()


SPACE_RE = re.compile(r"\s+")
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
SCHEMELESS_URL_RE = re.compile(
    r"(?<!@)\b(?:www\.)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s]*)?(?:\?[^\s]*)?(?:#[^\s]*)?",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
AUTHOR_SPLIT_RE = re.compile(r"\s*[;/]\s*")
INITIAL_TOKEN_RE = re.compile(r"^[A-Za-zА-Яа-яЁё]\.?$")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
AUTHOR_WORD_RE = r"[A-Za-zА-Яа-яЁё-]+"
INITIALS_BLOCK_RE = r"(?:[A-Za-zА-Яа-яЁё]\.\s*){1,4}"
AUTHOR_ENTRY_RE = re.compile(
    rf"(?:{AUTHOR_WORD_RE},\s*{INITIALS_BLOCK_RE}|{AUTHOR_WORD_RE}\s+{INITIALS_BLOCK_RE}|{INITIALS_BLOCK_RE}{AUTHOR_WORD_RE})(?=(?:\s*,\s*|$))"
)
META_TAG_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
FULL_NAME_RE = re.compile(r"\b[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+){1,2}\b")
TRANSLATOR_RE = re.compile(r"пер\.\s*с\s*([а-яё.]+)\s+([А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+){1,2})", re.IGNORECASE)
ISBN_TEXT_RE = re.compile(r"\b97[89][\d\- ]{10,16}\b")

OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://localhost:11434/api/chat")
DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

REFERENCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "source_type": {
            "type": "string",
            "enum": ["book", "article", "website"],
        },
        "citation_style": {
            "type": "string",
            "enum": ["gost_ru", "english"],
        },
        "authors": {"type": "string"},
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "translator": {"type": "string"},
        "translation_from": {"type": "string"},
        "city": {"type": "string"},
        "publisher": {"type": "string"},
        "year": {"type": "string"},
        "pages": {"type": "string"},
        "isbn": {"type": "string"},
        "journal": {"type": "string"},
        "volume": {"type": "string"},
        "issue": {"type": "string"},
        "issn": {"type": "string"},
        "doi": {"type": "string"},
        "site_name": {"type": "string"},
        "url": {"type": "string"},
        "access_date": {"type": "string"},
        "confidence": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": [
        "source_type",
        "citation_style",
        "authors",
        "title",
        "subtitle",
        "translator",
        "translation_from",
        "city",
        "publisher",
        "year",
        "pages",
        "isbn",
        "journal",
        "volume",
        "issue",
        "issn",
        "doi",
        "site_name",
        "url",
        "access_date",
        "confidence",
        "notes",
    ],
}


def normalize_space(value: str) -> str:
    return SPACE_RE.sub(" ", value or "").strip()


def normalize_raw_reference(value: str) -> str:
    normalized = value or ""
    normalized = normalized.replace("\u2014", "-").replace("\u2013", "-")
    return normalize_space(normalized)


def clean_url_candidate(value: str) -> str:
    return normalize_space(value).strip("()[]{}<>,;.'\"")


def extract_reference_url(value: str) -> str:
    normalized = normalize_raw_reference(value)
    match = URL_RE.search(normalized)
    if match:
        return clean_url_candidate(match.group(0))

    match = SCHEMELESS_URL_RE.search(normalized)
    if not match:
        return ""

    candidate = clean_url_candidate(match.group(0))
    if not candidate or candidate.lower().startswith(("doi:", "issn")):
        return ""
    return f"https://{candidate.lstrip('/')}"


def normalize_citation_style(value: str) -> str:
    cleaned = normalize_space(value)
    return cleaned if cleaned in {"gost_ru", "english"} else ""


def infer_citation_style(*values: str) -> str:
    merged = " ".join(normalize_space(value) for value in values if value)
    if CYRILLIC_RE.search(merged):
        return "gost_ru"
    return "english"


def split_person_name(full_name: str) -> tuple[str, list[str]]:
    cleaned = normalize_space((full_name or "").strip(" ,;"))
    if not cleaned:
        return "", []

    if "," in cleaned:
        surname_part, given_part = cleaned.split(",", 1)
        surname = normalize_space(surname_part)
        given_parts = [part for part in normalize_space(given_part).split() if part]
        return surname, given_parts

    parts = [part for part in cleaned.split() if part]
    if not parts:
        return "", []
    if len(parts) > 1 and all(INITIAL_TOKEN_RE.match(part) for part in parts[:-1]):
        surname = parts[-1]
        given_parts = parts[:-1]
    else:
        surname = parts[0]
        given_parts = parts[1:]
    return surname, given_parts


def format_initials(given_parts: list[str]) -> str:
    chunks = []
    for part in given_parts:
        cleaned = normalize_space(part).strip(".")
        if not cleaned:
            continue
        chunks.append(f"{cleaned[0]}.")
    return " ".join(chunks)


def heading_author(full_name: str) -> str:
    surname, given_parts = split_person_name(full_name)
    if not surname:
        return ""
    initials_part = format_initials(given_parts)
    if initials_part:
        return f"{surname}, {initials_part}"
    return surname


def responsibility_author(full_name: str) -> str:
    surname, given_parts = split_person_name(full_name)
    if not surname:
        return ""
    initials_part = format_initials(given_parts)
    if initials_part:
        return f"{initials_part} {surname}"
    return surname


def normalize_authors(raw: str) -> list[str]:
    cleaned = normalize_space(raw)
    if not cleaned:
        return []

    if ";" in cleaned or "/" in cleaned:
        return [part for part in (normalize_space(item) for item in AUTHOR_SPLIT_RE.split(cleaned)) if part]

    matches = [normalize_space(match.group(0)) for match in AUTHOR_ENTRY_RE.finditer(cleaned)]
    if matches:
        remainder = AUTHOR_ENTRY_RE.sub("", cleaned)
        remainder = re.sub(r"[\s,]+", "", remainder)
        if not remainder:
            return matches

    if re.search(r"\s+(?:and|и)\s+", cleaned, re.IGNORECASE):
        parts = [normalize_space(part) for part in re.split(r"\s+(?:and|и)\s+", cleaned, flags=re.IGNORECASE)]
        if len(parts) > 1:
            return [part for part in parts if part]

    return [cleaned]


def author_block(authors: list[str]) -> str:
    if not authors:
        return ""
    return heading_author(authors[0])


def responsibility_authors(authors: list[str]) -> str:
    formatted = [responsibility_author(author) for author in authors if responsibility_author(author)]
    return ", ".join(formatted)


def is_initials_block(value: str) -> bool:
    parts = [part for part in normalize_space(value).split() if part]
    return bool(parts) and all(INITIAL_TOKEN_RE.match(part) for part in parts)


def split_english_person_name(full_name: str) -> tuple[str, list[str]]:
    cleaned = normalize_space((full_name or "").strip(" ,;"))
    if not cleaned:
        return "", []

    if "," in cleaned:
        surname_part, given_part = cleaned.split(",", 1)
        surname = normalize_space(surname_part)
        given_parts = [part for part in normalize_space(given_part).split() if part]
        return surname, given_parts

    parts = [part for part in cleaned.split() if part]
    if not parts:
        return "", []
    if len(parts) == 1:
        return parts[0], []
    if INITIAL_TOKEN_RE.match(parts[-1]) and not all(INITIAL_TOKEN_RE.match(part) for part in parts[:-1]):
        return parts[0], parts[1:]
    if all(INITIAL_TOKEN_RE.match(part) for part in parts[:-1]):
        return parts[-1], parts[:-1]
    return parts[-1], parts[:-1]


def normalize_authors_english(raw: str) -> list[str]:
    cleaned = normalize_space(raw)
    if not cleaned:
        return []

    if ";" in cleaned or "/" in cleaned:
        return [part for part in (normalize_space(item) for item in AUTHOR_SPLIT_RE.split(cleaned)) if part]

    and_parts = [normalize_space(part) for part in re.split(r"\s+and\s+", cleaned, flags=re.IGNORECASE)]
    if len(and_parts) > 1:
        return [part for part in and_parts if part]

    comma_parts = [normalize_space(part) for part in cleaned.split(",") if normalize_space(part)]
    if len(comma_parts) > 1:
        if len(comma_parts) == 2 and is_initials_block(comma_parts[1]):
            return [cleaned]

        authors = []
        index = 0
        while index < len(comma_parts):
            current = comma_parts[index]
            next_part = comma_parts[index + 1] if index + 1 < len(comma_parts) else ""
            if next_part and is_initials_block(next_part):
                authors.append(f"{current}, {next_part}")
                index += 2
            else:
                authors.append(current)
                index += 1
        return authors

    return [cleaned]


def heading_author_english(full_name: str) -> str:
    surname, given_parts = split_english_person_name(full_name)
    if not surname:
        return ""
    initials_part = format_initials(given_parts)
    if initials_part:
        return f"{surname}, {initials_part}"
    return surname


def responsibility_author_english(full_name: str) -> str:
    surname, given_parts = split_english_person_name(full_name)
    if not surname:
        return ""
    initials_part = format_initials(given_parts)
    if initials_part:
        return f"{initials_part} {surname}"
    return surname


def author_block_english(authors: list[str]) -> str:
    if not authors:
        return ""
    return heading_author_english(authors[0])


def responsibility_authors_english(authors: list[str]) -> str:
    formatted = [
        responsibility_author_english(author)
        for author in authors
        if responsibility_author_english(author)
    ]
    return ", ".join(formatted)


def add_dot(text: str) -> str:
    text = normalize_space(text)
    if not text:
        return ""
    return text if text.endswith(".") else f"{text}."


def format_article_issue(issue: str) -> str:
    issue = normalize_space(issue)
    if not issue:
        return ""
    lowered = issue.lower()
    if lowered.startswith(("№", "no.", "no ", "т.", "вып.")):
        return issue
    if lowered.startswith("no."):
        return issue.replace("No.", "№", 1).replace("no.", "№", 1)
    if lowered.startswith("no "):
        return f"№ {issue[3:].strip()}"
    return f"№ {issue}"


def format_english_article_issue(issue: str) -> str:
    issue = normalize_space(issue)
    if not issue:
        return ""
    lowered = issue.lower()
    if lowered.startswith("issue "):
        return f"No. {issue[6:].strip()}"
    if lowered.startswith("issue."):
        return f"No. {issue[6:].strip()}"
    if lowered.startswith(("no. ", "no ", "no.")):
        return issue.replace("no ", "No. ", 1).replace("no.", "No.", 1)
    return f"No. {issue}"


PLACEHOLDER_RE = re.compile(r"\[(.*?)\]")


def sanitize_llm_field(value: str) -> str:
    cleaned = normalize_space(value)
    if not cleaned:
        return ""
    if PLACEHOLDER_RE.search(cleaned):
        return ""
    lowered = cleaned.lower()
    banned = {
        "не указано",
        "неизвестно",
        "unknown",
        "n/a",
        "нет данных",
    }
    if lowered in banned:
        return ""
    return cleaned


def sanitize_llm_result(data: dict[str, str]) -> dict[str, str]:
    sanitized = {}
    for key, value in data.items():
        if key in {"source_type", "citation_style", "confidence", "notes"}:
            sanitized[key] = normalize_space(value)
        else:
            sanitized[key] = sanitize_llm_field(value)
    if sanitized.get("pages", "").endswith(" с."):
        sanitized["pages"] = sanitized["pages"][:-3].strip()
    sanitized["citation_style"] = normalize_citation_style(
        sanitized.get("citation_style", "")
    ) or infer_citation_style(
        sanitized.get("authors", ""),
        sanitized.get("title", ""),
        sanitized.get("journal", ""),
        sanitized.get("site_name", ""),
    )
    return sanitized


def normalize_isbn(value: str) -> str:
    digits = re.sub(r"[^0-9Xx]", "", value or "")
    return digits if len(digits) in {10, 13} else ""


def derive_pdf_context(metadata: dict[str, str]) -> dict[str, str]:
    derived: dict[str, str] = {}
    title = sanitize_llm_field(metadata.get("pdf_title", "") or metadata.get("title", ""))
    author = sanitize_llm_field(metadata.get("pdf_author", ""))
    excerpt = normalize_space(metadata.get("pdf_text_excerpt", ""))

    if excerpt:
        title_position = excerpt.find(title) if title else -1
        search_zone = excerpt[title_position + len(title):] if title_position >= 0 else excerpt
        name_matches = FULL_NAME_RE.findall(search_zone[:400])
        if name_matches:
            author = author or name_matches[0]

        translator_match = TRANSLATOR_RE.search(excerpt[:4000])
        if translator_match:
            derived["translation_from"] = sanitize_llm_field(translator_match.group(1))
            derived["translator"] = sanitize_llm_field(translator_match.group(2))

        year_match = YEAR_RE.search(excerpt[:4000])
        if year_match:
            derived["year"] = year_match.group(0)

        isbn_match = ISBN_TEXT_RE.search(excerpt[:4000])
        if isbn_match:
            derived["isbn"] = normalize_isbn(isbn_match.group(0))

    if title:
        derived["title"] = title
    if author:
        derived["author"] = author
    if metadata.get("isbn_from_url"):
        derived["isbn"] = normalize_isbn(metadata.get("isbn_from_url", "")) or derived.get("isbn", "")
    return {key: value for key, value in derived.items() if value}


def derive_html_book_context(title_text: str) -> dict[str, str]:
    cleaned = normalize_space(title_text)
    if not cleaned or " / " not in cleaned:
        return {}

    derived: dict[str, str] = {}
    heading_part = cleaned.split(" / ", 1)[0]
    heading_match = re.match(
        r"^(?P<author>.+?,\s*(?:[A-Za-zА-ЯЁ]\.\s*){1,3})\s+(?P<title>.+)$",
        heading_part,
    )
    if heading_match:
        derived["author"] = sanitize_llm_field(heading_match.group("author"))
        derived["title"] = sanitize_llm_field(heading_match.group("title"))
    else:
        derived["title"] = sanitize_llm_field(heading_part)

    translator_match = re.search(
        r"пер\.\s*с\s*([A-Za-zА-ЯЁа-яё.-]+)\s+((?:[A-Za-zА-ЯЁ]\.\s*){1,3}[A-Za-zА-ЯЁа-яё-]+)",
        cleaned,
        re.IGNORECASE,
    )
    if translator_match:
        derived["translation_from"] = sanitize_llm_field(translator_match.group(1))
        derived["translator"] = sanitize_llm_field(translator_match.group(2))

    imprint_match = re.search(r"\s[-—]\s*([^:]+?)\s*:\s*([^,]+),\s*((?:19|20)\d{2})", cleaned)
    if imprint_match:
        derived["city"] = sanitize_llm_field(imprint_match.group(1))
        derived["publisher"] = sanitize_llm_field(imprint_match.group(2))
        derived["year"] = imprint_match.group(3)

    pages_match = re.search(r"\s[-—]\s*(\d+)\s*с\.?", cleaned, re.IGNORECASE)
    if pages_match:
        derived["pages"] = pages_match.group(1)

    isbn_match = ISBN_TEXT_RE.search(cleaned)
    if isbn_match:
        derived["isbn"] = normalize_isbn(isbn_match.group(0))

    return {key: value for key, value in derived.items() if value}


def format_book(data: dict[str, Any]) -> str:
    authors = normalize_authors(data.get("authors", ""))
    title = normalize_space(data.get("title", ""))
    subtitle = normalize_space(data.get("subtitle", ""))
    translator = normalize_space(data.get("translator", ""))
    translation_from = normalize_space(data.get("translation_from", ""))
    city = normalize_space(data.get("city", ""))
    publisher = normalize_space(data.get("publisher", ""))
    year = normalize_space(data.get("year", ""))
    pages = normalize_space(data.get("pages", ""))
    isbn = normalize_space(data.get("isbn", ""))
    url = normalize_space(data.get("url", ""))
    access_date = normalize_space(data.get("access_date", ""))

    parts = []
    heading = author_block(authors)
    title_block = ": ".join(part for part in [title, subtitle] if part)
    if heading:
        parts.append(f"{heading} {title_block}".strip())
    else:
        parts.append(title_block)

    responsibility = []
    if authors:
        responsibility.append(responsibility_authors(authors[:3]))
    if translator:
        translation_prefix = "пер."
        if translation_from:
            translation_prefix = f"пер. с {translation_from}"
        responsibility.append(f"{translation_prefix} {translator}")
    if responsibility:
        parts.append(f"/ {'; '.join(responsibility)}")

    imprint = ""
    if city and publisher and year:
        imprint = f"— {city} : {publisher}, {year}"
    elif city and publisher:
        imprint = f"— {city} : {publisher}"
    elif city and year:
        imprint = f"— {city}, {year}"
    elif publisher and year:
        imprint = f"— {publisher}, {year}"
    elif city:
        imprint = f"— {city}"
    elif publisher:
        imprint = f"— {publisher}"
    elif year:
        imprint = f"— {year}"
    if imprint:
        parts.append(imprint)
    if pages:
        parts.append(f"— {pages} с.")
    if isbn:
        parts.append(f"— ISBN {isbn}.")
    if url:
        parts.append(f"— URL: {url}")
    if access_date:
        parts.append(f"(дата обращения: {access_date})")

    result = " ".join(part for part in parts if part)
    if year and pages:
        result = result.replace(f"{year} —", f"{year}. —")
    return add_dot(result)


def format_article(data: dict[str, Any]) -> str:
    authors = normalize_authors(data.get("authors", ""))
    title = normalize_space(data.get("title", ""))
    journal = normalize_space(data.get("journal", ""))
    year = normalize_space(data.get("year", ""))
    volume = normalize_space(data.get("volume", ""))
    issue = normalize_space(data.get("issue", ""))
    pages = normalize_space(data.get("pages", ""))
    issn = normalize_space(data.get("issn", ""))
    doi = normalize_space(data.get("doi", ""))
    if volume == year:
        volume = ""

    parts = []
    heading = author_block(authors)
    if heading:
        parts.append(f"{heading} {title}".strip())
    else:
        parts.append(title)
    if authors:
        parts.append(f"/ {responsibility_authors(authors[:3])}")
    if journal:
        parts.append(f"// {add_dot(journal)}")
    if year:
        parts.append(f"— {year}.")
    if volume:
        parts.append(f"— {add_dot(volume)}")
    if issue:
        parts.append(f"— {add_dot(format_article_issue(issue))}")
    if pages:
        parts.append(f"— С. {pages}")
    if issn:
        parts.append(f"— ISSN {issn}.")
    if doi:
        parts.append(f"— DOI: {doi}")
    return add_dot(" ".join(part for part in parts if part))


def format_website(data: dict[str, Any]) -> str:
    title = normalize_space(data.get("title", ""))
    site_name = normalize_space(data.get("site_name", ""))
    year = normalize_space(data.get("year", ""))
    url = normalize_space(data.get("url", ""))
    access_date = normalize_space(data.get("access_date", ""))

    parts = [title]
    if site_name:
        parts.append(f"// {add_dot(site_name)}")
    if year:
        parts.append(f"— {year}.")
    if url:
        parts.append(f"— URL: {url}")
    if access_date:
        parts.append(f"(дата обращения: {access_date})")
    return add_dot(" ".join(part for part in parts if part))


def format_book_english(data: dict[str, Any]) -> str:
    authors = normalize_authors_english(data.get("authors", ""))
    title = normalize_space(data.get("title", ""))
    subtitle = normalize_space(data.get("subtitle", ""))
    city = normalize_space(data.get("city", ""))
    publisher = normalize_space(data.get("publisher", ""))
    year = normalize_space(data.get("year", ""))
    pages = normalize_space(data.get("pages", ""))
    isbn = normalize_space(data.get("isbn", ""))
    url = normalize_space(data.get("url", ""))
    access_date = normalize_space(data.get("access_date", ""))

    parts = []
    heading = author_block_english(authors)
    title_block = ": ".join(part for part in [title, subtitle] if part)
    if heading:
        parts.append(f"{heading} {title_block}".strip())
    else:
        parts.append(title_block)
    if authors:
        parts.append(f"/ {responsibility_authors_english(authors[:3])}")

    imprint = ""
    if city and publisher and year:
        imprint = f"— {city} : {publisher}, {year}"
    elif city and publisher:
        imprint = f"— {city} : {publisher}"
    elif city and year:
        imprint = f"— {city}, {year}"
    elif publisher and year:
        imprint = f"— {publisher}, {year}"
    elif city:
        imprint = f"— {city}"
    elif publisher:
        imprint = f"— {publisher}"
    elif year:
        imprint = f"— {year}"
    if imprint:
        parts.append(imprint)
    if pages:
        parts.append(f"— {pages} p.")
    if isbn:
        parts.append(f"— ISBN {isbn}.")
    if url:
        parts.append(f"— URL: {url}")
    if access_date:
        parts.append(f"(visited on {access_date})")
    result = " ".join(part for part in parts if part)
    if year and pages:
        result = result.replace(f"{year} —", f"{year}. —")
    return add_dot(result)


def format_article_english(data: dict[str, Any]) -> str:
    authors = normalize_authors_english(data.get("authors", ""))
    title = normalize_space(data.get("title", ""))
    journal = normalize_space(data.get("journal", ""))
    year = normalize_space(data.get("year", ""))
    volume = normalize_space(data.get("volume", ""))
    issue = normalize_space(data.get("issue", ""))
    pages = normalize_space(data.get("pages", ""))
    issn = normalize_space(data.get("issn", ""))
    doi = normalize_space(data.get("doi", ""))
    if volume == year:
        volume = ""

    parts = []
    heading = author_block_english(authors)
    if heading:
        parts.append(f"{heading} {title}".strip())
    else:
        parts.append(title)
    if authors:
        parts.append(f"/ {responsibility_authors_english(authors[:3])}")
    if journal:
        parts.append(f"// {add_dot(journal)}")
    if year:
        parts.append(f"— {year}.")
    if volume:
        parts.append(f"— {add_dot(volume)}")
    if issue:
        parts.append(f"— {add_dot(format_english_article_issue(issue))}")
    if pages:
        parts.append(f"— P. {pages}.")
    if issn:
        parts.append(f"— ISSN {issn}.")
    if doi:
        parts.append(f"— DOI: {doi}")
    return add_dot(" ".join(part for part in parts if part))


def format_website_english(data: dict[str, Any]) -> str:
    title = normalize_space(data.get("title", ""))
    site_name = normalize_space(data.get("site_name", ""))
    year = normalize_space(data.get("year", ""))
    url = normalize_space(data.get("url", ""))
    access_date = normalize_space(data.get("access_date", ""))

    parts = [add_dot(title)]
    if site_name:
        parts.append(f"// {add_dot(site_name)}")
    if year:
        parts.append(f"— {year}.")
    if url:
        parts.append(f"— URL: {url}")
    if access_date:
        parts.append(f"(visited on {access_date})")
    return add_dot(" ".join(part for part in parts if part))


FORMATTERS = {
    "gost_ru": {
        "book": format_book,
        "article": format_article,
        "website": format_website,
    },
    "english": {
        "book": format_book_english,
        "article": format_article_english,
        "website": format_website_english,
    },
}


def get_formatter(source_type: str, citation_style: str):
    style = normalize_citation_style(citation_style) or "gost_ru"
    return FORMATTERS.get(style, {}).get(source_type)


def infer_type(raw: str) -> str:
    raw = normalize_raw_reference(raw)
    lowered = raw.lower()
    if "http://" in lowered or "https://" in lowered or "url:" in lowered or lowered.endswith(".pdf"):
        return "website"
    if "//" in raw:
        return "article"
    return "book"


def split_candidate_authors(raw: str) -> tuple[str, str]:
    raw = normalize_raw_reference(raw)
    comma_initials_match = re.match(
        r"^\s*([A-ZА-ЯЁ][A-Za-zА-Яа-яЁё-]+,\s*(?:[A-ZА-ЯЁ]\.\s*){1,4})\s+(.+)$",
        raw,
    )
    if comma_initials_match:
        return normalize_space(comma_initials_match.group(1)), normalize_space(comma_initials_match.group(2))
    match = re.match(
        r"^\s*([A-ZА-ЯЁ][a-zа-яё-]+(?:\s+[A-ZА-ЯЁ]\.\s*[A-ZА-ЯЁ]\.)?(?:\s*,\s*[A-ZА-ЯЁ][a-zа-яё-]+(?:\s+[A-ZА-ЯЁ]\.\s*[A-ZА-ЯЁ]\.)?)*)\s+(.+)$",
        raw,
    )
    if match:
        return normalize_space(match.group(1)), normalize_space(match.group(2))
    return "", normalize_space(raw)


def parse_book(raw: str) -> dict[str, str]:
    raw = normalize_raw_reference(raw)
    authors, rest = split_candidate_authors(raw)
    year_match = YEAR_RE.search(rest)
    year = year_match.group(0) if year_match else ""
    pages_match = re.search(r"(\d+)\s*с\.?", rest, re.IGNORECASE)
    pages = pages_match.group(1) if pages_match else ""
    city_match = re.search(r"-\s*([А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z.\- ]+)\s*:", rest)
    city = normalize_space(city_match.group(1)) if city_match else ""
    publisher_match = re.search(r":\s*([^,]+),\s*((?:19|20)\d{2})", rest)
    publisher = normalize_space(publisher_match.group(1)) if publisher_match else ""
    isbn_match = ISBN_TEXT_RE.search(rest)
    isbn = normalize_isbn(isbn_match.group(0)) if isbn_match else ""

    title = rest
    title = re.sub(r"\s*-\s*[А-ЯЁA-Z].*?:\s*[^,]+,\s*(?:19|20)\d{2}", "", title)
    title = re.sub(r"\s*-\s*\d+\s*с\.?", "", title, flags=re.IGNORECASE)
    title = title.strip(" .")
    return {
        "source_type": "book",
        "citation_style": infer_citation_style(raw),
        "authors": authors,
        "title": normalize_space(title),
        "subtitle": "",
        "translator": "",
        "translation_from": "",
        "city": city,
        "publisher": publisher,
        "year": year,
        "pages": pages,
        "isbn": isbn,
        "journal": "",
        "volume": "",
        "issue": "",
        "issn": "",
        "doi": "",
        "site_name": "",
        "url": "",
        "access_date": "",
        "confidence": "low",
        "notes": "Запись разобрана эвристически.",
    }


def parse_article(raw: str) -> dict[str, str]:
    raw = normalize_raw_reference(raw)
    authors, rest = split_candidate_authors(raw)
    title_part, _, journal_part = rest.partition("//")
    responsibility_match = re.search(r"/\s*([^/]+)$", title_part)
    if responsibility_match:
        responsibility_authors_list = normalize_authors(responsibility_match.group(1))
        if responsibility_authors_list:
            authors = "; ".join(responsibility_authors_list)
    title_part = re.sub(r"/\s*[A-ZА-ЯЁ][^/]+$", "", title_part).strip(" .")
    journal_part = normalize_space(journal_part)
    year_match = YEAR_RE.search(journal_part)
    year = year_match.group(0) if year_match else ""
    volume_match = re.search(r"Vol\.?\s*([0-9]+)", journal_part, re.IGNORECASE)
    volume = volume_match.group(1) if volume_match else ""
    issue_match = re.search("(?:\\u2116|issue|no\\.?)\\s*([0-9]+)", journal_part, re.IGNORECASE)
    issue = issue_match.group(1) if issue_match else ""
    pages_match = re.search("(?:\\u0421\\.|P\\.)\\s*([\\d–-]+)", journal_part, re.IGNORECASE)
    pages = pages_match.group(1) if pages_match else ""
    issn_match = re.search(r"ISSN\s*([\d-]+)", journal_part, re.IGNORECASE)
    issn = issn_match.group(1) if issn_match else ""
    doi_match = re.search(r"DOI:\s*([^\s]+)", journal_part, re.IGNORECASE)
    doi = doi_match.group(1).rstrip(".,;") if doi_match else ""
    journal = re.split(
        "\\s+-\\s+(?=(?:19|20)\\d{2}|Vol\\.|\\u2116|issue|no\\.?|\\u0421\\.|P\\.|ISSN|DOI)",
        journal_part,
        maxsplit=1,
    )[0].strip(" .")
    return {
        "source_type": "article",
        "citation_style": infer_citation_style(raw),
        "authors": authors,
        "title": normalize_space(title_part.strip(" .")),
        "subtitle": "",
        "translator": "",
        "translation_from": "",
        "city": "",
        "publisher": "",
        "year": year,
        "pages": pages,
        "isbn": "",
        "journal": normalize_space(journal),
        "volume": volume,
        "issue": issue,
        "issn": issn,
        "doi": doi,
        "site_name": "",
        "url": "",
        "access_date": "",
        "confidence": "low",
        "notes": "Запись разобрана эвристически.",
    }


def parse_website(raw: str) -> dict[str, str]:
    raw = normalize_raw_reference(raw)
    url = extract_reference_url(raw)
    access_match = re.search(
        r"дата обращения[:\s]*([0-3]?\d\.[01]?\d\.\d{4})",
        raw,
        re.IGNORECASE,
    )
    access_date = access_match.group(1) if access_match else ""
    if not access_date:
        visited_match = re.search(r"visited on\s*([0-1]?\d/[0-3]?\d/\d{4})", raw, re.IGNORECASE)
        access_date = visited_match.group(1) if visited_match else ""
    year_match = YEAR_RE.search(raw)
    year = year_match.group(0) if year_match else ""

    title = raw
    if url:
        title = title.replace(url, "")
    title = re.sub(r"\(.*?дата обращения.*?\)", "", title, flags=re.IGNORECASE)
    title = re.sub(r"-\s*URL:.*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"-\s*\d{4}", "", title)
    title = title.strip(" .-")

    site_name = ""
    if "//" in raw:
        title, _, site_name = raw.partition("//")
        title = title.strip(" .-")
        site_name = re.sub(r"[-].*$", "", site_name).strip(" .")

    if url and (not title or title.lower() == "https:" or title.lower() == "http:"):
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")
        title = f"Материал сайта {domain}" if parsed.path.strip("/") else "Материал сайта"
        site_name = domain

    return {
        "source_type": "website",
        "citation_style": infer_citation_style(raw),
        "authors": "",
        "title": normalize_space(title),
        "subtitle": "",
        "translator": "",
        "translation_from": "",
        "city": "",
        "publisher": "",
        "year": year,
        "pages": "",
        "isbn": "",
        "journal": "",
        "volume": "",
        "issue": "",
        "issn": "",
        "doi": "",
        "site_name": normalize_space(site_name),
        "url": url,
        "access_date": access_date,
        "confidence": "low",
        "notes": "Запись разобрана эвристически.",
    }


def infer_source_type_from_metadata(metadata: dict[str, str]) -> str:
    if not metadata:
        return ""

    resource_type = metadata.get("resource_type", "")
    if resource_type == "pdf":
        return "book"

    parsed_url = urlparse(metadata.get("url", ""))
    domain = parsed_url.netloc.lower().replace("www.", "")
    title = metadata.get("title", "")
    journal = metadata.get("journal", "")

    if journal:
        return "article"
    if domain.endswith("cyberleninka.ru"):
        return "article"
    if domain.endswith("znanium.ru"):
        return "book"
    if resource_type == "html" and title and metadata.get("author"):
        return "article"
    return ""


def apply_metadata_overlay(parsed: dict[str, str], fetched_meta: dict[str, str]) -> dict[str, str]:
    if not fetched_meta:
        return parsed

    inferred_type = infer_source_type_from_metadata(fetched_meta)
    if inferred_type:
        parsed["source_type"] = inferred_type

    parsed["title"] = fetched_meta.get("title", "") or parsed.get("title", "")
    parsed["authors"] = fetched_meta.get("author", "") or parsed.get("authors", "")
    parsed["year"] = fetched_meta.get("year", "") or parsed.get("year", "")
    parsed["url"] = fetched_meta.get("url", "") or parsed.get("url", "")
    parsed["journal"] = fetched_meta.get("journal", "") or parsed.get("journal", "")
    parsed["site_name"] = fetched_meta.get("site_name", "") or parsed.get("site_name", "")
    parsed["isbn"] = fetched_meta.get("isbn", "") or fetched_meta.get("isbn_from_url", "") or parsed.get("isbn", "")
    parsed["translator"] = fetched_meta.get("translator", "") or parsed.get("translator", "")
    parsed["translation_from"] = fetched_meta.get("translation_from", "") or parsed.get("translation_from", "")
    parsed["publisher"] = fetched_meta.get("publisher", "") or parsed.get("publisher", "")
    parsed["city"] = fetched_meta.get("city", "") or parsed.get("city", "")

    if parsed.get("source_type") == "article":
        parsed["site_name"] = ""
    elif parsed.get("source_type") == "book":
        parsed["journal"] = ""
        parsed["site_name"] = ""

    return parsed


def heuristic_parse_reference(raw: str) -> dict[str, str]:
    source_type = infer_type(raw)
    if source_type == "website":
        return parse_website(raw)
    if source_type == "article":
        return parse_article(raw)
    return parse_book(raw)


def fetch_page_metadata(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {}

    request_obj = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0 Safari/537.36"
            )
        },
    )

    try:
        with urlopen(request_obj, timeout=12) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read(1_500_000)
    except (HTTPError, URLError, TimeoutError):
        return {}

    metadata: dict[str, str] = {}
    if "pdf" in content_type.lower() or url.lower().endswith(".pdf"):
        pdf_name = Path(urlparse(url).path).name
        metadata["resource_type"] = "pdf"
        metadata["filename"] = pdf_name
        metadata["title"] = pdf_name
        metadata["url"] = url
        isbn_match = re.search(r"(97[89]\d{10})", url)
        if isbn_match:
            metadata["isbn_from_url"] = isbn_match.group(1)
        if PdfReader is not None:
            try:
                reader = PdfReader(BytesIO(data))
                pdf_meta = reader.metadata or {}
                if pdf_meta.get("/Title"):
                    metadata["pdf_title"] = normalize_space(str(pdf_meta.get("/Title")))
                if pdf_meta.get("/Author"):
                    metadata["pdf_author"] = normalize_space(str(pdf_meta.get("/Author")))
                extracted_pages = []
                for page in reader.pages[:3]:
                    text = normalize_space(page.extract_text() or "")
                    if text:
                        extracted_pages.append(text)
                if extracted_pages:
                    metadata["pdf_text_excerpt"] = " ".join(extracted_pages)[:12000]
            except Exception:
                metadata["pdf_parse_note"] = "Не удалось извлечь текст или метаданные из PDF."
        metadata.update(derive_pdf_context(metadata))
        return metadata

    html = data.decode("utf-8", errors="ignore")

    title_match = TITLE_RE.search(html)
    if title_match:
        metadata["html_title"] = normalize_space(unescape(title_match.group(1)))

    for name, content in META_TAG_RE.findall(html):
        key = name.strip().lower()
        if key not in metadata and content.strip():
            metadata[key] = normalize_space(unescape(content))

    normalized = {
        "resource_type": "html",
        "title": metadata.get("citation_title")
        or metadata.get("og:title")
        or metadata.get("twitter:title")
        or metadata.get("html_title", ""),
        "site_name": metadata.get("og:site_name")
        or metadata.get("application-name", ""),
        "author": metadata.get("citation_author")
        or metadata.get("author")
        or metadata.get("article:author", ""),
        "journal": metadata.get("citation_journal_title", ""),
        "year": "",
        "url": url,
        "citation_style": infer_citation_style(
            metadata.get("citation_title", ""),
            metadata.get("og:title", ""),
            metadata.get("application-name", ""),
            metadata.get("author", ""),
        ),
    }

    if normalized["title"]:
        normalized.update(derive_html_book_context(normalized["title"]))

    date_value = (
        metadata.get("citation_publication_date")
        or metadata.get("article:published_time")
        or metadata.get("date")
        or metadata.get("dc.date")
        or ""
    )
    year_match = YEAR_RE.search(date_value)
    if year_match:
        normalized["year"] = year_match.group(0)

    return {key: value for key, value in normalized.items() if value}


def build_ollama_messages(raw: str, fetched_meta: dict[str, str]) -> list[dict[str, str]]:
    meta_text = json.dumps(fetched_meta, ensure_ascii=False, indent=2) if fetched_meta else "{}"
    schema_text = json.dumps(REFERENCE_SCHEMA, ensure_ascii=False, indent=2)
    system_text = (
        "Ты — аккуратный библиографический помощник для русскоязычной ВКР. "
        "Твоя задача: разобрать сырой источник и оформить его по ГОСТ Р 7.0.100-2018 и ГОСТ Р 7.0.5-2008 в разумном учебном приближении. "
        "Верни только JSON строго по схеме. "
        "Никогда не добавляй markdown, пояснения или текст вне JSON. "
        "Если информации недостаточно, оставляй пустую строку. "
        "Используй такие правила: "
        "1) Если передан прямой URL на PDF книги или предпросмотр книги, постарайся определить, что это book, а не website. "
        "Если есть pdf_text_excerpt, pdf_title, pdf_author, author, title или isbn_from_url, активно используй их. "
        "2) Если это URL статьи или журнальной публикации, выбирай article. "
        "3) Если это обычная веб-страница или раздел сайта, выбирай website. "
        "4) Поле confidence заполняй одним из значений: high, medium, low. "
        "5) Если ты не уверен, но видишь PDF книги, всё равно лучше собрать book и объяснить неопределённость в notes. "
        "6) Не выдумывай автора, переводчика, издательство, год или страницы без оснований. "
        "7) Никогда не вставляй заглушки вида [год], [Издательство], [Б.м.], [дата] и подобные. "
        "8) В поле citation_style ставь gost_ru для русскоязычных источников и english для англоязычных. "
        "9) Не переводи имена, названия работ, журналов и сайтов: сохраняй язык оригинального источника."
    )
    user_text = (
        f"Сырой ввод:\n{raw}\n\n"
        f"Метаданные и контекст, если удалось получить:\n{meta_text}\n\n"
        f"JSON schema:\n{schema_text}"
    )
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]


def call_ollama_parser(raw: str, fetched_meta: dict[str, str]) -> dict[str, str]:
    payload = {
        "model": os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
        "messages": build_ollama_messages(raw, fetched_meta),
        "stream": False,
        "format": REFERENCE_SCHEMA,
        "options": {
            "temperature": 0,
        },
    }

    request_obj = Request(
        OLLAMA_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request_obj, timeout=120) as response:
            body = response.read().decode("utf-8")
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Ollama API error: {detail}") from error
    except URLError as error:
        raise RuntimeError("Could not reach Ollama API") from error

    response_payload = json.loads(body)
    content = response_payload.get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("Ollama response did not contain message content")

    parsed = json.loads(content)
    for key in REFERENCE_SCHEMA["required"]:
        parsed.setdefault(key, "")
    return sanitize_llm_result(parsed)


def smart_parse_reference(raw: str) -> tuple[dict[str, str], str, str]:
    normalized = normalize_raw_reference(raw)
    inferred_style = infer_citation_style(normalized)
    fetched_meta = {}
    detected_url = extract_reference_url(normalized)
    if detected_url:
        fetched_meta = fetch_page_metadata(detected_url)

    try:
        parsed = call_ollama_parser(normalized, fetched_meta)
        parsed = apply_metadata_overlay(parsed, fetched_meta)
        parsed["citation_style"] = normalize_citation_style(
            fetched_meta.get("citation_style", "")
        ) or inferred_style or normalize_citation_style(parsed.get("citation_style", ""))
        return parsed, "ollama", ""
    except RuntimeError as error:
        ollama_error = str(error)

    parsed = heuristic_parse_reference(normalized)
    if fetched_meta:
        parsed = apply_metadata_overlay(parsed, fetched_meta)
        parsed["citation_style"] = normalize_citation_style(
            fetched_meta.get("citation_style", "")
        ) or inferred_style or normalize_citation_style(parsed.get("citation_style", ""))
        parsed["notes"] = "Использованы метаданные страницы и эвристический разбор."
        return parsed, "metadata+heuristics", ollama_error
    parsed["citation_style"] = inferred_style or normalize_citation_style(parsed.get("citation_style", ""))
    return parsed, "heuristics", ollama_error
