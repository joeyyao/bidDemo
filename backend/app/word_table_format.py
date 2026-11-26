import re

from docx.oxml.ns import qn


def word_value(node, name, attribute="val"):
    child = node.find(qn(f"w:{name}")) if node is not None else None
    return child.get(qn(f"w:{attribute}")) if child is not None else None


def word_color(value):
    return f"#{value}" if value and re.fullmatch(r"[0-9a-fA-F]{6}", value) else None


def word_length(value, units_per_point):
    if value and value.isascii() and value.isdigit() and len(value) <= 8:
        return f"{int(value) / units_per_point:g}pt"
    return None


def table_format(table, styles):
    direct = table.find(qn("w:tblPr"))
    style_id = word_value(direct, "tblStyle") or "TableNormal"
    chain = []
    seen = set()
    while style_id in styles and style_id not in seen:
        seen.add(style_id)
        style = styles[style_id]
        chain.append(style)
        style_id = word_value(style, "basedOn")
    properties = [style.find(qn("w:tblPr")) for style in reversed(chain)] + [direct]
    borders = {}
    margins = {}
    for item in properties:
        borders.update(borders_for(item, "tblBorders"))
        margins.update(margins_for(item, "tblCellMar"))
    alignment = word_value(direct, "jc")
    width_type = word_value(direct, "tblW", "type")
    width_value = word_value(direct, "tblW", "w")
    width = word_length(width_value, 20) if width_type == "dxa" else None
    if width_type == "pct" and width_value and re.fullmatch(r"[0-9]{1,5}", width_value):
        width = f"{min(int(width_value) / 50, 100):g}%"
    return borders, margins, width, alignment


def borders_for(properties, name):
    elements = properties.find(qn(f"w:{name}")) if properties is not None else None
    if elements is None:
        return {}
    borders = {}
    for line in elements:
        side = line.tag.rsplit("}", 1)[-1]
        kind = line.get(qn("w:val"))
        if kind in ("none", "nil"):
            borders[side] = "none"
        elif kind:
            css_kind = {
                "double": "double", "triple": "double", "dotted": "dotted",
                "dashed": "dashed", "dashSmallGap": "dashed", "dotDash": "dashed",
            }.get(kind, "solid")
            width = word_length(line.get(qn("w:sz")), 8) or "0.5pt"
            color = word_color(line.get(qn("w:color"))) or "#000000"
            borders[side] = f"{width} {css_kind} {color}"
    return borders


def margins_for(properties, name):
    elements = properties.find(qn(f"w:{name}")) if properties is not None else None
    if elements is None:
        return {}
    margins = {}
    for element in elements:
        size = word_length(element.get(qn("w:w")), 20)
        if size is not None:
            margins[element.tag.rsplit("}", 1)[-1]] = size
    return margins


def cell_format(cell, margins):
    properties = cell.find(qn("w:tcPr"))
    padding = {**margins, **margins_for(properties, "tcMar")}
    css = {}
    for side, logical in (("top", "top"), ("right", "end"), ("bottom", "bottom"), ("left", "start")):
        value = padding.get(logical, padding.get(side))
        if value is not None:
            css[f"padding-{side}"] = value
    fill = word_color(word_value(properties, "shd", "fill"))
    if fill:
        css["background-color"] = fill
    vertical = word_value(properties, "vAlign")
    if vertical in ("top", "center", "bottom"):
        css["vertical-align"] = "middle" if vertical == "center" else vertical
    paragraphs = cell.findall(qn("w:p"))
    alignments = {word_value(p.find(qn("w:pPr")), "jc") for p in paragraphs}
    if len(alignments) == 1:
        align = alignments.pop()
        if align in ("left", "right", "center", "both", "start", "end"):
            css["text-align"] = "justify" if align == "both" else align
    runs = [run.find(qn("w:rPr")) for paragraph in paragraphs for run in paragraph.iter(qn("w:r"))
            if any(text.text for text in run.findall(qn("w:t")))]
    if runs:
        sizes = {word_length(word_value(run, "sz"), 2) for run in runs}
        colors = {word_color(word_value(run, "color")) for run in runs}
        if len(sizes) == 1 and None not in sizes:
            css["font-size"] = sizes.pop()
        if len(colors) == 1 and None not in colors:
            css["color"] = colors.pop()
        if all(run is not None and run.find(qn("w:b")) is not None
               and word_value(run, "b") not in ("0", "false", "off") for run in runs):
            css["font-weight"] = "bold"
    return css, borders_for(properties, "tcBorders")
