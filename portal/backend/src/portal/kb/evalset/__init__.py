"""Контрольный набор для оценки поиска: документы и вопросы (docs/portal-design.md §5.2).

Тексты встроенного набора сочинены для проверки поиска и не являются документами
заказчика: юридические (договор, постановление, выписка, акт, регламент) и технические
(PostGIS, плагины, развёртывание). Файлы лежат в пакете, чтобы команда работала из образа.
Набор на документах заказчика (общая база и «Министерства») лежит в каталоге на ВМ в том
же формате и в репозиторий не попадает; личные документы и переписка в набор не входят.
"""

from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

import yaml

from portal.kb.evaluation import KINDS, EvalDocument, EvalQuestion
from portal.kb.ports import DialogTurn

_ROLES = ("user", "assistant")


def load(directory: Path | None = None) -> tuple[list[EvalDocument], list[EvalQuestion]]:
    """Прочитать набор: встроенный либо из каталога с `corpus.yaml` и `questions.yaml`.

    Негодный файл, набор без документов или вопросов, страница не строкой, неизвестный
    вид вопроса или роль в истории, ссылка вопроса на несуществующую страницу и история
    не у уточняющего вопроса — ошибка набора (`ValueError`).
    """
    files: Traversable = directory if directory is not None else resources.files(__package__)
    try:
        corpus = yaml.safe_load(files.joinpath("corpus.yaml").read_text(encoding="utf-8"))
        asked = yaml.safe_load(files.joinpath("questions.yaml").read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError("файл набора не разбирается как YAML") from error
    if not corpus["documents"] or not asked["questions"]:
        raise ValueError("в наборе нет документов или вопросов")
    for item in corpus["documents"]:
        if not all(isinstance(page, str) for page in item["pages"]):
            raise ValueError(f"страница документа — не строка: {item['id']}")
    documents = [
        EvalDocument(item["id"], item["title"], tuple(page.strip() for page in item["pages"]))
        for item in corpus["documents"]
    ]
    questions = [
        EvalQuestion(
            item["text"],
            item["document"],
            tuple(item["pages"]),
            item["kind"],
            tuple(DialogTurn(turn["role"], turn["text"]) for turn in item.get("history", ())),
        )
        for item in asked["questions"]
    ]
    page_counts = {document.id: len(document.pages) for document in documents}
    for question in questions:
        if question.kind not in KINDS:
            raise ValueError(f"неизвестный вид вопроса: {question.text}")
        if any(turn.role not in _ROLES for turn in question.history):
            raise ValueError(f"неизвестная роль в истории вопроса: {question.text}")
        if not all(1 <= page <= page_counts.get(question.document, 0) for page in question.pages):
            raise ValueError(f"вопрос ссылается на несуществующую страницу: {question.text}")
        if (question.kind == "followup") != bool(question.history):
            raise ValueError(f"история нужна уточняющему вопросу и только ему: {question.text}")
    return documents, questions
