from django.contrib import admin

from .models import (
    Machine,
    MachineCurrentState,
    MachineReading,
    PlcController,
    SignalMapping,
)


admin.site.site_header = "KV8000 Machine Monitoring"
admin.site.site_title = "KV8000 Monitoring"
admin.site.index_title = "System Administration"


@admin.register(PlcController)
class PlcControllerAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "host",
        "port",
        "protocol",
        "is_active",
        "health_label",
        "last_seen_at",
        "consecutive_failures",
    )
    list_filter = ("is_active", "protocol")
    search_fields = ("code", "name", "host")
    ordering = ("code",)
    readonly_fields = (
        "last_poll_at",
        "last_seen_at",
        "last_error",
        "consecutive_failures",
        "created_at",
        "updated_at",
    )


class SignalMappingInline(admin.TabularInline):
    model = SignalMapping
    extra = 0
    fields = (
        "signal",
        "address",
        "data_type",
        "word_order",
        "scale",
        "offset",
        "is_enabled",
    )


@admin.register(Machine)
class MachineAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "controller",
        "mapping_count",
        "is_active",
        "updated_at",
    )
    list_filter = ("is_active", "controller")
    search_fields = ("code", "name", "controller__code", "controller__host")
    ordering = ("code",)
    inlines = [SignalMappingInline]

    @admin.display(description="Mappings")
    def mapping_count(self, obj):
        return obj.signal_mappings.filter(is_enabled=True).count()


@admin.register(SignalMapping)
class SignalMappingAdmin(admin.ModelAdmin):
    list_display = (
        "machine",
        "signal",
        "address",
        "data_type",
        "word_order",
        "is_enabled",
    )
    list_filter = ("signal", "data_type", "is_enabled", "machine__controller")
    search_fields = (
        "machine__code",
        "machine__name",
        "address",
        "machine__controller__code",
    )
    ordering = ("machine__code", "signal")




@admin.register(MachineCurrentState)
class MachineCurrentStateAdmin(admin.ModelAdmin):
    list_display = (
        "machine",
        "recorded_at",
        "machine_status",
        "production_count",
        "cycle_time_ms",
        "alarm_code",
        "recipe_no",
    )
    list_filter = ("plc_online", "run_bit", "stop_bit", "alarm_bit")
    search_fields = ("machine__code", "machine__name")
    ordering = ("machine__code",)
    readonly_fields = [field.name for field in MachineCurrentState._meta.fields]

    @admin.display(description="Status")
    def machine_status(self, obj):
        return obj.status_label

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
    )
    search_fields = ("machine__code", "machine__name")
    date_hierarchy = "recorded_at"
    ordering = ("-recorded_at",)
    readonly_fields = [field.name for field in MachineReading._meta.fields]

    @admin.display(description="Status")
    def machine_status(self, obj):
        return obj.status_label

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
