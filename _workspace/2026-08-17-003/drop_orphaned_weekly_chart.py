from pathlib import Path

from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT


PPTX = Path(
    "/home/arcosium/projects/GenomicWQB/docs/머신발표/GenomicWQB_머신발표.pptx"
)


def main() -> None:
    presentation = Presentation(PPTX)
    slide = presentation.slides[3]
    slide_xml = slide._element.xml

    for rel in list(slide.part.rels.values()):
        if rel.reltype != RT.IMAGE:
            continue
        if rel.rId not in slide_xml:
            slide.part.drop_rel(rel.rId)

    presentation.save(PPTX)


if __name__ == "__main__":
    main()
