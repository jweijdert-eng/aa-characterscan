"""Admin models — Character Scan."""

from django.contrib import admin, messages

from .models import Recruit, RecruitLogEntry, Settings


@admin.register(Settings)
class SettingsAdmin(admin.ModelAdmin):
    """Eén bewerkbare rij met plugin-instellingen (webhook + toggles)."""

    fieldsets = (
        ("Discord-notificaties", {
            "fields": ("discord_webhook", "notify_new_application", "notify_alerts"),
        }),
        ("Lijst-weergave", {
            "fields": ("show_actions_on_done", "wait_alert_hours"),
        }),
        ("Onboarding-overzicht", {
            "fields": ("onboarding_new_days", "onboarding_state", "onboarding_groups"),
        }),
        ("Vetting-drempels", {
            "fields": ("wallet_alert_isk", "injector_alert",
                       "trusted_link_domains", "extra_enemy_ids"),
        }),
    )
    actions = ("send_test_notification",)

    @admin.action(description="Stuur test-notificatie naar Discord")
    def send_test_notification(self, request, queryset):
        from .discord import notify_test, webhook_url
        if not webhook_url():
            self.message_user(request, "Geen webhook-URL ingesteld — vul die eerst in en sla op.",
                              level=messages.WARNING)
            return
        if notify_test():
            self.message_user(request, "Test-notificatie verstuurd — check je Discord-kanaal.",
                              level=messages.SUCCESS)
        else:
            self.message_user(request, "Versturen mislukt — controleer de webhook-URL.",
                              level=messages.ERROR)

    def _can(self, request):
        return request.user.is_superuser or request.user.has_perm("characterscan.manage_settings")

    def has_view_permission(self, request, obj=None):
        return self._can(request)

    def has_change_permission(self, request, obj=None):
        return self._can(request)

    def has_add_permission(self, request):
        return self._can(request) and not Settings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class RecruitLogInline(admin.TabularInline):
    model = RecruitLogEntry
    extra = 0
    can_delete = False
    ordering = ("-created_at",)
    readonly_fields = ("action", "actor", "comment", "created_at")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Recruit)
class RecruitAdmin(admin.ModelAdmin):
    list_display = ("character_name", "corporation", "status", "applied_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("eve_character__character_name", "eve_character__corporation_name")
    list_select_related = ("eve_character",)
    raw_id_fields = ("eve_character",)
    readonly_fields = ("applied_at", "updated_at")
    ordering = ("-updated_at",)
    inlines = [RecruitLogInline]

    @admin.display(description="Character", ordering="eve_character__character_name")
    def character_name(self, obj):
        return obj.eve_character.character_name

    @admin.display(description="Corp")
    def corporation(self, obj):
        return obj.eve_character.corporation_name
