"""Контрольный набор для оценки поиска: документы и вопросы (docs/portal-design.md §5.2).

Тексты набора сочинены для проверки поиска и не являются документами заказчика:
юридические (договор, постановление, выписка, акт, регламент) и технические (PostGIS,
плагины, развёртывание). Файлы лежат в пакете, чтобы команда работала из образа.
"""

from importlib import resources

import yaml

from portal.kb.evaluation import EvalDocument, EvalQuestion


def load() -> tuple[list[EvalDocument], list[EvalQuestion]]:
    """Прочитать набор; ссылка вопроса на несуществующую страницу — ошибка набора."""
    files = resources.files(__package__)
    corpus = yaml.safe_load(files.joinpath("corpus.yaml").read_text(encoding="utf-8"))
    asked = yaml.safe_load(files.joinpath("questions.yaml").read_text(encoding="utf-8"))
    documents = [
        EvalDocument(item["id"], item["title"], tuple(page.strip() for page in item["pages"]))
        for item in corpus["documents"]
    ]
    questions = [
        EvalQuestion(item["text"], item["document"], tuple(item["pages"]), item["kind"])
        for item in asked["questions"]
    ]
    page_counts = {document.id: len(document.pages) for document in documents}
    for question in questions:
        if not all(1 <= page <= page_counts.get(question.document, 0) for page in question.pages):
            raise ValueError(f"вопрос ссылается на несуществующую страницу: {question.text}")
    return documents, questions
