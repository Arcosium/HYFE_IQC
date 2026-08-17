from pathlib import Path

from docx import Document


DOCX = Path('/home/arcosium/projects/GenomicWQB/docs/유전알고리즘_알파리서치_리포트.docx')


def main():
    doc = Document(DOCX)
    doc.paragraphs[26].text = (
        '유전 알고리즘의 기본 요소는 개체, 유전체, 적합도, 선택, 교차, 변이다[2,3]. '
        '알파 리서치의 언어로 옮기면 개체는 알파 후보 하나, 유전체는 그 알파를 만들어 내는 '
        '설계 변수의 묶음, 적합도는 시뮬레이션이 돌려주는 지표, 선택은 다음 라운드에 시드로 '
        '쓸 엘리트를 고르는 절차, 교차는 두 엘리트의 유전자를 섞는 것, 변이는 유전자 일부를 '
        '바꾸는 것이다.'
    )
    doc.save(DOCX)


if __name__ == '__main__':
    main()
