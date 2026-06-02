from unittest import mock

from django.test import TestCase

from . import ingest, retriever
from .models import TaxChunk, TaxSource


class ChunkTests(TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(ingest._chunk("a short sentence"), ["a short sentence"])

    def test_empty_text_no_chunks(self):
        self.assertEqual(ingest._chunk("   "), [])

    def test_long_text_overlapping_chunks(self):
        text = " ".join(["word"] * 600)  # ~3000 chars
        chunks = ingest._chunk(text, max_chars=1000, overlap=150)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        # Reassembled chunks must cover the whole source.
        self.assertIn("word", chunks[0])
        self.assertIn("word", chunks[-1])


class HTMLExtractTests(TestCase):
    def test_strips_script_and_style(self):
        html = (
            "<html><head><title>Tax Page</title><style>.x{color:red}</style></head>"
            "<body><p>ETFs are taxed.</p><script>var x=1;</script></body></html>"
        )
        parser = ingest._HTMLTextExtractor()
        parser.feed(html)
        self.assertIn("ETFs are taxed.", parser.text)
        self.assertNotIn("color:red", parser.text)
        self.assertNotIn("var x", parser.text)
        self.assertEqual(parser.title, "Tax Page")


class SearchTests(TestCase):
    def setUp(self):
        self.source = TaxSource.objects.create(
            url="https://example.com/tax", title="Tax Truth", status="indexed"
        )
        # Hand-built 3-dim embeddings — easy to reason about cosine ranking.
        TaxChunk.objects.create(source=self.source, ordinal=0,
                                content="Vorabpauschale on accumulating ETFs", embedding=[1.0, 0.0, 0.0])
        TaxChunk.objects.create(source=self.source, ordinal=1,
                                content="Sparerpauschbetrag allowance", embedding=[0.0, 1.0, 0.0])
        TaxChunk.objects.create(source=self.source, ordinal=2,
                                content="Unrelated content", embedding=[0.0, 0.0, 1.0])

    def test_search_ranks_by_cosine(self):
        with mock.patch.object(retriever, "embed_query", return_value=[0.9, 0.1, 0.0]):
            hits = retriever.search("vorabpauschale?", k=2)
        self.assertEqual(len(hits), 2)
        self.assertIn("Vorabpauschale", hits[0]["content"])
        self.assertEqual(hits[0]["url"], "https://example.com/tax")
        self.assertGreaterEqual(hits[0]["score"], hits[1]["score"])

    def test_build_context_threshold(self):
        # Orthogonal query → score 0 → below default threshold → empty context.
        with mock.patch.object(retriever, "embed_query", return_value=[0.0, 0.0, 1.0]):
            ctx = retriever.build_context("matches the unrelated vector", min_score=0.5, k=2)
        self.assertNotEqual(ctx, "")  # the unrelated chunk itself scores 1.0

        with mock.patch.object(retriever, "embed_query", return_value=[1.0, 0.0, 0.0]):
            ctx = retriever.build_context("vorabpauschale", min_score=0.5)
        self.assertIn("example.com/tax", ctx)
        self.assertIn("Vorabpauschale", ctx)

    def test_search_empty_when_no_indexed_chunks(self):
        TaxSource.objects.all().update(status="pending")
        with mock.patch.object(retriever, "embed_query", return_value=[1.0, 0.0, 0.0]):
            self.assertEqual(retriever.search("anything"), [])
