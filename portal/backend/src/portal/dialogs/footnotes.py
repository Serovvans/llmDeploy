"""Что считается сноской на источник (docs/portal-api.md §5.5).

Сноска — подстрока `[n]` вне кода, где `n` — номер источника без ведущих нулей. Кодом
считаются ограждённые блоки (строки из трёх и более обратных апострофов или тильд;
незакрытый блок длится до конца текста), строки с отступом в четыре пробела или
табуляцией и встроенный код между обратными апострофами. То же правило применяет
интерфейс; на этапе 5 им пользуется экспорт в DOCX.
"""

import re
from collections.abc import Collection

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INDENTED = re.compile(r"^(?: {4}|\t)")
_BACKTICKS = re.compile(r"`+")
_FOOTNOTE = re.compile(r"\[([1-9][0-9]*)\]")


def _outside_code_blocks(text: str) -> str:
    """Текст без ограждённых блоков и строк с отступом; строки остаются на своих местах."""
    kept: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        opened = _FENCE.match(line)
        if fence is not None:
            closes = (
                opened is not None and opened[1][0] == fence[0] and len(opened[1]) >= len(fence)
            )
            if closes and not line.strip().strip(fence[0]):
                fence = None
            kept.append("")
        elif opened is not None:
            fence = opened[1]
            kept.append("")
        elif _INDENTED.match(line) and line.strip():
            kept.append("")
        else:
            kept.append(line)
    return "\n".join(kept)


def _outside_inline_code(text: str) -> str:
    """Текст без встроенного кода: участок между сериями апострофов одной длины."""
    kept: list[str] = []
    position = 0
    while (opening := _BACKTICKS.search(text, position)) is not None:
        closing = next(
            (
                run
                for run in _BACKTICKS.finditer(text, opening.end())
                if len(run[0]) == len(opening[0])
            ),
            None,
        )
        if closing is None:
            # Серия без пары — обычные символы, а не начало кода.
            kept.append(text[position : opening.end()])
            position = opening.end()
            continue
        kept.append(text[position : opening.start()])
        position = closing.end()
    kept.append(text[position:])
    return "".join(kept)


def cited_numbers(text: str, numbers: Collection[int]) -> set[int]:
    """Номера источников из `numbers`, на которые в тексте есть сноска вне кода."""
    prose = _outside_inline_code(_outside_code_blocks(text))
    return {int(match[1]) for match in _FOOTNOTE.finditer(prose)} & set(numbers)
