from django.contrib import admin

from .models import (AlertEvent, Expense, MaintenancePlan, WorkOrder, Incident, IncidentNote, Driver, DriverAssignment, FleetDocument, AlertRule, AVLRecord, Connection, Device, Geofence, GeofenceEvent, IOValue, PortEvent, RawPacket,
                     ReceiverHeartbeat, TestRun, WhatsAppMessage)


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("name", "masked_imei", "model", "enabled", "firmware", "last_packet_utc", "packet_count", "error_count")
    list_filter = ("enabled", "model", "group")
    search_fields = ("name", "imei", "plate")


@admin.register(Connection)
class ConnectionAdmin(admin.ModelAdmin):
    list_display = ("opened_utc", "device", "claimed_imei", "remote", "source", "handshake", "frames", "close_reason")
    list_filter = ("handshake", "source")


@admin.register(RawPacket)
class RawPacketAdmin(admin.ModelAdmin):
    list_display = ("received_utc", "device", "codec", "size", "crc_ok", "record_count", "parse_status", "ack_status")
    list_filter = ("parse_status", "ack_status", "codec")


class IOInline(admin.TabularInline):
    model = IOValue
    extra = 0


@admin.register(AVLRecord)
class AVLRecordAdmin(admin.ModelAdmin):
    list_display = ("timestamp_utc", "device", "latitude", "longitude", "speed", "satellites", "valid_fix", "late")
    list_filter = ("valid_fix", "late", "device")
    inlines = [IOInline]


admin.site.register(ReceiverHeartbeat)
admin.site.register(TestRun)


@admin.register(PortEvent)
class PortEventAdmin(admin.ModelAdmin):
    list_display = ("id", "ts", "kind", "level", "remote", "imei", "size", "message")
    list_filter = ("kind", "level")
    search_fields = ("imei", "remote", "message", "hex")
    readonly_fields = [f.name for f in PortEvent._meta.fields]


@admin.register(Geofence)
class GeofenceAdmin(admin.ModelAdmin):
    list_display = ("name", "shape", "active", "alert_enter", "alert_exit", "radius_m", "updated_utc")
    list_filter = ("active", "shape")
    filter_horizontal = ("devices",)


@admin.register(GeofenceEvent)
class GeofenceEventAdmin(admin.ModelAdmin):
    list_display = ("timestamp_utc", "device", "kind", "geofence_name", "speed", "alert_status")
    list_filter = ("kind", "alert_status")
    readonly_fields = [f.name for f in GeofenceEvent._meta.fields]


@admin.register(AlertRule)
class AlertRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "active", "speed_limit", "duration_s", "cooldown_min")
    list_filter = ("kind", "active")
    filter_horizontal = ("devices",)


@admin.register(AlertEvent)
class AlertEventAdmin(admin.ModelAdmin):
    list_display = ("timestamp_utc", "device", "rule_name", "kind", "status")
    list_filter = ("kind", "status")
    readonly_fields = [f.name for f in AlertEvent._meta.fields]


@admin.register(WhatsAppMessage)
class WhatsAppMessageAdmin(admin.ModelAdmin):
    list_display = ("created_utc", "direction", "source", "number", "kind", "status", "detail")
    list_filter = ("direction", "status", "source")
    readonly_fields = [f.name for f in WhatsAppMessage._meta.fields]


@admin.register(Driver)
class DriverAdmin(admin.ModelAdmin):
    list_display = ("name", "employee_id", "phone", "licence_number", "ibutton_id", "active")
    search_fields = ("name", "employee_id", "phone", "ibutton_id")


@admin.register(DriverAssignment)
class DriverAssignmentAdmin(admin.ModelAdmin):
    list_display = ("device", "driver", "start_utc", "end_utc", "source")
    list_filter = ("source",)


@admin.register(FleetDocument)
class FleetDocumentAdmin(admin.ModelAdmin):
    list_display = ("kind", "number", "device", "driver", "expiry", "last_reminder_on")
    list_filter = ("kind",)


class IncidentNoteInline(admin.TabularInline):
    model = IncidentNote
    extra = 0


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ("opened_utc", "title", "severity", "status", "occurrences", "acknowledged_by", "closed_by")
    list_filter = ("status", "severity", "kind")
    inlines = [IncidentNoteInline]


admin.site.register(MaintenancePlan)
admin.site.register(WorkOrder)
admin.site.register(Expense)
