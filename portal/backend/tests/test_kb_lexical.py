"""Токены и разреженный вектор лексической части поиска (docs/portal-api.md §10.5)."""

import math
import zlib

import pytest

from portal.kb.lexical import sparse_vector, tokenize


def _indices(text: str) -> set[int]:
    return set(sparse_vector(text).indices)


def _shared(left: str, right: str) -> int:
    return len(_indices(left) & _indices(right))


# --- шаг 1: приведение текста ---


def test_lowercase_and_yo() -> None:
    assert tokenize("ЕГРН") == ["егрн"]
    assert tokenize("Семёнов ПЁТР") == tokenize("семенов петр")
    assert "елка" in tokenize("Ёлка")


@pytest.mark.parametrize("dash", ["-", "‐", "‑", "‒", "–", "—", "−"])
def test_all_dashes_are_one_sign(dash: str) -> None:
    assert tokenize(f"14{dash}А")[0] == "14-а"


# --- шаг 2: пробелы внутри номера ---


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("14 - А", "14-а"),
        ("14 -А", "14-а"),
        ("14- А", "14-а"),
        ("123 / 2024 - ПП", "123/2024-пп"),
        ("стр. 5 - 7", "5-7"),
        ("№ 7 – С", "7-с"),
    ],
)
def test_spaces_around_number_signs_are_removed(text: str, token: str) -> None:
    assert token in tokenize(text)


def test_spaces_between_plain_words_are_kept() -> None:
    tokens = tokenize("Москва - столица, вход / выход")
    assert "москва-столица" not in tokens and "вход/выход" not in tokens
    assert {"москва", "столица", "вход", "выход"} <= set(tokens)


def test_number_split_by_line_break_is_a_known_limitation() -> None:
    assert "14-а" not in tokenize("14 -\nА")


# --- шаг 3: токены ---


@pytest.mark.parametrize(
    ("text", "whole", "parts"),
    [
        ("участок 77:01:0004012:345.", "77:01:0004012:345", ["77", "01", "0004012", "345"]),
        ("от 18.04.2024 № 123/2024-ПП", "123/2024-пп", ["123", "2024", "пп"]),
        ("дата 05.10.2026,", "05.10.2026", ["05", "10", "2026"]),
        ("доля в праве 1/2", "1/2", ["1", "2"]),
        ("учётный номер 50:21:0030210:418/чзу1", "50:21:0030210:418/чзу1", ["418", "чзу1"]),
    ],
)
def test_compound_numbers_are_kept_whole_and_by_parts(
    text: str, whole: str, parts: list[str]
) -> None:
    tokens = tokenize(text)
    assert whole in tokens
    assert set(parts) <= set(tokens)


def test_sentence_punctuation_is_not_part_of_a_token() -> None:
    assert tokenize("Срок: 49 лет. Плата, 186 400 руб.") == [
        "срок", "49", "лет", "плата", "плат", "186", "400", "руб",
    ]  # fmt: skip


def test_dash_between_word_and_number_glues_them_but_keeps_parts() -> None:
    """Побочное следствие шага 2: «плата - 186» склеивается; отрезки остаются токенами."""
    assert tokenize("Плата - 186 руб.") == ["плата-186", "плата", "186", "плат", "руб"]


def test_neighbouring_numbers_are_different_tokens() -> None:
    whole = zlib.crc32(b"77:01:0004012:345")
    assert whole in _indices("участок 77:01:0004012:345")
    assert whole not in _indices("77:01:0004012:344")
    assert whole not in _indices("77:01:0004012:3450")
    assert _shared("квартал 77:01:0004012", "участок 77:01:0004012:345") >= 3  # по частям


# --- шаг 4: буквы-двойники ---


@pytest.mark.parametrize(
    ("latin", "cyrillic"),
    [
        ("14-A", "14-А"),  # отрезок из двойников в номере с цифрой
        ("T-34", "Т-34"),
        ("123/2024-PP", "123/2024-РР"),
        ("Cоколов", "Соколов"),  # слово, набранное вперемешку
        ("Kузнецoвa", "Кузнецова"),
        ("77AB123", "77АВ123"),  # отрезок с цифрой
        ("формат A4", "формат А4"),
    ],
)
def test_latin_lookalikes_become_cyrillic(latin: str, cyrillic: str) -> None:
    assert tokenize(latin) == tokenize(cyrillic)


@pytest.mark.parametrize(
    "word", ["usb", "ISO", "select", "CoGIS", "COVID-19", "ST_Area", "x", "A-B"]
)
def test_ordinary_latin_words_are_not_changed(word: str) -> None:
    tokens = tokenize(word)
    assert tokens
    assert all(token.isascii() for token in tokens)


def test_latin_words_and_numbers_get_no_stems() -> None:
    assert tokenize("indexes running") == ["indexes", "running"]
    assert tokenize("чзу1 14-а") == ["чзу1", "14-а", "14", "а"]


# --- шаг 5: основы слов ---


def test_surname_stems_as_they_really_are() -> None:
    """Основы Snowball, проверенные прогоном, а не выведенные вручную.

    Женские и косвенные формы сводятся к `соколов`; именительный падеж мужской фамилии
    стеммер режет дальше — до `сокол` (принимает «-ов» за окончание), поэтому совпадение
    с ним держится на общем пространстве токенов: основа вопроса равна точной форме.
    """
    forms = ["соколова", "соколову", "соколовой", "соколовым", "соколове", "соколовых"]
    for form in forms:
        assert tokenize(form) == [form, "соколов"], form
    assert tokenize("Соколов") == ["соколов", "сокол"]
    assert tokenize("Гришин") == ["гришин"]  # основа равна слову — второго токена нет
    assert tokenize("Гришиным") == ["гришиным", "гришин"]
    assert tokenize("Соколовская") == ["соколовская", "соколовск"]


@pytest.mark.parametrize(
    ("question", "document"),
    [
        ("Соколовой", "Соколова"),  # косвенный падеж → именительный женский
        ("Соколова", "Соколовой"),  # и наоборот
        ("Соколову", "Соколов"),  # → именительный мужской: основа вопроса = точная форма
        ("Соколов", "Соколовым"),
        ("Семеновой", "Семёнова"),
        ("Гришина", "Гришин"),
        ("кадастровых инженеров", "кадастровый инженер"),
        ("договора аренды", "договор аренды"),
    ],
)
def test_inflected_question_shares_a_token_with_document(question: str, document: str) -> None:
    assert _shared(question, document) >= 1


def test_exact_form_matches_stronger_than_inflected() -> None:
    """Точная форма совпадает двумя токенами, склонённая — одним: порядок сохраняется."""
    assert _shared("Соколова", "Соколова") == 2
    assert _shared("Соколова", "Соколов") == 1
    assert _shared("Соколов", "Соколов") == 2
    assert _shared("Соколов", "Соколова") == 1
    assert _shared("Соколов", "Соколовская") == 0  # другая фамилия


def test_stem_side_effects_and_limits_are_known() -> None:
    """Что правило основ даёт побочно и чего не закрывает (§10.5, ограничения)."""
    assert _shared("сокол", "Соколов") == 1  # «сокол» — основа мужской фамилии
    assert _shared("Иван", "Иванов") == 1
    assert _shared("Льва", "Лев") == 0  # беглая гласная
    assert _shared("Sokolov", "Соколов") == 0  # транслит


# --- шаг 6: вектор ---


def test_sparse_vector_value_grows_with_frequency_but_saturates() -> None:
    """Значение — `1 + ln(tf)`: с учётом отрезков и основ, без линейного роста."""
    vector = sparse_vector("Иванова и Иванова: участок 14-А")
    values = dict(zip(vector.indices, vector.values, strict=True))
    twice = pytest.approx(1 + math.log(2))
    assert values[zlib.crc32("иванова".encode())] == twice
    assert values[zlib.crc32("иванов".encode())] == twice  # основа — в том же пространстве
    assert values[zlib.crc32("14-а".encode())] == 1.0
    # Значение хеша зафиксировано: от него зависят уже записанные в Qdrant векторы.
    assert values[1784717085] == twice
    many = sparse_vector("77 " * 100)
    assert list(many.values) == [pytest.approx(1 + math.log(100))]
    assert many.values[0] < 6
    # Правило одно для фрагмента и для запроса: функция общая.
    assert sparse_vector("срок срок аренды").values[0] == twice
    assert all(0 <= index < 2**32 for index in vector.indices)


def test_text_without_words_gives_empty_vector() -> None:
    assert sparse_vector("?! … —").indices == ()
