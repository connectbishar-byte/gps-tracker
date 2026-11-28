from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


def default_retention_days():
    return settings.FLEET.get("DEFAULT_RETENTION_DAYS", 60)


class Device(models.Model):
    imei = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    model = models.CharField(max_length=40, default="FMC130")
    enabled = models.BooleanField(default=True, help_text="Only enabled (allow-listed) IMEIs are accepted by the receiver.")
    firmware = models.CharField(max_length=40, blank=True)
    plate = models.CharField(max_length=40, blank=True)
    group = models.CharField(max_length=60, blank=True, default="Default")
    config_revision = models.CharField(max_length=60, blank=True, help_text="Configurator file / revision currently on the device")
    notes = models.TextField(blank=True)
    retention_days = models.PositiveIntegerField(
        "Data retention (days)", default=default_retention_days, validators=[MinValueValidator(1), MaxValueValidator(3650)],
        help_text="Records, parameters, raw packets and connections older than this are deleted by the daily purge.")
    created_utc = models.DateTimeField(default=timezone.now)

    # fleet registry (FleetConnect 360 FLT-01) - edited on the Fleet page
    VEHICLE_TYPES = [("car", "Car"), ("pickup", "Pickup"), ("van", "Van"), ("bus", "Bus"), ("truck", "Truck"),
                     ("trailer", "Trailer"), ("equipment", "Equipment"), ("motorbike", "Motorbike"), ("other", "Other")]
    AVAILABILITY = [("available", "Available"), ("in_use", "In use"), ("maintenance", "In maintenance"),
                    ("out_of_service", "Out of service")]
    vehicle_type = models.CharField(max_length=12, choices=VEHICLE_TYPES, default="car")
    make_model = models.CharField("Make / model", max_length=80, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(1950), MaxValueValidator(2100)])
    vin = models.CharField("VIN / chassis", max_length=40, blank=True)
    capacity = models.CharField(max_length=60, blank=True, help_text="e.g. 3 t, 14 seats, 2 pallets")
    fuel_type = models.CharField(max_length=20, blank=True)
    odometer_km = models.FloatField("Odometer (km)", null=True, blank=True, validators=[MinValueValidator(0)],
                                    help_text="Reading on the date below; GPS distance since then is added to it.")
    odometer_date = models.DateField(null=True, blank=True)
    sim_number = models.CharField("SIM phone number", max_length=30, blank=True)
    sim_iccid = models.CharField("SIM ICCID", max_length=30, blank=True)
    availability = models.CharField(max_length=16, choices=AVAILABILITY, default="available")

    # denormalised status, maintained by the ingest pipeline
    last_connection_utc = models.DateTimeField(null=True, blank=True)
    last_packet_utc = models.DateTimeField(null=True, blank=True)
    last_record_ts = models.DateTimeField(null=True, blank=True)
    last_fix_ts = models.DateTimeField(null=True, blank=True)
    last_fix_lat = models.FloatField(null=True, blank=True)
    last_fix_lon = models.FloatField(null=True, blank=True)
    last_fix_speed = models.IntegerField(null=True, blank=True)
    last_fix_angle = models.IntegerField(null=True, blank=True)
    last_fix_sats = models.IntegerField(null=True, blank=True)
    last_ignition = models.BooleanField(null=True, blank=True)
    last_movement = models.BooleanField(null=True, blank=True)
    last_gsm_signal = models.IntegerField(null=True, blank=True)
    last_ext_voltage = models.FloatField(null=True, blank=True)
    packet_count = models.PositiveIntegerField(default=0)
    record_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["group", "name"]

    def __str__(self):
        return f"{self.name} ({self.masked_imei})"

    @property
    def masked_imei(self):
        return mask_imei(self.imei)


def mask_imei(imei: str) -> str:
    if not imei or len(imei) < 8:
        return imei
    return imei[:4] + "*" * (len(imei) - 8) + imei[-4:]


class Connection(models.Model):
    device = models.ForeignKey(Device, null=True, blank=True, on_delete=models.SET_NULL, related_name="connections")
    claimed_imei = models.CharField(max_length=40, blank=True)
    remote = models.CharField(max_length=80)
    source = models.CharField(max_length=20, default="tcp", help_text="tcp | simulator | probe")
    opened_utc = models.DateTimeField(default=timezone.now)
    closed_utc = models.DateTimeField(null=True, blank=True)
    handshake = models.CharField(max_length=20, default="pending")  # accepted | rejected | invalid | timeout | pending
    frames = models.PositiveIntegerField(default=0)
    close_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-opened_utc"]


class RawPacket(models.Model):
    connection = models.ForeignKey(Connection, null=True, blank=True, on_delete=models.SET_NULL, related_name="packets")
    device = models.ForeignKey(Device, null=True, blank=True, on_delete=models.SET_NULL, related_name="packets")
    received_utc = models.DateTimeField(default=timezone.now, db_index=True)
    hex = models.TextField()
    size = models.PositiveIntegerField()
    truncated = models.BooleanField(default=False, help_text="HEX was cut to the storage bound")
    crc_ok = models.BooleanField(null=True)
    crc_value = models.CharField(max_length=8, blank=True)
    codec = models.CharField(max_length=8, blank=True)
    record_count = models.PositiveIntegerField(default=0)
    new_records = models.PositiveIntegerField(default=0)
    duplicate_records = models.PositiveIntegerField(default=0)
    parse_status = models.CharField(max_length=12, default="ok")  # ok | error
    error = models.CharField(max_length=300, blank=True)
    ack_status = models.CharField(max_length=12, default="pending")  # sent | nack | none | pending
    ack_value = models.IntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, db_index=True)

    class Meta:
        ordering = ["-received_utc", "-id"]


class AVLRecord(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="records")
    packet = models.ForeignKey(RawPacket, on_delete=models.CASCADE, related_name="records")
    index = models.PositiveSmallIntegerField()
    timestamp_utc = models.DateTimeField(db_index=True)
    received_utc = models.DateTimeField(default=timezone.now)
    latitude = models.FloatField()
    longitude = models.FloatField()
    altitude = models.IntegerField()
    angle = models.IntegerField()
    satellites = models.IntegerField()
    speed = models.IntegerField(help_text="GNSS speed km/h from GPS element")
    priority = models.SmallIntegerField()
    event_io_id = models.IntegerField()
    valid_fix = models.BooleanField(default=False)
    late = models.BooleanField(default=False, help_text="Arrived after a newer record for the same device")
    fingerprint = models.CharField(max_length=64)

    class Meta:
        ordering = ["-timestamp_utc"]
        constraints = [models.UniqueConstraint(fields=["device", "fingerprint"], name="uniq_device_record")]
        indexes = [models.Index(fields=["device", "timestamp_utc"])]


class IOValue(models.Model):
    """One row per parameter (AVL I/O element) per record. device and timestamp_utc are copied from the
    record so a parameter can be charted for a vehicle without a join, and purged by date."""
    record = models.ForeignKey(AVLRecord, on_delete=models.CASCADE, related_name="io")
    device = models.ForeignKey(Device, null=True, blank=True, on_delete=models.CASCADE, related_name="+", db_index=False)
    timestamp_utc = models.DateTimeField(null=True, blank=True)
    avl_id = models.IntegerField()
    byte_length = models.SmallIntegerField()
    raw_hex = models.CharField(max_length=2048)
    value = models.FloatField(null=True, blank=True)
    text = models.CharField(max_length=200, blank=True)
    name = models.CharField(max_length=80, blank=True)
    unit = models.CharField(max_length=20, blank=True)
    known = models.BooleanField(default=True)
    mapping_version = models.CharField(max_length=40)

    class Meta:
        ordering = ["avl_id"]
        indexes = [models.Index(fields=["device", "avl_id", "timestamp_utc"], name="io_device_param_ts"),
                   models.Index(fields=["device", "timestamp_utc"], name="io_device_ts")]


class ReceiverHeartbeat(models.Model):
    """Single row per receiver instance; written every few seconds so the web UI can show listener health."""
    name = models.CharField(max_length=60, unique=True)
    host = models.CharField(max_length=60)
    port = models.IntegerField()
    started_utc = models.DateTimeField()
    last_beat_utc = models.DateTimeField()
    active_connections = models.IntegerField(default=0)
    total_connections = models.IntegerField(default=0)
    frames_ok = models.IntegerField(default=0)
    frames_bad = models.IntegerField(default=0)
    pid = models.IntegerField(default=0)


class TestRun(models.Model):
    """Result of an automated connectivity / acceptance test run (from the web UI or CLI)."""
    started_utc = models.DateTimeField(default=timezone.now)
    target = models.CharField(max_length=120)
    passed = models.BooleanField(default=False)
    summary = models.CharField(max_length=200, blank=True)
    steps = models.JSONField(default=list)

    class Meta:
        ordering = ["-started_utc"]


class PortEvent(models.Model):
    """
    Everything that happens on the tracker TCP port, in order: connections, every byte received and
    sent (HEX), handshakes, decoded frame summaries, ACKs, errors and disconnects. Feeds the live
    Port terminal page, the /api/v1/port-events/ endpoint and `manage.py port_monitor`.
    """
    KINDS = [
        ("connect", "Connection opened"), ("refused", "Connection refused"), ("rx", "Bytes received"),
        ("tx", "Bytes sent"), ("imei", "IMEI handshake"), ("frame", "Decoded frame"), ("error", "Error"),
        ("close", "Connection closed"), ("info", "Receiver info"),
    ]
    ts = models.DateTimeField(default=timezone.now, db_index=True)
    kind = models.CharField(max_length=10, choices=KINDS)
    level = models.CharField(max_length=8, default="info")  # info | ok | warn | error
    remote = models.CharField(max_length=80, blank=True, db_index=True)
    imei = models.CharField(max_length=40, blank=True, db_index=True)
    connection = models.ForeignKey(Connection, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    packet = models.ForeignKey(RawPacket, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    size = models.PositiveIntegerField(default=0)
    hex = models.TextField(blank=True)
    truncated = models.BooleanField(default=False)
    message = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.ts:%H:%M:%S} {self.kind} {self.remote} {self.message}"


class IntegrationSetting(models.Model):
    """
    Admin-editable integration settings (Google Maps key, OpenAI key and model). Values are encrypted
    at rest with a key derived from DJANGO SECRET_KEY - see tracking/integrations.py. Read them only
    through integrations.get(); never directly.
    """
    key = models.CharField(max_length=64, unique=True)
    value = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=150, blank=True)

    def __str__(self):
        return self.key


class SavedPlace(models.Model):
    """
    A place saved from the mobile car view (/drive/): either where a drive was stopped ("trip"), with that
    drive's summary, or a spot pinned while driving ("pin"), or a row imported from a KML/CSV/GeoJSON file.
    Listed on /places/ and exportable as KML (Google My Maps / Google Earth), CSV and GeoJSON.
    """
    KINDS = [("trip", "Drive stop"), ("pin", "Pinned spot"), ("import", "Imported")]
    SOURCES = [("phone", "Phone GPS"), ("vehicle", "Vehicle tracker"), ("file", "Imported file")]

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="saved_places")
    device = models.ForeignKey(Device, null=True, blank=True, on_delete=models.SET_NULL, related_name="saved_places")
    kind = models.CharField(max_length=10, choices=KINDS, default="trip")
    source = models.CharField(max_length=10, choices=SOURCES, default="phone")
    name = models.CharField(max_length=120)
    notes = models.TextField(blank=True)
    latitude = models.FloatField(validators=[MinValueValidator(-90), MaxValueValidator(90)])
    longitude = models.FloatField(validators=[MinValueValidator(-180), MaxValueValidator(180)])
    accuracy_m = models.FloatField(null=True, blank=True)
    address = models.CharField(max_length=300, blank=True)
    created_utc = models.DateTimeField(default=timezone.now, db_index=True)

    # drive summary (kind == "trip")
    started_utc = models.DateTimeField(null=True, blank=True)
    ended_utc = models.DateTimeField(null=True, blank=True)
    start_latitude = models.FloatField(null=True, blank=True)
    start_longitude = models.FloatField(null=True, blank=True)
    start_address = models.CharField(max_length=300, blank=True)
    distance_km = models.FloatField(null=True, blank=True)
    duration_s = models.PositiveIntegerField(null=True, blank=True)
    moving_s = models.PositiveIntegerField(null=True, blank=True)
    max_speed_kmh = models.FloatField(null=True, blank=True)
    avg_speed_kmh = models.FloatField(null=True, blank=True)
    driver_score = models.PositiveSmallIntegerField(null=True, blank=True)
    driver_rating = models.CharField(max_length=12, blank=True)  # good | fair | aggressive
    harsh_accel = models.PositiveIntegerField(default=0)
    harsh_brake = models.PositiveIntegerField(default=0)
    harsh_turn = models.PositiveIntegerField(default=0)
    overspeed_s = models.PositiveIntegerField(default=0)
    track = models.JSONField(default=list, blank=True, help_text="[[lat, lon], ...] of the drive, thinned")

    class Meta:
        ordering = ["-created_utc"]
        indexes = [models.Index(fields=["owner", "created_utc"], name="place_owner_ts")]

    def __str__(self):
        return self.name


class Geofence(models.Model):
    """
    A zone drawn on the Geofences page: a circle (centre + radius) or a polygon. The ingest pipeline checks
    every new GPS fix against the active zones (tracking/geofence.py) and logs a GeofenceEvent when a vehicle
    enters or leaves one; a WhatsApp alert is sent for the crossings the zone asks for (tracking/whatsapp.py).
    """
    SHAPES = [("circle", "Circle"), ("polygon", "Polygon")]

    name = models.CharField(max_length=100)
    shape = models.CharField(max_length=10, choices=SHAPES, default="circle")
    center_lat = models.FloatField(null=True, blank=True, validators=[MinValueValidator(-90), MaxValueValidator(90)])
    center_lon = models.FloatField(null=True, blank=True, validators=[MinValueValidator(-180), MaxValueValidator(180)])
    radius_m = models.FloatField(null=True, blank=True, validators=[MinValueValidator(10), MaxValueValidator(500000)])
    points = models.JSONField(default=list, blank=True, help_text="Polygon corners [[lat, lon], ...]")
    color = models.CharField(max_length=9, default="#2563eb")
    active = models.BooleanField(default=True)
    alert_enter = models.BooleanField("Alert on enter", default=True)
    alert_exit = models.BooleanField("Alert on exit", default=False)
    devices = models.ManyToManyField(Device, blank=True, related_name="geofences",
                                     help_text="Vehicles this zone watches. None selected = every vehicle.")
    recipients = models.TextField(blank=True, help_text="WhatsApp numbers for this zone only (blank = the default list "
                                                        "on Settings → WhatsApp alerts).")
    notes = models.TextField(blank=True)
    created_by = models.CharField(max_length=150, blank=True)
    created_utc = models.DateTimeField(default=timezone.now)
    updated_utc = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class GeofenceState(models.Model):
    """Whether a vehicle is currently inside a zone (one row per zone and vehicle)."""
    geofence = models.ForeignKey(Geofence, on_delete=models.CASCADE, related_name="states")
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="geofence_states")
    inside = models.BooleanField(default=False)
    since_utc = models.DateTimeField(null=True, blank=True)
    last_fix_utc = models.DateTimeField(null=True, blank=True)
    last_alert_utc = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["geofence", "device"], name="uniq_geofence_device")]


class GeofenceEvent(models.Model):
    """One crossing: a vehicle entered or left a zone. Kept for the vehicle's retention period."""
    KINDS = [("enter", "Entered"), ("exit", "Left")]
    ALERT = [("off", "No alert asked"), ("pending", "Sending"), ("sent", "Sent"), ("failed", "Failed"),
             ("skipped", "Not sent")]

    geofence = models.ForeignKey(Geofence, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    geofence_name = models.CharField(max_length=100)
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="geofence_events")
    kind = models.CharField(max_length=5, choices=KINDS)
    timestamp_utc = models.DateTimeField(db_index=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    speed = models.IntegerField(default=0)
    created_utc = models.DateTimeField(default=timezone.now)
    alert_status = models.CharField(max_length=8, choices=ALERT, default="off")
    alert_detail = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-timestamp_utc", "-id"]
        indexes = [models.Index(fields=["device", "timestamp_utc"], name="gfevent_device_ts")]


class WhatsAppMessage(models.Model):
    """
    Log of WhatsApp traffic through the Meta Cloud API: messages this site sent (text, image, PDF, template) and
    the delivery statuses / replies Meta posts to the webhook (/whatsapp/webhook/). Shown on Settings → WhatsApp.
    """
    DIRECTIONS = [("out", "Sent"), ("status", "Delivery status"), ("in", "Received")]

    created_utc = models.DateTimeField(default=timezone.now, db_index=True)
    direction = models.CharField(max_length=6, choices=DIRECTIONS, default="out")
    source = models.CharField(max_length=12, blank=True, help_text="test | api | alert | webhook")
    number = models.CharField(max_length=20, blank=True)
    kind = models.CharField(max_length=12, blank=True, help_text="text | image | document | template | status")
    wamid = models.CharField(max_length=200, blank=True, db_index=True)
    status = models.CharField(max_length=12, blank=True, help_text="accepted | failed | sent | delivered | read | received")
    status_code = models.IntegerField(null=True, blank=True)
    detail = models.CharField(max_length=500, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    response = models.JSONField(default=dict, blank=True, help_text="Meta's answer")
    request_id = models.CharField(max_length=40, blank=True)
    meta = models.JSONField(default=dict, blank=True, help_text="URL and masked Authorization header")

    class Meta:
        ordering = ["-created_utc", "-id"]


class AlertRule(models.Model):
    """
    One line of the alert rule engine (Alerts page): *when* something happens to a vehicle (kind + its settings),
    *who* is told (recipients) and *how* (channels: WhatsApp now, SMS later). Evaluated by tracking/rules.py.
    """
    KINDS = [
        ("geofence_enter", "Enters a geofence"), ("geofence_exit", "Leaves a geofence"),
        ("overspeed", "Over speed"), ("idle", "Idling too long"), ("crash", "Crash detected"),
        ("harsh", "Harsh driving"), ("ignition_on", "Ignition on"), ("ignition_off", "Ignition off"),
        ("sos", "SOS / panic button"), ("power_loss", "Power cut / tracker unplugged"),
        ("towing", "Towing / moved with ignition off"), ("offline", "No data (offline)"),
    ]
    INCIDENT_KINDS = {"crash", "sos", "power_loss", "towing", "offline"}
    CHANNELS = [("whatsapp", "WhatsApp"), ("sms", "SMS (coming later)")]

    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=20, choices=KINDS)
    active = models.BooleanField(default=True)
    devices = models.ManyToManyField(Device, blank=True, related_name="alert_rules",
                                     help_text="None selected = every vehicle.")
    geofence = models.ForeignKey(Geofence, null=True, blank=True, on_delete=models.CASCADE, related_name="rules",
                                 help_text="Geofence rules: this zone only (blank = any zone).")
    speed_limit = models.PositiveIntegerField(null=True, blank=True, help_text="Over speed: km/h")
    duration_s = models.PositiveIntegerField(default=0, help_text="Over speed: for at least this long. Idle: idle for this long.")
    channels = models.JSONField(default=list, blank=True)
    recipients = models.TextField(blank=True, help_text="Numbers in international format (blank = the default "
                                                        "WhatsApp numbers on Settings).")
    message = models.TextField(blank=True, help_text="Message text; blank = a standard text for this kind.")
    cooldown_min = models.PositiveIntegerField(default=10, help_text="At most one alert per vehicle in this many minutes.")
    threshold = models.FloatField(null=True, blank=True, help_text="Power cut: alert below this many volts (default 6 V).")
    io_id = models.PositiveIntegerField("Input (AVL ID)", null=True, blank=True,
                                        help_text="SOS: the tracker input wired to the button (default 2 = DIN2).")
    create_incident = models.BooleanField("Open an incident", default=False,
                                          help_text="Log it on the Incidents page to acknowledge, escalate and close.")
    escalate_after_min = models.PositiveIntegerField("Escalate after (minutes)", default=0,
                                                     help_text="Not acknowledged in this long → tell the escalation numbers. 0 = never.")
    escalation_recipients = models.TextField(blank=True, help_text="WhatsApp numbers for escalation, one per line.")
    created_by = models.CharField(max_length=150, blank=True)
    created_utc = models.DateTimeField(default=timezone.now)
    updated_utc = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class AlertRuleState(models.Model):
    """Running state of a rule for one vehicle (e.g. 'over the limit since 10:02', 'idle since 09:40')."""
    rule = models.ForeignKey(AlertRule, on_delete=models.CASCADE, related_name="states")
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="alert_states")
    active = models.BooleanField(default=False)
    since_utc = models.DateTimeField(null=True, blank=True)
    fired = models.BooleanField(default=False, help_text="Already alerted for the current episode")
    peak = models.FloatField(null=True, blank=True)
    last_ts = models.DateTimeField(null=True, blank=True)
    last_fired_utc = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["rule", "device"], name="uniq_rule_device")]


class AlertEvent(models.Model):
    """One alert raised by a rule, with what happened to each channel."""
    rule = models.ForeignKey(AlertRule, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    rule_name = models.CharField(max_length=100)
    kind = models.CharField(max_length=20)
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="alert_events")
    timestamp_utc = models.DateTimeField(db_index=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    speed = models.IntegerField(default=0)
    text = models.TextField(blank=True)
    status = models.CharField(max_length=8, default="pending")  # pending | sent | failed | skipped
    detail = models.CharField(max_length=500, blank=True)
    created_utc = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-timestamp_utc", "-id"]
        indexes = [models.Index(fields=["device", "timestamp_utc"], name="alert_device_ts")]


class Driver(models.Model):
    """A driver in the fleet registry (FleetConnect 360 FLT-01 / IVM-02)."""
    name = models.CharField(max_length=100)
    employee_id = models.CharField("Employee ID", max_length=40, blank=True)
    phone = models.CharField(max_length=30, blank=True, help_text="International format, e.g. +971501234567")
    email = models.EmailField(blank=True)
    licence_number = models.CharField(max_length=40, blank=True)
    licence_class = models.CharField(max_length=40, blank=True)
    ibutton_id = models.CharField("iButton / driver card ID", max_length=40, blank=True,
                                  help_text="As the tracker reports it (AVL 78), decimal or HEX. Touching the key "
                                            "starts this driver's assignment to the vehicle automatically.")
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="driver_profile", help_text="Login used by this driver, if any.")
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_utc = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class DriverAssignment(models.Model):
    """Who drove (or is assigned to) a vehicle from start_utc to end_utc (open = current)."""
    SOURCES = [("manual", "Assigned on the Fleet page"), ("ibutton", "iButton / driver card"), ("api", "REST API")]
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name="assignments")
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="assignments")
    start_utc = models.DateTimeField(default=timezone.now, db_index=True)
    end_utc = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=8, choices=SOURCES, default="manual")
    created_by = models.CharField(max_length=150, blank=True)
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-start_utc", "-id"]
        indexes = [models.Index(fields=["device", "start_utc"], name="assign_device_ts")]

    def __str__(self):
        return f"{self.driver} → {self.device.name}"


class FleetDocument(models.Model):
    """Registration, insurance, licence, permit... for a vehicle or a driver, with an expiry reminder (FLT-05)."""
    KINDS = [("registration", "Registration (Mulkiya)"), ("insurance", "Insurance"), ("licence", "Driving licence"),
             ("permit", "Permit / pass"), ("inspection", "Inspection / test"), ("contract", "Contract / lease"),
             ("other", "Other")]
    kind = models.CharField(max_length=14, choices=KINDS, default="registration")
    title = models.CharField(max_length=120, blank=True)
    number = models.CharField("Document number", max_length=60, blank=True)
    device = models.ForeignKey(Device, null=True, blank=True, on_delete=models.CASCADE, related_name="documents")
    driver = models.ForeignKey(Driver, null=True, blank=True, on_delete=models.CASCADE, related_name="documents")
    issuer = models.CharField(max_length=80, blank=True)
    issued = models.DateField(null=True, blank=True)
    expiry = models.DateField(null=True, blank=True, db_index=True)
    remind_days = models.PositiveIntegerField("Remind days before", default=30, validators=[MaxValueValidator(365)])
    link = models.URLField("Link to scan", blank=True, help_text="Where the scan is kept (Drive, SharePoint...).")
    notes = models.TextField(blank=True)
    last_reminder_on = models.DateField(null=True, blank=True)
    created_utc = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["expiry", "id"]

    def __str__(self):
        return self.title or self.get_kind_display()

    @property
    def owner_name(self):
        return self.device.name if self.device_id else (self.driver.name if self.driver_id else "")

    def days_left(self, today=None):
        if not self.expiry:
            return None
        return (self.expiry - (today or timezone.localdate())).days

    def state(self, today=None):
        d = self.days_left(today)
        if d is None:
            return "none"
        if d < 0:
            return "expired"
        return "due" if d <= self.remind_days else "ok"


class Incident(models.Model):
    """
    A critical event to be handled by a person (FleetConnect 360 GPS-05 / IVM-05): crash, SOS, power cut, towing,
    vehicle offline. Opened by an alert rule with "Open an incident" ticked; repeats of the same kind on the same
    vehicle while it is still open are counted on it instead of opening another. Every step is kept in IncidentNote.
    """
    STATUSES = [("open", "Open"), ("acknowledged", "Acknowledged"), ("escalated", "Escalated"), ("closed", "Closed")]
    SEVERITIES = [("critical", "Critical"), ("high", "High"), ("medium", "Medium")]
    RESOLUTIONS = [("genuine", "Genuine — handled"), ("false_alarm", "False alarm"), ("test", "Test"), ("other", "Other")]

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="incidents")
    driver = models.ForeignKey("Driver", null=True, blank=True, on_delete=models.SET_NULL, related_name="incidents")
    rule = models.ForeignKey(AlertRule, null=True, blank=True, on_delete=models.SET_NULL, related_name="incidents")
    alert = models.ForeignKey(AlertEvent, null=True, blank=True, on_delete=models.SET_NULL, related_name="incidents")
    kind = models.CharField(max_length=20)
    title = models.CharField(max_length=200)
    severity = models.CharField(max_length=8, choices=SEVERITIES, default="high")
    status = models.CharField(max_length=12, choices=STATUSES, default="open", db_index=True)
    timestamp_utc = models.DateTimeField(db_index=True, help_text="When it happened (tracker time)")
    opened_utc = models.DateTimeField(default=timezone.now)
    last_seen_utc = models.DateTimeField(null=True, blank=True)
    occurrences = models.PositiveIntegerField(default=1)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    speed = models.IntegerField(default=0)
    acknowledged_by = models.CharField(max_length=150, blank=True)
    acknowledged_utc = models.DateTimeField(null=True, blank=True)
    escalated_utc = models.DateTimeField(null=True, blank=True)
    closed_by = models.CharField(max_length=150, blank=True)
    closed_utc = models.DateTimeField(null=True, blank=True)
    resolution = models.CharField(max_length=12, choices=RESOLUTIONS, blank=True)

    class Meta:
        ordering = ["-opened_utc", "-id"]
        indexes = [models.Index(fields=["device", "kind", "status"], name="incident_dev_kind_status")]

    def __str__(self):
        return self.title

    @property
    def is_open(self):
        return self.status != "closed"


class IncidentNote(models.Model):
    """Audit trail of an incident: opened, repeated, acknowledged, escalated, comments, closed, reopened."""
    ACTIONS = [("opened", "Opened"), ("repeat", "Happened again"), ("ack", "Acknowledged"), ("escalate", "Escalated"),
               ("auto_escalate", "Escalated automatically"), ("comment", "Comment"), ("close", "Closed"),
               ("reopen", "Reopened"), ("notify", "Notification")]
    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, related_name="notes")
    created_utc = models.DateTimeField(default=timezone.now)
    user = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=14, choices=ACTIONS)
    text = models.TextField(blank=True)

    class Meta:
        ordering = ["created_utc", "id"]
