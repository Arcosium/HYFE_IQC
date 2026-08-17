from copy import deepcopy
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
import os

from lxml import etree


DOCX = Path("/home/arcosium/projects/GenomicWQB/docs/유전알고리즘_알파리서치_리포트.docx")
TMP = DOCX.with_suffix(".docx.tmp")
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
MARKER_PROPERTIES = ("keepNext", "keepLines", "pageBreakBefore", "suppressLineNumbers")


def remove_marker_properties(root: etree._Element) -> dict[str, int]:
    counts = {name: 0 for name in MARKER_PROPERTIES}
    for name in MARKER_PROPERTIES:
        for node in list(root.xpath(f"//w:{name}", namespaces=NS)):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
                counts[name] += 1
    return counts


with ZipFile(DOCX) as source:
    replacements: dict[str, bytes] = {}
    totals = {name: 0 for name in MARKER_PROPERTIES}

    for item in source.infolist():
        if not item.filename.endswith(".xml"):
            continue
        try:
            root = etree.fromstring(source.read(item.filename))
        except etree.XMLSyntaxError:
            continue

        counts = remove_marker_properties(root)
        if any(counts.values()):
            replacements[item.filename] = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )
            for name, count in counts.items():
                totals[name] += count

    expected = {
        "keepNext": 14,
        "keepLines": 11,
        "pageBreakBefore": 0,
        "suppressLineNumbers": 0,
    }
    if totals != expected:
        raise RuntimeError(f"unexpected marker property counts: {totals}; expected {expected}")

    with ZipFile(TMP, "w", ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = replacements.get(item.filename, source.read(item.filename))
            target.writestr(deepcopy(item), data)

os.replace(TMP, DOCX)
print("removed=" + ", ".join(f"{name}:{count}" for name, count in totals.items()))
