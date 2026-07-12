"""Admin models — Character Scan."""

from django.contrib import admin

from .models import Recruit


@admin.register(Recruit)
class RecruitAdmin(admin.ModelAdmin):
    list_display = ("character_name", "corporation", "status", "applied_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("eve_character__character_name", "eve_character__corporation_name")
    list_select_related = ("eve_character",)
    raw_id_fields = ("eve_character",)
    readonly_fields = ("applied_at", "updated_at")
    ordering = ("-updated_at",)

    @admin.display(description="Character", ordering="eve_character__character_name")
    def character_name(self, obj):
        return obj.eve_character.character_name

    @admin.display(description="Corp")
    def corporation(self, obj):
        return obj.eve_character.corporation_name
