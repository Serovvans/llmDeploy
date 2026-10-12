"""Разметка блоков данных в запросах к модели (docs/portal-api.md §8.4, §12.1).

Содержимое документов и диалога — недоверенные данные: оно стоит в размеченных блоках и
не может закрыть свою разметку.
"""

import re

# Имена пометок: блок базы знаний в запросе ответа и блоки протоколов 0.15.
KNOWLEDGE = "база_знаний"
FRAGMENT = "фрагмент"
DIALOG = "диалог"
USER = "пользователь"
ASSISTANT = "помощник"
QUESTION = "вопрос"
DOCUMENT = "документ"
SECTION = "раздел"
NEIGHBORS = "соседи"

_TAGS = (KNOWLEDGE, FRAGMENT, DIALOG, USER, ASSISTANT, QUESTION, DOCUMENT, SECTION, NEIGHBORS)
# Закрывающая пометка любого вида в любом написании: с пробелами и в любом регистре.
_CLOSING_TAG = re.compile(rf"<\s*/\s*({'|'.join(_TAGS)})", re.IGNORECASE)


def inert(text: str) -> str:
    """Не дать содержимому закрыть свой блок данных: разметка блока — только наша."""
    return _CLOSING_TAG.sub(r"<\\/\1", text)


def block(tag: str, text: str) -> str:
    """Блок данных на отдельных строках; содержимое обезврежено."""
    return f"<{tag}>\n{inert(text)}\n</{tag}>"


def inline(tag: str, text: str) -> str:
    """Блок данных в одну строку; содержимое обезврежено, переводы строк убраны."""
    return f"<{tag}>{inert(' '.join(text.split()))}</{tag}>"
