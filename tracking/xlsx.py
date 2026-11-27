"""
A small, dependency-free .xlsx writer (Office Open XML), enough for report sheets: several worksheets,
text / numbers, a bold header style, a title style, column widths and a frozen header row.

Used by the driver score sheet so the server needs no extra Python package.

    rows = [[Cell("Vehicle", "head"), Cell("Score", "head")], ["Truck 7", 86]]
    data = workbook([Sheet("Drivers", rows, widths=[24, 10], freeze=1)])
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from xml.sax.saxutils import escape

STYLES = {"": 0, "head": 1, "bold": 2, "title": 3, "good": 4, "fair": 5, "aggressive": 6, "muted": 7}


@dataclass
class Cell:
    value: object
    style: str = ""


@dataclass
class Sheet:
    name: str
    rows: list
    widths: list = field(default_factory=list)
    freeze: int = 0  # number of header rows to keep visible


def _col(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _clean(text: str) -> str:
    # XML 1.0 forbids most control characters
    return "".join(c for c in text if c in "\t\n\r" or ord(c) >= 32)


def _cell_xml(ref: str, cell) -> str:
    style = 0
    if isinstance(cell, Cell):
        style, cell = STYLES.get(cell.style, 0), cell.value
    s = f' s="{style}"' if style else ""
    if cell is None or cell == "":
        return f'<c r="{ref}"{s}/>' if style else ""
    if isinstance(cell, bool):
        cell = "Yes" if cell else "No"
    if isinstance(cell, (int, float)):
        return f'<c r="{ref}"{s}><v>{cell}</v></c>'
    if isinstance(cell, datetime):
        cell = cell.strftime("%Y-%m-%d %H:%M")
    elif isinstance(cell, date):
        cell = cell.strftime("%Y-%m-%d")
    text = escape(_clean(str(cell)))
    return f'<c r="{ref}" t="inlineStr"{s}><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet_xml(sheet: Sheet) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    if sheet.freeze:
        out.append(f'<sheetViews><sheetView workbookViewId="0"><pane ySplit="{sheet.freeze}" '
                   f'topLeftCell="A{sheet.freeze + 1}" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>')
    if sheet.widths:
        out.append("<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
                                      for i, w in enumerate(sheet.widths)) + "</cols>")
    out.append("<sheetData>")
    for r, row in enumerate(sheet.rows, start=1):
        cells = "".join(_cell_xml(f"{_col(c)}{r}", v) for c, v in enumerate(row))
        out.append(f'<row r="{r}">{cells}</row>')
    out.append("</sheetData></worksheet>")
    return "".join(out)


_STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="7">'
    '<font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="15"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><color rgb="FF15803D"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><color rgb="FFB45309"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><color rgb="FFB91C1C"/><name val="Calibri"/></font>'
    '</fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF0F766E"/><bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="8">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="0" fontId="4" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="0" fontId="5" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="0" fontId="6" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>')


def _safe_name(name: str, used: set) -> str:
    base = "".join(c for c in name if c not in '[]:*?/\\')[:31] or "Sheet"
    n, i = base, 2
    while n.lower() in used:
        suffix = f" ({i})"
        n, i = base[:31 - len(suffix)] + suffix, i + 1
    used.add(n.lower())
    return n


def workbook(sheets: list[Sheet]) -> bytes:
    used: set = set()
    names = [_safe_name(s.name, used) for s in sheets]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(len(sheets)))
                   + '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{escape(n, {chr(34): "&quot;"})}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
                             for i, n in enumerate(names))
                   + '</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                             for i in range(len(sheets)))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   '</Relationships>')
        z.writestr("xl/styles.xml", _STYLES_XML)
        for i, s in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", _sheet_xml(s))
    return buf.getvalue()
