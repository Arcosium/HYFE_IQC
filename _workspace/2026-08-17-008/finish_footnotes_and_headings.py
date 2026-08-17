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

SOURCES = {
    8: (
        "WorldQuant BRAIN. Consultant Dos and Don’ts; Getting Started: Power Pool "
        "Alphas; Getting Started: Finding Consultant Alphas; BRAIN Genius. "
        "Learn 문서, 2026-08-14 인출."
    ),
    9: (
        "2026 WorldQuant 컨설턴트 서머 부트캠프 5주차 세션"
        "(2026-08-12·13) 강의 자료."
    ),
    10: (
        "분석 원장: GenomicWQB 운영 데이터베이스(SQLite), "
        "alphas·rounds·submit_attempts·bandit_arms·strategy_specs 테이블, "
        "2026-08-14 16:20 KST 스냅샷."
    ),
}


def paragraph_text(paragraph) -> str:
    return "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))


def base_rpr(paragraph):
    first_run = paragraph.find(Q("r"))
    if first_run is None:
        return None
    rpr = first_run.find(Q("rPr"))
    return copy.deepcopy(rpr) if rpr is not None else None


def clear_runs(paragraph) -> None:
    ppr = paragraph.find(Q("pPr"))
    for child in list(paragraph):
        if child is not ppr:
            paragraph.remove(child)


def add_text_run(paragraph, text: str, rpr=None) -> None:
    if not text:
        return
    run = etree.SubElement(paragraph, Q("r"))
    if rpr is not None:
        run.append(copy.deepcopy(rpr))
    node = etree.SubElement(run, Q("t"))
    if text[:1].isspace() or text[-1:].isspace():
        node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    node.text = text


def style_reference_run(run) -> None:
    rpr = etree.SubElement(run, Q("rPr"))
    style = etree.SubElement(rpr, Q("rStyle"))
    style.set(Q("val"), "FootnoteReference")
    color = etree.SubElement(rpr, Q("color"))
    color.set(Q("val"), "000000")
    size = etree.SubElement(rpr, Q("sz"))
    size.set(Q("val"), "16")
    size_cs = etree.SubElement(rpr, Q("szCs"))
    size_cs.set(Q("val"), "16")
    valign = etree.SubElement(rpr, Q("vertAlign"))
    valign.set(Q("val"), "superscript")


def add_footnote_reference(paragraph, footnote_id: int) -> None:
    run = etree.SubElement(paragraph, Q("r"))
    style_reference_run(run)
    ref = etree.SubElement(run, Q("footnoteReference"))
    ref.set(Q("id"), str(footnote_id))


def rewrite_with_marker(paragraph, marker: str, footnote_id: int) -> None:
    text = paragraph_text(paragraph)
    if text.count(marker) != 1:
        raise RuntimeError(f"marker count for {marker!r}: {text.count(marker)}")
    before, after = text.split(marker)
    rpr = base_rpr(paragraph)
    clear_runs(paragraph)
    add_text_run(paragraph, before, rpr)
    add_footnote_reference(paragraph, footnote_id)
    add_text_run(paragraph, after, rpr)


def append_marker_before_period(paragraph, footnote_id: int) -> None:
    text = paragraph_text(paragraph)
    rpr = base_rpr(paragraph)
    clear_runs(paragraph)
    if text.endswith("."):
        add_text_run(paragraph, text[:-1], rpr)
        add_footnote_reference(paragraph, footnote_id)
        add_text_run(paragraph, ".", rpr)
    else:
        add_text_run(paragraph, text, rpr)
        add_footnote_reference(paragraph, footnote_id)


def add_footnote(footnotes_root, footnote_id: int, text: str) -> None:
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
    style_reference_run(reference_run)
    etree.SubElement(reference_run, Q("footnoteRef"))

    text_run = etree.SubElement(paragraph, Q("r"))
    rpr = etree.SubElement(text_run, Q("rPr"))
    color = etree.SubElement(rpr, Q("color"))
    color.set(Q("val"), "000000")
    size = etree.SubElement(rpr, Q("sz"))
    size.set(Q("val"), "20")
    size_cs = etree.SubElement(rpr, Q("szCs"))
    size_cs.set(Q("val"), "20")
    node = etree.SubElement(text_run, Q("t"))
    node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    node.text = f" {text}"


def set_paragraph_text(paragraph, text: str) -> None:
    rpr = base_rpr(paragraph)
    clear_runs(paragraph)
    add_text_run(paragraph, text, rpr)


def main() -> None:
    with ZipFile(DOCX, "r") as source:
        entries = {name: source.read(name) for name in source.namelist()}

    document_root = etree.fromstring(entries["word/document.xml"])
    footnotes_root = etree.fromstring(entries["word/footnotes.xml"])
    body = document_root.find(f".//{Q('body')}")
    paragraphs = list(body.findall(Q("p")))

    # Remove the reference heading and every list paragraph before the appendix.
    reference_heading = next(
        paragraph for paragraph in paragraphs if paragraph_text(paragraph) == "10. 참고문헌"
    )
    appendix_heading = next(
        paragraph
        for paragraph in paragraphs
        if paragraph_text(paragraph).startswith("부록.")
    )
    start = paragraphs.index(reference_heading)
    end = paragraphs.index(appendix_heading)
    for paragraph in paragraphs[start:end]:
        body.remove(paragraph)

    # Convert the two existing [8] citations.
    citation_paragraphs = [
        paragraph
        for paragraph in body.xpath(".//w:p", namespaces=NS)
        if "[8]" in paragraph_text(paragraph)
    ]
    if len(citation_paragraphs) != 2:
        raise RuntimeError(f"expected two [8] citations, got {len(citation_paragraphs)}")

    existing_ids = [
        int(value)
        for value in footnotes_root.xpath(
            './w:footnote[number(@w:id) > 0]/@w:id', namespaces=NS
        )
    ]
    next_id = max(existing_ids, default=0) + 1
    new_notes: list[tuple[int, str]] = []
    for paragraph in citation_paragraphs:
        rewrite_with_marker(paragraph, "[8]", next_id)
        new_notes.append((next_id, SOURCES[8]))
        next_id += 1

    # [9] belongs to the high-turnover WARNING explanation.
    high_turnover = next(
        paragraph
        for paragraph in body.xpath(".//w:p", namespaces=NS)
        if paragraph_text(paragraph).startswith("여기에 한 가지 함정이 있었다.")
    )
    append_marker_before_period(high_turnover, next_id)
    new_notes.append((next_id, SOURCES[9]))
    next_id += 1

    # [10] documents the appendix snapshot and aggregation rules.
    appendix_data = next(
        paragraph
        for paragraph in body.xpath(".//w:p", namespaces=NS)
        if paragraph_text(paragraph).startswith("본 보고서의 집계 시각은")
    )
    append_marker_before_period(appendix_data, next_id)
    new_notes.append((next_id, SOURCES[10]))

    for footnote_id, text in new_notes:
        add_footnote(footnotes_root, footnote_id, text)

    # Remove only the period following top-level section numbers.
    for paragraph in body.xpath("./w:p", namespaces=NS):
        text = paragraph_text(paragraph)
        style = paragraph.xpath("./w:pPr/w:pStyle/@w:val", namespaces=NS)
        if style == ["1"] and re.match(r"^[1-9]\.\s+", text):
            set_paragraph_text(paragraph, re.sub(r"^([1-9])\.\s+", r"\1 ", text))
        elif text.startswith("부록. "):
            set_paragraph_text(paragraph, text.replace("부록. ", "부록 ", 1))

    entries["word/document.xml"] = etree.tostring(
        document_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    entries["word/footnotes.xml"] = etree.tostring(
        footnotes_root, xml_declaration=True, encoding="UTF-8", standalone=True
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

    print(
        f"added_footnotes={len(new_notes)} removed_reference_paragraphs={end - start}"
    )


if __name__ == "__main__":
    main()
