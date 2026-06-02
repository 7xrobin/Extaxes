from django.contrib import admin

from .ingest import index_source
from .models import TaxChunk, TaxSource


@admin.register(TaxSource)
class TaxSourceAdmin(admin.ModelAdmin):
    list_display = ("title", "url", "status", "chunk_count", "fetched_at")
    list_filter = ("status",)
    search_fields = ("title", "url")
    readonly_fields = ("status", "error", "chunk_count", "fetched_at", "created_at")
    actions = ("reindex_selected",)

    def save_model(self, request, obj, form, change):
        """Index on create or whenever the URL changes."""
        super().save_model(request, obj, form, change)
        if (not change) or ("url" in form.changed_data):
            index_source(obj)
        if obj.status == "failed":
            self.message_user(
                request,
                f"Indexing failed for {obj.url}: {obj.error}",
                level="error",
            )
        else:
            self.message_user(request, f"Indexed {obj.chunk_count} chunk(s) from {obj.url}.")

    @admin.action(description="Re-index selected sources")
    def reindex_selected(self, request, queryset):
        ok = failed = 0
        for source in queryset:
            index_source(source)
            if source.status == "indexed":
                ok += 1
            else:
                failed += 1
        self.message_user(request, f"Re-indexed {ok} source(s); {failed} failed.")


@admin.register(TaxChunk)
class TaxChunkAdmin(admin.ModelAdmin):
    list_display = ("source", "ordinal", "preview")
    list_select_related = ("source",)
    search_fields = ("content",)

    def has_add_permission(self, request):
        return False

    @admin.display(description="Content")
    def preview(self, obj):
        return (obj.content[:120] + "…") if len(obj.content) > 120 else obj.content
