"""
FMC130 AVL I/O dictionary (prototype subset).

Source: Teltonika "FMC130 I/O settings" wiki page. Verify each entry against the
firmware actually installed on the device before relying on the value; the mapping
version below is stored with every decoded I/O value so it can be re-interpreted later.

Unknown AVL IDs are NEVER discarded: they are stored with raw bytes and shown as
"unknown" in the I/O inspector.
"""
from __future__ import annotations

MAPPING_VERSION = "fmc130-proto-2026.11"

# id: (name, signed, multiplier, unit, value labels)
IO_MAP: dict[int, tuple] = {
    1:   ("Digital Input 1", False, 1, "", {0: "Off", 1: "On"}),
    2:   ("Digital Input 2", False, 1, "", {0: "Off", 1: "On"}),
    3:   ("Digital Input 3", False, 1, "", {0: "Off", 1: "On"}),
    6:   ("Analog Input 2", False, 0.001, "V", None),
    9:   ("Analog Input 1", False, 0.001, "V", None),
    11:  ("ICCID1", False, 1, "", None),
    12:  ("Fuel Used GPS", False, 0.001, "L", None),
    13:  ("Fuel Rate GPS", False, 0.01, "L/100km", None),
    14:  ("ICCID2", False, 1, "", None),
    15:  ("Eco Score", False, 0.01, "", None),
    16:  ("Total Odometer", False, 0.001, "km", None),
    17:  ("Axis X", True, 1, "mG", None),
    18:  ("Axis Y", True, 1, "mG", None),
    19:  ("Axis Z", True, 1, "mG", None),
    21:  ("GSM Signal", False, 1, "/5", None),
    24:  ("Speed (I/O)", False, 1, "km/h", None),
    66:  ("External Voltage", False, 0.001, "V", None),
    67:  ("Battery Voltage", False, 0.001, "V", None),
    68:  ("Battery Current", False, 0.001, "A", None),
    69:  ("GNSS Status", False, 1, "", {0: "GNSS off", 1: "On with fix", 2: "On without fix", 3: "GNSS sleep"}),
    72:  ("Dallas Temperature 1", True, 0.1, "°C", None),
    73:  ("Dallas Temperature 2", True, 0.1, "°C", None),
    74:  ("Dallas Temperature 3", True, 0.1, "°C", None),
    75:  ("Dallas Temperature 4", True, 0.1, "°C", None),
    78:  ("iButton", False, 1, "", None),
    80:  ("Data Mode", False, 1, "", {0: "Home on stop", 1: "Home on move", 2: "Roaming on stop",
                                      3: "Roaming on move", 4: "Unknown on stop", 5: "Unknown on move"}),
    113: ("Battery Level", False, 1, "%", None),
    179: ("Digital Output 1", False, 1, "", {0: "Off", 1: "On"}),
    180: ("Digital Output 2", False, 1, "", {0: "Off", 1: "On"}),
    181: ("GNSS PDOP", False, 0.1, "", None),
    182: ("GNSS HDOP", False, 0.1, "", None),
    199: ("Trip Odometer", False, 0.001, "km", None),
    200: ("Sleep Mode", False, 1, "", {0: "No sleep", 1: "GPS sleep", 2: "Deep sleep", 3: "Online sleep", 4: "Ultra sleep"}),
    205: ("GSM Cell ID", False, 1, "", None),
    206: ("GSM Area Code", False, 1, "", None),
    239: ("Ignition", False, 1, "", {0: "Off", 1: "On"}),
    240: ("Movement", False, 1, "", {0: "Off", 1: "On"}),
    241: ("Active GSM Operator", False, 1, "", None),
    246: ("Towing", False, 1, "", {0: "Steady", 1: "Towing"}),
    247: ("Crash Detection", False, 1, "", None),
    249: ("Jamming", False, 1, "", {0: "Jamming stop", 1: "Jamming start"}),
    250: ("Trip", False, 1, "", {0: "Trip stop", 1: "Trip start"}),
    251: ("Idling", False, 1, "", {0: "Moving", 1: "Idling"}),
    252: ("Unplug", False, 1, "", {0: "Battery present", 1: "Battery unplugged"}),
    253: ("Green Driving Type", False, 1, "", {1: "Acceleration", 2: "Braking", 3: "Cornering"}),
    255: ("Over Speeding", False, 1, "km/h", None),
    380: ("Digital Output 3", False, 1, "", {0: "Off", 1: "On"}),
}

# Named IDs used by dashboard logic
IGNITION = 239
MOVEMENT = 240
GSM_SIGNAL = 21
GNSS_STATUS = 69
EXT_VOLTAGE = 66
BATTERY_VOLTAGE = 67
SLEEP_MODE = 200
ODOMETER = 16


def interpret(avl_id: int, raw: bytes) -> dict:
    """Return a dict with name, value (typed), display text, unit, known flag."""
    entry = IO_MAP.get(avl_id)
    if entry is None:
        return {
            "name": f"Unknown AVL {avl_id}",
            "known": False,
            "value": int.from_bytes(raw, "big") if 0 < len(raw) <= 8 else None,
            "text": raw.hex().upper(),
            "unit": "",
        }
    name, signed, mult, unit, labels = entry
    if len(raw) > 8:
        return {"name": name, "known": True, "value": None, "text": raw.hex().upper(), "unit": unit}
    iv = int.from_bytes(raw, "big", signed=signed)
    value = round(iv * mult, 6) if mult != 1 else iv
    if labels and iv in labels:
        text = labels[iv]
    elif avl_id in (11, 14, 78):
        text = str(iv)
    else:
        text = f"{value:g} {unit}".strip() if isinstance(value, float) else f"{value} {unit}".strip()
    return {"name": name, "known": True, "value": value, "text": text, "unit": unit}
