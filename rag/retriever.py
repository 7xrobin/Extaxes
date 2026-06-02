"""
Retrieval over indexed tax chunks. Cosine similarity computed in-memory with numpy.
"""
import numpy as np

from .ingest import embed_texts
from .models import TaxChunk


def embed_query(text: str) -> list[float]:
    """Embed a single query string."""
    vecs = embed_texts([text])
    return vecs[0] if vecs else []


def search(query: str, k: int = 4) -> list[dict]:
    """
    Return up to k most similar chunks as
    {content, url, title, score}, sorted by descending cosine score.
    Returns [] when there are no indexed chunks.
    """
    query = (query or "").strip()
    if not query:
        return []

    rows = list(
        TaxChunk.objects.filter(source__status="indexed")
        .select_related("source")
        .values_list("content", "embedding", "source__url", "source__title")
    )
    rows = [r for r in rows if r[1]]  # drop any chunk without an embedding
    if not rows:
        return []

    q = np.asarray(embed_query(query), dtype=np.float32)
    if q.size == 0:
        return []
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return []

    matrix = np.asarray([r[1] for r in rows], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1e-9
    scores = (matrix @ q) / (norms * q_norm)

    top = np.argsort(scores)[::-1][:k]
    return [
        {
            "content": rows[i][0],
            "url":     rows[i][2],
            "title":   rows[i][3] or rows[i][2],
            "score":   float(scores[i]),
        }
        for i in top
    ]


def build_context(query: str, k: int = 4, min_score: float = 0.30) -> str:
    """
    Build a prompt-ready, source-cited context block from the top matches.
    Returns "" when nothing is indexed or the best match is below min_score
    (keeps non-tax questions clean).
    """
    hits = search(query, k=k)
    hits = [h for h in hits if h["score"] >= min_score]
    if not hits:
        return ""

    blocks = []
    for h in hits:
        blocks.append(f"[Source: {h['title']} — {h['url']}]\n{h['content']}")
    return "\n\n".join(blocks)
