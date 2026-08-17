from __future__ import annotations

import copy
import os
import re
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree


DOCX = Path(
    "/home/arcosium/projects/GenomicWQB/docs/유전알고리즘_알파리서치_리포트.docx"
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
Q = lambda name: f"{{{W}}}{name}"
CITATION_RE = re.compile(r"\[([1-7](?:,[1-7])*)\]")
REFERENCE_RE = re.compile(r"^\[([1-7])\]\s+(.*)$", re.DOTALL)


def paragraph_text(paragraph) -> str:
    return "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))


def set_run_properties(run, *, footnote_reference: bool = False, size: int = 20) -> None:
    rpr = etree.SubElement(run, Q("rPr"))
    color = etree.SubElement(rpr, Q("color"))
    color.set(Q("val"), "000000")
    sz = etree.SubElement(rpr, Q("sz"))
    sz.set(Q("val"), str(size))
    sz_cs = etree.SubElement(rpr, Q("szCs"))
    sz_cs.set(Q("val"), str(size))
    if footnote_reference:
        style = etree.SubElement(rpr, Q("rStyle"))
        style.set(Q("val"), "FootnoteReference")
        valign = etree.SubElement(rpr, Q("vertAlign"))
        valign.set(Q("val"), "superscript")


def add_text_run(paragraph, text: str, base_rpr=None) -> None:
    if not text:
        return
    run = etree.SubElement(paragraph, Q("r"))
    if base_rpr is not None:
        run.append(copy.deepcopy(base_rpr))
    else:
        set_run_properties(run)
    node = etree.SubElement(run, Q("t"))
    if text[:1].isspace() or text[-1:].isspace():
        node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    node.text = text


def add_footnote_reference(paragraph, footnote_id: int) -> None:
    run = etree.SubElement(paragraph, Q("r"))
    set_run_properties(run, footnote_reference=True, size=16)
    ref = etree.SubElement(run, Q("footnoteReference"))
    ref.set(Q("id"), str(footnote_id))


def replace_citations_with_footnotes(document_root, references: dict[int, str]):
    footnote_specs: list[tuple[int, str]] = []
    next_id = 1

    for paragraph in document_root.xpath(".//w:body//w:p", namespaces=NS):
        text = paragraph_text(paragraph)
        if not CITATION_RE.search(text):
            continue

        first_run = paragraph.find(Q("r"))
        base_rpr = None
        if first_run is not None:
            current_rpr = first_run.find(Q("rPr"))
            if current_rpr is not None:
                base_rpr = copy.deepcopy(current_rpr)

        ppr = paragraph.find(Q("pPr"))
        for child in list(paragraph):
            if child is not ppr:
                paragraph.remove(child)

        cursor = 0
        for match in CITATION_RE.finditer(text):
            add_text_run(paragraph, text[cursor : match.start()], base_rpr)
            reference_ids = [int(value) for value in match.group(1).split(",")]
            note_text = "; ".join(references[value] for value in reference_ids)
            add_footnote_reference(paragraph, next_id)
            footnote_specs.append((next_id, note_text))
            next_id += 1
            cursor = match.end()
        add_text_run(paragraph, text[cursor:], base_rpr)

    return footnote_specs


def remove_reference_paragraphs(document_root) -> dict[int, str]:
    references: dict[int, str] = {}
    for paragraph in list(document_root.xpath(".//w:body/w:p", namespaces=NS)):
        text = paragraph_text(paragraph)
        match = REFERENCE_RE.match(text)
        if not match:
            continue
        ref_id = int(match.group(1))
        references[ref_id] = match.group(2).strip()
        paragraph.getparent().remove(paragraph)

    missing = sorted(set(range(1, 8)) - references.keys())
    if missing:
        raise RuntimeError(f"missing references: {missing}")
    return references


def populate_footnotes(footnotes_root, specs: list[tuple[int, str]]) -> None:
    for old in list(footnotes_root.xpath('./w:footnote[number(@w:id) > 0]', namespaces=NS)):
        footnotes_root.remove(old)

    for footnote_id, text in specs:
        footnote = etree.SubElement(footnotes_root, Q("footnote"))
        footnote.set(Q("id"), str(footnote_id))
        paragraph = etree.SubElement(footnote, Q("p"))
        ppr = etree.SubElement(paragraph, Q("pPr"))
        style = etree.SubElement(ppr, Q("pStyle"))
        style.set(Q("val"), "FootnoteText")
        spacing = etree.SubElement(ppr, Q("spacing"))
        spacing.set(Q("after"), "0")
        spacing.set(Q("line"), "200")
        spacing.set(Q("lineRule"), "auto")

        reference_run = etree.SubElement(paragraph, Q("r"))
        set_run_properties(reference_run, footnote_reference=True, size=16)
        etree.SubElement(reference_run, Q("footnoteRef"))
        add_text_run(paragraph, f" {text}", None)


def ensure_footnote_styles(styles_root) -> None:
    if not styles_root.xpath('//w:style[@w:styleId="FootnoteText"]', namespaces=NS):
        style = etree.SubElement(styles_root, Q("style"))
        style.set(Q("type"), "paragraph")
        style.set(Q("styleId"), "FootnoteText")
        name = etree.SubElement(style, Q("name"))
        name.set(Q("val"), "footnote text")
        based_on = etree.SubElement(style, Q("basedOn"))
        based_on.set(Q("val"), "Normal")
        ui = etree.SubElement(style, Q("uiPriority"))
        ui.set(Q("val"), "99")
        etree.SubElement(style, Q("semiHidden"))
        etree.SubElement(style, Q("unhideWhenUsed"))
        ppr = etree.SubElement(style, Q("pPr"))
        spacing = etree.SubElement(ppr, Q("spacing"))
        spacing.set(Q("after"), "0")
        rpr = etree.SubElement(style, Q("rPr"))
        color = etree.SubElement(rpr, Q("color"))
        color.set(Q("val"), "000000")
        sz = etree.SubElement(rpr, Q("sz"))
        sz.set(Q("val"), "20")
        szcs = etree.SubElement(rpr, Q("szCs"))
        szcs.set(Q("val"), "20")

    if not styles_root.xpath('//w:style[@w:styleId="FootnoteReference"]', namespaces=NS):
        style = etree.SubElement(styles_root, Q("style"))
        style.set(Q("type"), "character")
        style.set(Q("styleId"), "FootnoteReference")
        name = etree.SubElement(style, Q("name"))
        name.set(Q("val"), "footnote reference")
        based_on = etree.SubElement(style, Q("basedOn"))
        based_on.set(Q("val"), "DefaultParagraphFont")
        ui = etree.SubElement(style, Q("uiPriority"))
        ui.set(Q("val"), "99")
        etree.SubElement(style, Q("semiHidden"))
        etree.SubElement(style, Q("unhideWhenUsed"))
        rpr = etree.SubElement(style, Q("rPr"))
        color = etree.SubElement(rpr, Q("color"))
        color.set(Q("val"), "000000")
        valign = etree.SubElement(rpr, Q("vertAlign"))
        valign.set(Q("val"), "superscript")


def main() -> None:
    with ZipFile(DOCX, "r") as source:
        entries = {name: source.read(name) for name in source.namelist()}

    document_root = etree.fromstring(entries["word/document.xml"])
    footnotes_root = etree.fromstring(entries["word/footnotes.xml"])
    styles_root = etree.fromstring(entries["word/styles.xml"])

    references = remove_reference_paragraphs(document_root)
    specs = replace_citations_with_footnotes(document_root, references)
    if len(specs) != 9:
        raise RuntimeError(f"expected 9 citation occurrences, got {len(specs)}")
    populate_footnotes(footnotes_root, specs)
    ensure_footnote_styles(styles_root)

    entries["word/document.xml"] = etree.tostring(
        document_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    entries["word/footnotes.xml"] = etree.tostring(
        footnotes_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    entries["word/styles.xml"] = etree.tostring(
        styles_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    fd, temp_name = tempfile.mkstemp(suffix=".docx", dir=str(DOCX.parent))
    os.close(fd)
    try:
        with ZipFile(temp_name, "w", ZIP_DEFLATED) as target:
            for name, data in entries.items():
                target.writestr(name, data)
        os.replace(temp_name, DOCX)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

    print(f"footnotes={len(specs)} references_moved={len(references)}")


if __name__ == "__main__":
    main()
