from pathlib import Path

from docx import Document
from docx.shared import Mm


DOCX = Path('/home/arcosium/projects/GenomicWQB/docs/유전알고리즘_알파리서치_리포트.docx')


def main():
    doc = Document(DOCX)
    first = doc.inline_shapes[0]
    first.width = Mm(105)
    first.height = Mm(51)
    doc.save(DOCX)


if __name__ == '__main__':
    main()
