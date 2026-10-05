"""Хранилище файлов и чтение документов (docs/portal-api.md §1.5, §13.3)."""

import hashlib
import io
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from portal.core.settings import FilesSettings
from portal.files.ports import DOCX, FileTooLargeError, UnreadableDocumentError
from portal.files.reader import ContentDocumentReader
from portal.files.storage import DiskFileStorage
from portal.files.text import decode_text, looks_like_text, read_text, store_as_utf8
from tests import samples

pytestmark = pytest.mark.anyio


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def _reader(
    tmp_path: Path, *, text_max_chars: int | None = None, max_side: int = 1568, **overrides: float
) -> ContentDocumentReader:
    values = {
        "text_layer_min_chars": 20,
        "text_layer_min_valid_share": 0.8,
        "image_max_pixels": 10_000_000,
        "docx_max_unpacked_bytes": 1_000_000,
        "docx_max_xml_bytes": 1_000_000,
        "reader_workers": 1,
    }
    settings = FilesSettings(root=tmp_path, pdf_render_scale=2.0, **{**values, **overrides})
    return ContentDocumentReader(settings, max_side, text_max_chars)


def _size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as image:
        return image.size


async def test_storage_generates_names_and_hashes_content(tmp_path: Path) -> None:
    storage = DiskFileStorage(tmp_path)
    stored = await storage.save("attachments", _chunks(b"abc", b"def"), 6)
    assert stored.size_bytes == 6 and stored.sha256 == hashlib.sha256(b"abcdef").digest()
    area, name = stored.key.split("/")
    assert area == "attachments" and len(name) == 36
    assert storage.path(stored.key).read_bytes() == b"abcdef"
    await storage.delete(stored.key)
    await storage.delete(stored.key)  # отсутствие файла — не ошибка
    assert not storage.path(stored.key).exists()


async def test_storage_stops_at_the_limit_and_removes_partial_file(tmp_path: Path) -> None:
    storage = DiskFileStorage(tmp_path)
    consumed: list[int] = []

    async def chunks() -> AsyncIterator[bytes]:
        for number in range(10):
            consumed.append(number)
            yield b"x" * 100

    with pytest.raises(FileTooLargeError):
        await storage.save("kb", chunks(), 250)
    assert consumed == [0, 1, 2]  # приём оборван на пределе
    assert list((tmp_path / "kb").iterdir()) == []


@pytest.mark.parametrize(
    "key", ["../etc/passwd", "attachments/../../x", "other/123", "attachments/имя.pdf", ""]
)
def test_storage_rejects_keys_it_did_not_issue(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError, match="ключ"):
        DiskFileStorage(tmp_path).path(key)


def test_reader_separates_text_pages_from_scans(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    path = tmp_path / "file"
    path.write_bytes(samples.text_pdf([samples.TEXT_LAYER, "", "short"]))
    assert reader.detect(path, "x") == "application/pdf"
    assert reader.page_count(path, "application/pdf") == 3
    assert reader.page_text(path, "application/pdf", 1) == samples.TEXT_LAYER
    # Пустая страница и страница с текстом короче порога — сканы.
    assert reader.page_text(path, "application/pdf", 2) is None
    assert reader.page_text(path, "application/pdf", 3) is None
    assert reader.page_image(path, "application/pdf", 2).startswith(b"\x89PNG")


def test_reader_handles_images_docx_and_text(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    path = tmp_path / "file"
    path.write_bytes(samples.jpeg())
    assert reader.detect(path, "a.txt") == "image/jpeg"
    assert reader.page_count(path, "image/jpeg") == 1
    assert reader.page_text(path, "image/jpeg", 1) is None
    assert reader.page_image(path, "image/jpeg", 1).startswith(b"\x89PNG")

    path.write_bytes(samples.docx_file("Первый абзац", "Второй абзац"))
    assert reader.detect(path, "a.pdf") == DOCX
    assert reader.page_count(path, DOCX) is None
    assert reader.page_text(path, DOCX, 1) == "Первый абзац\nВторой абзац"

    path.write_bytes("﻿Текст".encode())
    assert reader.detect(path, "a.txt") == "text/plain"
    assert reader.page_text(path, "text/plain", 1) == "Текст"
    assert decode_text("Текст".encode("cp1251")) == "Текст"
    assert decode_text(b"a\x00b") is None


def test_reader_refuses_bombs_and_broken_files(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(samples.png(2000, 2000))
    with pytest.raises(UnreadableDocumentError):
        _reader(tmp_path, image_max_pixels=1_000_000).page_count(path, "image/png")
    path.write_bytes(samples.docx_file("x" * 5000))
    with pytest.raises(UnreadableDocumentError):
        _reader(tmp_path, docx_max_unpacked_bytes=1000).page_count(path, DOCX)
    path.write_bytes(b"%PDF-1.7 broken")
    with pytest.raises(UnreadableDocumentError):
        _reader(tmp_path).page_count(path, "application/pdf")


def test_pdf_calls_from_many_threads_run_one_at_a_time(tmp_path: Path) -> None:
    """PDFium не потокобезопасен: параллельные вызовы читателя не должны ронять процесс."""
    reader = _reader(tmp_path)
    path = tmp_path / "file"
    path.write_bytes(samples.text_pdf([samples.TEXT_LAYER] * 5))
    results: list[object] = []

    def work(page: int) -> None:
        for _ in range(10):
            results.append(reader.page_text(path, "application/pdf", page))
            results.append(len(reader.page_image(path, "application/pdf", page)) > 0)

    threads = [threading.Thread(target=work, args=(n % 5 + 1,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(samples.TEXT_LAYER) == 80 and results.count(True) == 80


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        ((300, 400), (600, 800)),  # небольшой лист — с масштабом из конфигурации
        ((612, 792), (1212, 1568)),  # А4 при масштабе 2.0 уже упирается в предел стороны
        ((3000, 3000), (1568, 1568)),  # большой лист — по длинной стороне
        ((14400, 14400), (1568, 1568)),  # предельный формат PDF
        ((14400, 200), (1568, 22)),
    ],
)
def test_pdf_page_raster_is_bounded_whatever_the_page_format(
    tmp_path: Path, page: tuple[int, int], expected: tuple[int, int]
) -> None:
    """Размер растра не зависит от формата страницы, записанного в файле."""
    reader = _reader(tmp_path, image_max_pixels=60_000_000)
    path = tmp_path / "file"
    path.write_bytes(samples.text_pdf([""], page_size=page))
    assert len(path.read_bytes()) < 1000
    assert reader.page_text(path, "application/pdf", 1) is None
    width, height = _size(reader.page_image(path, "application/pdf", 1))
    assert abs(width - expected[0]) <= 1 and abs(height - expected[1]) <= 1


def test_image_page_is_downscaled_too(tmp_path: Path) -> None:
    reader = _reader(tmp_path, image_max_pixels=60_000_000, max_side=500)
    path = tmp_path / "file"
    for media_type, content in (
        ("image/png", samples.png(2000, 1000)),
        ("image/jpeg", samples.jpeg(4000, 2000)),
    ):
        path.write_bytes(content)
        assert reader.page_count(path, media_type) == 1  # type: ignore[arg-type]
        assert _size(reader.page_image(path, media_type, 1)) == (500, 250)  # type: ignore[arg-type]


def test_extracted_text_is_cut_at_the_limit(tmp_path: Path) -> None:
    reader = _reader(tmp_path, text_max_chars=30)
    path = tmp_path / "file"
    path.write_bytes(samples.text_pdf(["A" * 500]))
    assert reader.page_text(path, "application/pdf", 1) == "A" * 30
    path.write_bytes(samples.docx_file(*["Б" * 20] * 50))
    text = reader.page_text(path, DOCX, 1)
    assert text is not None and len(text) == 30
    path.write_text("В" * 500, encoding="utf-8")
    assert reader.page_text(path, "text/plain", 1) == "В" * 30


def test_docx_with_oversized_markup_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(samples.docx_file("x" * 50_000))
    assert _reader(tmp_path).page_count(path, DOCX) is None
    with pytest.raises(UnreadableDocumentError):
        _reader(tmp_path, docx_max_xml_bytes=40_000).page_count(path, DOCX)


def test_rasters_are_unpacked_one_at_a_time_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Десять одновременных загрузок больших изображений — один раскрытый растр, не десять.

    Ещё один растр может быть раскрыт при подготовке запроса к модели: там своя
    блокировка на процесс. Итого не больше двух разом (§1.5).
    """
    from PIL import ImageFile

    from portal.llm.bifrost import shrink_image
    from portal.llm.ports import ImagePart

    counter = threading.Lock()
    unpacked: set[int] = set()
    state = {"open": 0, "peak": 0}
    original_load = ImageFile.ImageFile.load
    original_close = Image.Image.close

    def load(self: ImageFile.ImageFile) -> object:
        with counter:
            if id(self) not in unpacked:
                unpacked.add(id(self))
                state["open"] += 1
                state["peak"] = max(state["peak"], state["open"])
        time.sleep(0.02)  # растр «живёт» достаточно долго, чтобы потоки пересеклись
        return original_load(self)

    def close(self: Image.Image) -> None:
        with counter:
            if id(self) in unpacked:
                unpacked.discard(id(self))
                state["open"] -= 1
        original_close(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", load)
    monkeypatch.setattr(Image.Image, "close", close)

    reader = _reader(tmp_path, image_max_pixels=60_000_000)
    path = tmp_path / "file"
    big = samples.png(3000, 2000)
    path.write_bytes(big)

    uploads = [
        threading.Thread(target=reader.page_count, args=(path, "image/png")) for _ in range(10)
    ]
    for thread in uploads:
        thread.start()
    for thread in uploads:
        thread.join()
    assert state == {"open": 0, "peak": 1}

    state["peak"] = 0
    part = ImagePart(big, "image/png")
    mixed = [
        threading.Thread(target=reader.page_image, args=(path, "image/png", 1)) for _ in range(6)
    ] + [threading.Thread(target=shrink_image, args=(part, 1568)) for _ in range(6)]
    for thread in mixed:
        thread.start()
    for thread in mixed:
        thread.join()
    assert state["open"] == 0 and 1 <= state["peak"] <= 2


def test_text_files_are_never_read_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Тип определяется по началу файла, а читается не больше, чем нужно вызывающему."""
    path = tmp_path / "большой.txt"
    path.write_bytes(("я" * 5_000_000).encode())  # 10 МБ

    def forbidden(self: Path) -> bytes:
        raise AssertionError("файл прочитан целиком")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    opened = Path.open
    volume = {"bytes": 0}

    class Counting:
        def __init__(self, file: Any) -> None:
            self._file = file

        def read(self, size: int = -1) -> bytes:
            data: bytes = self._file.read(size)
            volume["bytes"] += len(data)
            return data

        def __enter__(self) -> "Counting":
            return self

        def __exit__(self, *args: object) -> None:
            self._file.close()

    monkeypatch.setattr(Path, "open", lambda self, *args, **kwargs: Counting(opened(self, *args)))
    reader = _reader(tmp_path, text_max_chars=1000)
    assert reader.detect(path, "большой.txt") == "text/plain"
    assert reader.page_text(path, "text/plain", 1) == "я" * 1000
    assert volume["bytes"] < 100_000


def test_text_detection_and_reading_by_parts(tmp_path: Path) -> None:
    path = tmp_path / "file"
    # Многобайтовый символ на границе прочитанной части ошибкой не считается.
    path.write_bytes(b"a" + ("я" * 40_000).encode())
    assert looks_like_text(path)
    assert read_text(path, 10) == "a" + "я" * 9
    assert read_text(path, None) == "a" + "я" * 40_000
    path.write_bytes("Текст в старой кодировке".encode("cp1251"))
    assert looks_like_text(path) and read_text(path, 5) == "Текст"
    path.write_bytes(b"\x89binary\x00data")
    assert not looks_like_text(path) and read_text(path, 5) is None


def test_store_as_utf8_converts_by_parts_only_when_needed(tmp_path: Path) -> None:
    path = tmp_path / "file"
    utf8 = ("Привет " * 1000).encode()
    path.write_bytes(utf8)
    assert store_as_utf8(path) == len(utf8) and path.read_bytes() == utf8
    path.write_bytes(("Привет " * 1000).encode("cp1251"))
    assert store_as_utf8(path) == len(utf8) and path.read_bytes() == utf8
    assert list(tmp_path.iterdir()) == [path]  # временного файла не осталось


@pytest.mark.parametrize("method", ["get_textpage", "render"])
def test_pdfium_failure_on_a_page_means_unreadable_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """Сбой PDFium на отдельной странице — постоянная ошибка файла, а не случайный сбой."""
    import pypdfium2 as pdfium

    reader = _reader(tmp_path)
    path = tmp_path / "file"
    path.write_bytes(samples.text_pdf([samples.TEXT_LAYER, ""]))

    def broken(self: object, *args: object, **kwargs: object) -> None:
        raise pdfium.PdfiumError("Failed to load page.")

    monkeypatch.setattr(pdfium.PdfPage, method, broken)
    call = reader.page_text if method == "get_textpage" else reader.page_image
    with pytest.raises(UnreadableDocumentError):
        call(path, "application/pdf", 1)
    # Файл, пропавший с диска, — другое дело: это сбой ввода-вывода, его можно повторить.
    with pytest.raises(OSError):
        call(tmp_path / "missing", "application/pdf", 1)


GARBAGE = "\u0e01\u0e2a\u0e14\u0e1f\u0e2b\u0e01\u0e14\u0e40\u0e49\u0e48" * 5  # нет ToUnicode


@pytest.mark.parametrize(
    ("layer", "usable"),
    [
        ("Постановление администрации города о предоставлении участка в аренду.", True),
        ("Lease agreement No. 14-A dated 2024-03-01, parcel 77:01:0004012:345", True),
        ("| 1 | 77:01:0004012:345 | 1 250,5 | 12.03.2024 | 49 | 100 % | +/- 3 |", True),
        ("Выписка № 99/2024 — п. 3 §2 ст. 39.6 ЗК РФ; угол 45°, «Участок»…", True),
        ("Площадь 1 250 м², допуск ±0,5 м; S = a × b, 12 м³", True),  # ², ±, ×, ³ — малая доля
        ("Текст\u00a0с неразрывными\tпробелами\nи переводами строк, 2024 г.", True),
        (GARBAGE, False),
        ("\ufffd" * 40, False),
        ("\x01\x02\x03\x04\x05" * 10, False),
        # Ровно на границе: 80 % ожидаемых символов — годен, чуть меньше — нет.
        ("а" * 80 + "\u0e01" * 20, True),
        ("а" * 79 + "\u0e01" * 21, False),
        # Короткая страница решается прежним порогом длины, доля уже не важна.
        ("Стр. 7", False),
        ("а" * 19, False),
        ("а" * 20, True),
    ],
)
def test_text_layer_usability(tmp_path: Path, layer: str, usable: bool) -> None:
    assert _reader(tmp_path)._usable_layer(layer) is usable


def test_page_with_garbage_layer_is_treated_as_a_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDF без таблицы соответствия шрифта: вместо текста мусор — страница идёт как скан."""
    import pypdfium2 as pdfium

    reader = _reader(tmp_path)
    path = tmp_path / "file"
    path.write_bytes(samples.text_pdf([samples.TEXT_LAYER]))
    assert reader.page_text(path, "application/pdf", 1) == samples.TEXT_LAYER
    monkeypatch.setattr(pdfium.PdfTextPage, "count_chars", lambda self: len(GARBAGE))
    monkeypatch.setattr(pdfium.PdfTextPage, "get_text_range", lambda self, *args: GARBAGE)
    assert reader.page_text(path, "application/pdf", 1) is None
    assert reader.page_image(path, "application/pdf", 1).startswith(b"\x89PNG")
