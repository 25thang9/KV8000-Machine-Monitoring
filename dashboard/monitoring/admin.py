from django.contrib import admin

from .models import Machine, MachineReading


admin.site.site_header = "KV8000 Machine Monitoring"
admin.site.site_title = "KV8000 Monitoring"
admin.site.index_title = "System Administration"


@admin.register(Machine)
class MachineAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "is_active",
        "updated_at",
    )

    list_filter = (
        "is_active",
    )

    search_fields = (
        "code",
        "name",
    )

    ordering = (
        "code",
    )


@admin.register(MachineReading)
class MachineReadingAdmin(admin.ModelAdmin):
    list_display = (
        "machine",
        "recorded_at",
        "machine_status",
        "production_count",
        "cycle_time_ms",
        "alarm_code",
        "recipe_no",
        "source",
    )

    list_filter = (
        "machine",
        "plc_online",
        "run_bit",
        "stop_bit",
        "alarm_bit",
        "auto_mode_bit",
        "source",
    )

    search_fields = (
        "machine__code",
        "machine__name",
    )

    date_hierarchy = "recorded_at"

    ordering = (
        "-recorded_at",
    )

    readonly_fields = (
        "created_at",
    )

    @admin.display(description="Status")
    def machine_status(self, obj):
        return obj.status_label