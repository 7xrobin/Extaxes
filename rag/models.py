from django.db import models


class TaxSource(models.Model):
    """A trusted URL the agent treats as a source of truth for German tax questions."""

    STATUS = [
        ("pending", "Pending"),
        ("indexed", "Indexed"),
        ("failed",  "Failed"),
    ]

    url = models.URLField(unique=True)
    title = models.CharField(max_length=300, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS, default="pending")
    error = models.TextField(blank=True, default="")
    chunk_count = models.IntegerField(default=0)
    fetched_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title or self.url


class TaxChunk(models.Model):
    """One embedded passage of a TaxSource. Embedding stored as a JSON list of floats."""

    source = models.ForeignKey(
        TaxSource, related_name="chunks", on_delete=models.CASCADE
    )
    ordinal = models.IntegerField(default=0)
    content = models.TextField()
    embedding = models.JSONField(default=list)

    class Meta:
        ordering = ["source_id", "ordinal"]

    def __str__(self):
        return f"{self.source_id}#{self.ordinal}"
