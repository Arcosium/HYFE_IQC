from pathlib import Path

from docx import Document


DOCX = Path('/home/arcosium/projects/GenomicWQB/docs/유전알고리즘_알파리서치_리포트.docx')


def main():
    doc = Document(DOCX)
    replacements = {
        0: '유전 알고리즘으로 알파 리서치하기',
        40: '5. RQ1 변경 폭과 개선율·기존 제출 풀 유사도의 관계',
        41: '5.1 변경 폭별 개선율',
        52: '5.3 정향변이 지시별 개선율 격차',
        62: '6. RQ2 다목적 선택 뒤에도 제출작 쏠림은 남았다',
        65: '6.2 선택 효과는 분리하기 어렵고 제출작은 쏠렸다',
    }
    for idx, text in replacements.items():
        doc.paragraphs[idx].text = text
    doc.save(DOCX)


if __name__ == '__main__':
    main()
