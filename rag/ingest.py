"""
Ingestion pipeline for tax source URLs: fetch → clean → chunk → embed.

Embeddings use OpenAI text-embedding-3-small, stored as JSON lists on TaxChunk.
No vector DB — retrieval is an in-memory cosine scan (see retriever.py), which is
plenty fast for a curated set of trusted sources.
"""
from html.parser import HTMLParser

import requests
from django.conf import settings
from django.utils import timezone
from openai import OpenAI

from .models import TaxChunk, TaxSource

_client = OpenAI(api_key=settings.OPENAI_API_KEY)

EMBEDDING_MODEL = getattr(settings, "RAG_EMBEDDING_MODEL", "text-embedding-3-small")

_SKIP_TAGS = {"script", "style", "noscript", "head", "svg", "nav", "footer"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"}


class _HTMLTextExtractor(HTMLParser):
    """Collapse HTML into readable text, dropping script/style/etc."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._parts.append(text + " ")

    @property
    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [ln.strip() for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)

    @property
    def title(self) -> str:
        return " ".join("".join(self._title_parts).split())


def _fetch_text(url: str) -> tuple[str, str]:
    """Return (cleaned_text, page_title). Raises on HTTP/network failure."""
    resp = requests.get(
        url,
        timeout=20,
        headers={"User-Agent": "KyronTaxBot/1.0 (+educational German tax assistant)"},
    )
    resp.raise_for_status()
    parser = _HTMLTextExtractor()
    parser.feed(resp.text)
    return parser.text, parser.title


def _chunk(text: str, max_chars: int = 1000, overlap: int = 150) -> list[str]:
    """Split text into overlapping char windows on whitespace boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chars, n)
        # Prefer to break on whitespace so we don't split mid-word.
        if end < n:
            ws = text.rfind(" ", start + max_chars - overlap, end)
            if ws > start:
                end = ws
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch-embed a list of strings. Returns one vector per input."""
    if not texts:
        return []
    resp = _client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    return [item.embedding for item in resp.data]


def index_source(source: TaxSource) -> TaxSource:
    """
    Fetch, chunk, embed and (re)store all chunks for a source.
    On any failure the source is marked 'failed' with the error recorded — never raises.
    """
    try:
        text, page_title = _fetch_text(source.url)
        chunks = _chunk(text)
        if not chunks:
            raise ValueError("No extractable text found at URL.")

        vectors = embed_texts(chunks)

        source.chunks.all().delete()
        TaxChunk.objects.bulk_create([
            TaxChunk(source=source, ordinal=i, content=c, embedding=v)
            for i, (c, v) in enumerate(zip(chunks, vectors))
        ])

        if not source.title and page_title:
            source.title = page_title[:300]
        source.status = "indexed"
        source.error = ""
        source.chunk_count = len(chunks)
        source.fetched_at = timezone.now()
    except Exception as exc:  # noqa: BLE001 — surface any failure to the admin
        source.status = "failed"
        source.error = str(exc)[:2000]
        source.chunk_count = 0
    source.save()
    return source
