from django import template

register = template.Library()


@register.filter
def dur(seconds):
    """Seconds as "1 h 05 min" / "12 min"; "—" for None."""
    if seconds is None or seconds == "":
        return "—"
    m = int(round(float(seconds) / 60))
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"
