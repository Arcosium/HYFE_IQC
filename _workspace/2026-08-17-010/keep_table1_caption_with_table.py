from copy import deepcopy
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
import os

from lxml import etree


LINK = Path("/home/arcosium/projects/GenomicWQB/docs/유전알고리즘_알파리서치_리포트.docx")
DOCX = LINK.resolve()
TMP = DOCX.with_suffix(".docx.tmp")
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}


def qn(local: str) -> str:
    return f"{{{W}}}{local}"


with ZipFile(DOCX) as source:
    document = etree.fromstring(source.read("word/document.xml"))
    matches = []
    for paragraph in document.xpath("//w:body//w:p", namespaces=NS):
        text = "".join(paragraph.xpath(".//w:t/text()", namespaces=NS)).strip()
        if text.startswith("표 1. 생성 경로별 탐색 수율"):
            matches.append(paragraph)
    if len(matches) != 1:
        raise RuntimeError(f"expected one Table 1 caption, got {len(matches)}")

    paragraph = matches[0]
    if paragraph.xpath('.//w:br[@w:type="page"]', namespaces=NS):
        raise RuntimeError("Table 1 caption already has a page break")

    run = etree.Element(qn("r"))
    br = etree.SubElement(run, qn("br"))
    br.set(qn("type"), "page")
    ppr = paragraph.find(qn("pPr"))
    insert_at = 1 if ppr is not None else 0
    paragraph.insert(insert_at, run)

    replacement = etree.tostring(
        document, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    with ZipFile(TMP, "w", ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = replacement if item.filename == "word/document.xml" else source.read(item.filename)
            target.writestr(deepcopy(item), data)

os.replace(TMP, DOCX)
print("inserted_page_break_before_table_1_caption=1")
