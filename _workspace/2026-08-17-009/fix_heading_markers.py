from copy import deepcopy
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
import os
import re

from lxml import etree


DOCX = Path("/home/arcosium/projects/GenomicWQB/docs/유전알고리즘_알파리서치_리포트.docx")
TMP = DOCX.with_suffix(".docx.tmp")
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}


def qn(local: str) -> str:
    return f"{{{W}}}{local}"


with ZipFile(DOCX) as source:
    document = etree.fromstring(source.read("word/document.xml"))
    styles = etree.fromstring(source.read("word/styles.xml"))

    restored = 0
    for paragraph in document.xpath("//w:body//w:p", namespaces=NS):
        style = paragraph.xpath("./w:pPr/w:pStyle/@w:val", namespaces=NS)
        if not style or style[0] != "1":
            continue

        text_nodes = paragraph.xpath(".//w:t", namespaces=NS)
        if not text_nodes:
            continue
        text = "".join(node.text or "" for node in text_nodes)

        revised = re.sub(r"^([1-9])\s", r"\1. ", text)
        if revised == text and text.startswith("부록 "):
            revised = "부록. " + text[len("부록 ") :]
        if revised == text:
            continue

        # 제목은 한 개 이상의 run으로 나뉠 수 있으므로 첫 텍스트 노드에
        # 전체 제목을 넣고 나머지는 비운다. run 서식은 그대로 보존한다.
        text_nodes[0].text = revised
        for node in text_nodes[1:]:
            node.text = ""
        restored += 1

    removed = 0
    for style_id in ("1", "21"):
        matches = styles.xpath(f'//w:style[@w:styleId="{style_id}"]', namespaces=NS)
        if len(matches) != 1:
            raise RuntimeError(f"heading style {style_id!r} not found uniquely")
        ppr = matches[0].find(qn("pPr"))
        if ppr is None:
            continue
        for tag in ("keepNext", "keepLines"):
            node = ppr.find(qn(tag))
            if node is not None:
                ppr.remove(node)
                removed += 1

    if restored != 10:
        raise RuntimeError(f"expected 10 restored heading labels, got {restored}")
    if removed != 4:
        raise RuntimeError(f"expected 4 pagination markers removed, got {removed}")

    replacements = {
        "word/document.xml": etree.tostring(
            document, xml_declaration=True, encoding="UTF-8", standalone=True
        ),
        "word/styles.xml": etree.tostring(
            styles, xml_declaration=True, encoding="UTF-8", standalone=True
        ),
    }

    with ZipFile(TMP, "w", ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = replacements.get(item.filename, source.read(item.filename))
            clone = deepcopy(item)
            target.writestr(clone, data)

os.replace(TMP, DOCX)
print(f"restored_heading_periods={restored} removed_marker_properties={removed}")
