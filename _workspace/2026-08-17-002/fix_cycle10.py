from pathlib import Path

from docx import Document


DOCX = Path('/home/arcosium/projects/GenomicWQB/docs/유전알고리즘_알파리서치_리포트.docx')


def main():
    doc = Document(DOCX)
    doc.paragraphs[27].text = (
        '유전 프로그래밍과는 구분된다[2]. 유전 프로그래밍은 임의의 구문 트리를 제한 없이 '
        '진화시키지만 GenomicWQB는 그렇게 하지 않는다. 문법과 연구 관행이 반영된 고정 '
        '유전자 공간에서 수식을 렌더링한다. 표현 자유도를 일부 포기하는 대신 문법 오류와 '
        '과도한 복잡도를 줄이는 선택이며, 실제로 35,701건을 돌리는 동안 문법 오류로 중단된 '
        '시뮬레이션은 없었다.'
    )
    doc.save(DOCX)


if __name__ == '__main__':
    main()
