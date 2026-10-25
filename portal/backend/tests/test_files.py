"""Хранилище файлов и чтение документов (docs/portal-api.md §1.5, §13.3)."""

import hashlib
import io
import shutil
import threading
import time
import tracemalloc
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import docx
import pytest
from PIL import Image

from portal.core.settings import FilesSettings
from portal.files.ports import (
    DOCX,
    DocxTooLargeError,
    FileTooLargeError,
    UnreadableDocumentError,
)
from portal.files.reader import ContentDocumentReader
from portal.files.storage import DiskFileStorage
from portal.files.text import decode_text, looks_like_text, read_text, store_as_utf8
from tests import samples

pytestmark = pytest.mark.anyio


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def _reader(
    tmp_path: Path,
    *,
    text_max_chars: int | None = None,
    max_side: int = 1568,
    max_images: int | None = None,
    **overrides: float,
) -> ContentDocumentReader:
    values = {
        "text_layer_min_chars": 20,
        "text_layer_min_valid_share": 0.8,
        "image_max_pixels": 10_000_000,
        "docx_max_unpacked_bytes": 1_000_000,
        "docx_max_xml_bytes": 1_000_000,
        "docx_max_media_bytes": 1_000_000,
        "zip_max_directory_bytes": 1_000_000,
        "reader_workers": 1,
    }
    settings = FilesSettings(root=tmp_path, pdf_render_scale=2.0, **{**values, **overrides})
    return ContentDocumentReader(settings, max_side, text_max_chars, max_images)


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

    # JPEG в CMYK (вывод сканеров и допечатной подготовки): в PNG такой режим не пишется.
    cmyk = io.BytesIO()
    Image.new("CMYK", (40, 30)).save(cmyk, "JPEG")
    path.write_bytes(cmyk.getvalue())
    assert reader.page_count(path, "image/jpeg") == 1
    with Image.open(io.BytesIO(reader.page_image(path, "image/jpeg", 1))) as page:
        assert (page.format, page.mode, page.size) == ("PNG", "RGB", (40, 30))

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


@pytest.mark.parametrize("method", [zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2])
def test_docx_part_lying_about_its_size_is_not_unpacked(tmp_path: Path, method: int) -> None:
    """Размер в оглавлении подделан: часть распаковывается не дальше предела разметки.

    Deflate читается с пределом, bzip2 и LZMA отклоняются целиком: `zipfile` распаковывает
    их без предела выхода за один вызов (находка приёмки 0.9).
    """
    with zipfile.ZipFile(io.BytesIO(samples.docx_package(samples.paragraph("x")))) as package:
        entries = {name: package.read(name) for name in package.namelist()}
    entries["_rels/.rels"] = bytes(30_000_000)
    path = tmp_path / "doc"
    path.write_bytes(samples.zip_claiming_size(entries, method, "_rels/.rels", 100))
    assert path.stat().st_size < 100_000
    reader = _reader(tmp_path)
    tracemalloc.start()
    try:
        with pytest.raises(UnreadableDocumentError):
            reader.page_text(path, DOCX, 1)
        with pytest.raises(UnreadableDocumentError):
            reader.embedded_images(path, DOCX, 1, min_side_px=1, min_area_px=1)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 5_000_000  # предел разметки 1 МБ, а не 30 МБ распакованного


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_docx_with_image_not_stored_or_deflated_is_unreadable(tmp_path: Path, method: int) -> None:
    """Картинка, сжатая bzip2 или LZMA, — DOCX нечитаем уже у `page_count` (§1.5, А-120)."""
    source = samples.docx_package(
        samples.paragraph("x"),
        relations={"rId5": "media/a.png"},
        parts={"word/media/a.png": samples.png()},
    )
    path = tmp_path / "doc"
    path.write_bytes(source)
    reader = _reader(tmp_path)
    assert reader.page_count(path, DOCX) is None
    path.write_bytes(samples.zip_with_method(source, "word/media/a.png", method))
    with pytest.raises(UnreadableDocumentError):
        reader.page_count(path, DOCX)


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

        def seek(self, offset: int, whence: int = 0) -> int:
            position: int = self._file.seek(offset, whence)
            return position

        def tell(self) -> int:
            position: int = self._file.tell()
            return position

        def __enter__(self) -> "Counting":
            return self

        def __exit__(self, *args: object) -> None:
            self._file.close()

    monkeypatch.setattr(Path, "open", lambda self, *args, **kwargs: Counting(opened(self, *args)))
    reader = _reader(tmp_path, text_max_chars=1000)
    assert reader.detect(path, "большой.txt") == "text/plain"
    assert reader.page_text(path, "text/plain", 1) == "я" * 1000
    # Запись конца ZIP ищется в последних 64 КБ файла (§1.5), остальное — начало файла.
    assert volume["bytes"] < 200_000


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


def _picture(width: int, height: int, color: str = "red") -> bytes:
    return samples.png(width, height, color)


def test_docx_pictures_are_extracted_in_order_within_limits(tmp_path: Path) -> None:
    """Картинки DOCX: пороги, слияние повторов, уменьшение, только JPEG и PNG (§8.6)."""
    big = _picture(3000, 1500)
    gif = io.BytesIO()
    Image.new("RGB", (300, 300), "blue").save(gif, "GIF")
    source = samples.docx_with_pictures(
        samples.jpeg(400, 300), _picture(50, 400), big, big, gif.getvalue()
    )
    path = tmp_path / "doc"
    path.write_bytes(source)
    reader = _reader(tmp_path, max_side=1000)
    images = reader.embedded_images(path, DOCX, 1, min_side_px=100, min_area_px=20_000)
    # Узкая картинка ниже порога стороны, повтор слит, GIF пропущен по сигнатуре.
    assert [(image.media_type, image.width, image.height) for image in images] == [
        ("image/jpeg", 400, 300),
        ("image/png", 1000, 500),
    ]
    assert images[1].sha256 == hashlib.sha256(big).digest()
    assert _size(images[1].data) == (1000, 500)
    # Картинка крупнее предела точек пропускается без декодирования.
    strict = _reader(tmp_path, image_max_pixels=1_000_000)
    kept = strict.embedded_images(path, DOCX, 1, min_side_px=100, min_area_px=20_000)
    assert [(image.width, image.height) for image in kept] == [(400, 300)]


def test_docx_part_with_foreign_content_is_not_decoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Декодер открывается только для JPEG и PNG: чужие байты в остальные не попадают."""
    opened: list[Any] = []
    real_open = Image.open

    def spy(source: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(kwargs.get("formats"))
        return real_open(source, *args, **kwargs)

    bmp = io.BytesIO()
    Image.new("RGB", (300, 300), "blue").save(bmp, "BMP")
    path = tmp_path / "doc"
    path.write_bytes(samples.docx_with_pictures(bmp.getvalue(), _picture(300, 300)))
    monkeypatch.setattr("portal.files.reader.Image.open", spy)
    reader = _reader(tmp_path, docx_max_unpacked_bytes=10_000_000)
    images = reader.embedded_images(path, DOCX, 1, min_side_px=100, min_area_px=1)
    assert len(images) == 1 and opened == [["PNG"]]


def test_pdf_pictures_come_only_from_pages_with_text_layer(tmp_path: Path) -> None:
    reader = _reader(tmp_path, max_side=200)
    picture = samples.jpeg(400, 300)
    with_text = tmp_path / "text.pdf"
    with_text.write_bytes(samples.pdf_with_picture(picture))
    images = reader.embedded_images(
        with_text, "application/pdf", 1, min_side_px=100, min_area_px=20_000
    )
    assert [(image.media_type, image.width, image.height) for image in images] == [
        ("image/jpeg", 200, 150)
    ]
    assert _size(images[0].data) == (200, 150)
    # Порог не пройден — картинки нет.
    assert (
        reader.embedded_images(with_text, "application/pdf", 1, min_side_px=301, min_area_px=1)
        == []
    )
    # Страница-скан распознаётся целиком и картинок не даёт.
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(samples.scan_pdf(1))
    assert reader.embedded_images(scan, "application/pdf", 1, min_side_px=1, min_area_px=1) == []


def test_repeated_picture_is_decoded_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Повтор отбрасывается по SHA-256 до декодирования: память и время не растут (§8.6)."""
    import pypdfium2 as pdfium

    decoded: list[object] = []
    real_bitmap = pdfium.PdfImage.get_bitmap

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        decoded.append(self)
        return real_bitmap(self, *args, **kwargs)

    monkeypatch.setattr(pdfium.PdfImage, "get_bitmap", spy)
    path = tmp_path / "many.pdf"
    path.write_bytes(samples.pdf_with_picture(samples.jpeg(400, 300), copies=30))
    images = _reader(tmp_path).embedded_images(
        path, "application/pdf", 1, min_side_px=100, min_area_px=1
    )
    assert len(images) == 1 and len(decoded) == 1


def test_many_references_to_one_picture_are_not_hashed_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Работа не растёт с числом ссылок: поток картинки читается считаные разы (§8.6)."""
    import pypdfium2 as pdfium

    read: list[object] = []
    real_data = pdfium.PdfImage.get_data

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        read.append(self)
        return real_data(self, *args, **kwargs)

    monkeypatch.setattr(pdfium.PdfImage, "get_data", spy)
    path = tmp_path / "references.pdf"
    path.write_bytes(samples.pdf_with_picture(samples.jpeg(400, 300), copies=3000))
    images = _reader(tmp_path).embedded_images(
        path, "application/pdf", 1, min_side_px=100, min_area_px=1
    )
    assert len(images) == 1 and len(read) <= 5


def test_reading_pictures_stops_at_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Разных картинок за вызов — не больше предела: остальные не декодируются (§8.6)."""
    opened: list[Any] = []
    real_open = Image.open

    def spy(source: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(source)
        return real_open(source, *args, **kwargs)

    pictures = [_picture(300, 200, color) for color in ("red", "green", "blue", "black", "white")]
    document = tmp_path / "doc"
    document.write_bytes(samples.docx_with_pictures(*pictures))
    monkeypatch.setattr("portal.files.reader.Image.open", spy)
    reader = _reader(tmp_path, max_images=3)
    images = reader.embedded_images(document, DOCX, 1, min_side_px=100, min_area_px=1)
    assert [image.sha256 for image in images] == [
        hashlib.sha256(picture).digest() for picture in pictures[:3]
    ]
    assert len(opened) == 3


def test_picture_that_breaks_the_decoder_is_skipped(tmp_path: Path) -> None:
    """Отказ Pillow на одной картинке не роняет документ; как файл она нечитаема."""
    hostile = samples.png_with_huge_text_chunk()
    with pytest.raises(ValueError, match="Decompressed"), Image.open(io.BytesIO(hostile)):
        pass
    reader = _reader(tmp_path, docx_max_unpacked_bytes=10_000_000)
    document = tmp_path / "doc"
    document.write_bytes(samples.docx_with_pictures(hostile, _picture(300, 200)))
    images = reader.embedded_images(document, DOCX, 1, min_side_px=100, min_area_px=1)
    assert [(image.width, image.height) for image in images] == [(300, 200)]
    path = tmp_path / "image"
    path.write_bytes(hostile)
    assert reader.embedded_images(path, "image/png", 1, min_side_px=100, min_area_px=1) == []
    with pytest.raises(UnreadableDocumentError):
        reader.page_count(path, "image/png")


def test_image_file_is_its_own_single_picture(tmp_path: Path) -> None:
    reader = _reader(tmp_path, max_side=100)
    content = samples.png(400, 200)
    path = tmp_path / "image"
    path.write_bytes(content)
    (image,) = reader.embedded_images(path, "image/png", 1, min_side_px=100, min_area_px=20_000)
    assert (image.media_type, image.width, image.height) == ("image/png", 100, 50)
    assert image.sha256 == hashlib.sha256(content).digest()
    text = tmp_path / "note.txt"
    text.write_text("текст")
    assert reader.embedded_images(text, "text/plain", 1, min_side_px=1, min_area_px=1) == []


async def test_storage_has_area_for_document_pictures(tmp_path: Path) -> None:
    storage = DiskFileStorage(tmp_path)
    stored = await storage.save("kb-images", _chunks(b"png"), 3)
    assert stored.key.startswith("kb-images/") and storage.path(stored.key).read_bytes() == b"png"


def test_known_pictures_are_not_decoded_but_keep_their_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`known`: место и порог — по заголовку, без декодирования; в предел входит (§8.6)."""
    encoded: list[bytes] = []
    real_encode = ContentDocumentReader._encode

    def spy(self: ContentDocumentReader, image: Any, image_format: str, sha256: bytes) -> Any:
        encoded.append(sha256)
        return real_encode(self, image, image_format, sha256)

    monkeypatch.setattr(ContentDocumentReader, "_encode", spy)
    first, small, second, third = (
        _picture(300, 200, "red"),
        _picture(50, 50, "green"),
        _picture(300, 200, "blue"),
        _picture(300, 200, "black"),
    )
    digest = {picture: hashlib.sha256(picture).digest() for picture in (first, small, second)}
    document = tmp_path / "doc"
    document.write_bytes(samples.docx_with_pictures(first, small, second, third))
    reader = _reader(tmp_path, max_images=2)
    known = frozenset({digest[first], digest[small]})
    images = reader.embedded_images(document, DOCX, 1, min_side_px=100, min_area_px=1, known=known)
    # Известная картинка ниже порога по-прежнему отбрасывается; третья не читается.
    assert [(image.data, image.sha256) for image in images[:1]] == [(b"", digest[first])]
    assert [image.sha256 for image in images] == [digest[first], digest[second]]
    assert images[1].data and encoded == [digest[second]]

    pdf = tmp_path / "doc.pdf"
    picture = samples.jpeg(400, 300)
    pdf.write_bytes(samples.pdf_with_picture(picture))
    (plain,) = reader.embedded_images(pdf, "application/pdf", 1, min_side_px=100, min_area_px=1)
    (placeholder,) = reader.embedded_images(
        pdf, "application/pdf", 1, min_side_px=100, min_area_px=1, known=frozenset({plain.sha256})
    )
    assert (placeholder.data, placeholder.sha256, placeholder.media_type) == (
        b"", plain.sha256, "image/jpeg",
    )  # fmt: skip
    image_file = tmp_path / "image"
    image_file.write_bytes(first)
    (own,) = reader.embedded_images(
        image_file, "image/png", 1, min_side_px=100, min_area_px=1, known=known
    )
    assert own.data == b"" and len(encoded) == 2


async def test_storage_reports_free_space_of_its_volume(tmp_path: Path) -> None:
    """`free_bytes` — свободное место тома корня; корня до первой загрузки может не быть."""
    root = tmp_path / "files"
    free = await DiskFileStorage(root).free_bytes()
    assert root.is_dir() and 0 < free <= shutil.disk_usage(tmp_path).total


def _docx_reason(reader: ContentDocumentReader, path: Path) -> str:
    with pytest.raises(DocxTooLargeError) as caught:
        reader.page_count(path, DOCX)
    return caught.value.reason


def test_docx_limits_are_counted_by_the_directory(tmp_path: Path) -> None:
    """Картинки — части под `word/media/`, кроме разметки; у них свой предел (§1.5)."""
    reader = _reader(
        tmp_path,
        docx_max_unpacked_bytes=200_000,
        docx_max_xml_bytes=100_000,
        docx_max_media_bytes=300_000,
    )
    text = samples.paragraph("Текст")
    path = tmp_path / "doc"
    # Картинки в общий предел не входят: 250 КБ при пределе остального 200 КБ.
    path.write_bytes(samples.docx_package(text, parts={"word/media/a.png": bytes(250_000)}))
    assert reader.page_count(path, DOCX) is None
    assert reader.page_text(path, DOCX, 1) == "Текст"
    path.write_bytes(samples.docx_package(text, parts={"word/media/a.png": bytes(350_000)}))
    assert _docx_reason(reader, path) == "images"
    # Разметка под word/media/ — разметка, а не картинки.
    path.write_bytes(samples.docx_package(text, parts={"word/media/a.xml": bytes(150_000)}))
    assert _docx_reason(reader, path) == "markup"
    # Картинка вне word/media/ — «остальное» (известное ограничение).
    path.write_bytes(samples.docx_package(text, parts={"word/pics/a.png": bytes(250_000)}))
    assert _docx_reason(reader, path) == "markup"
    # Оба предела превышены — сначала остальное.
    both = {"word/pics/a.png": bytes(250_000), "word/media/b.png": bytes(350_000)}
    path.write_bytes(samples.docx_package(text, parts=both))
    assert _docx_reason(reader, path) == "markup"
    # Тот же отказ у текста и картинок: пределы проверяются до чтения частей.
    with pytest.raises(DocxTooLargeError):
        reader.page_text(path, DOCX, 1)
    with pytest.raises(DocxTooLargeError):
        reader.embedded_images(path, DOCX, 1, min_side_px=1, min_area_px=1)


def test_docx_main_part_is_found_by_package_relation(tmp_path: Path) -> None:
    """Основная часть — цель связи `officeDocument`, а не имя `word/document.xml`."""
    decoy = {"word/document.xml": b"<x/>"}
    path = tmp_path / "doc"
    path.write_bytes(
        samples.docx_package(samples.paragraph("Основной текст"), main="word/main.xml", parts=decoy)
    )
    reader = _reader(tmp_path)
    assert reader.detect(path, "a.docx") == DOCX
    assert reader.page_text(path, DOCX, 1) == "Основной текст"


def test_broken_docx_media_part_does_not_spoil_text(tmp_path: Path) -> None:
    """Текст не читает частей `word/media/`: неверная контрольная сумма ему не мешает."""
    picture = _picture(300, 200)
    source = samples.docx_package(
        samples.paragraph("Текст") + '<w:p><w:r><a:blip r:embed="rId1"/></w:r></w:p>',
        relations={"rId1": "media/a.png"},
        parts={"word/media/a.png": picture},
        stored=True,
    )
    tail = source.index(picture) + len(picture) - 1
    path = tmp_path / "doc"
    path.write_bytes(source[:tail] + bytes([source[tail] ^ 0xFF]) + source[tail + 1 :])
    reader = _reader(tmp_path)
    assert reader.page_count(path, DOCX) is None
    assert reader.page_text(path, DOCX, 1) == "Текст"
    # Испорченная картинка пропускается, документ не роняет.
    assert reader.embedded_images(path, DOCX, 1, min_side_px=1, min_area_px=1) == []


@pytest.mark.parametrize(
    "parts",
    [
        {"word/document.xml": b"not xml"},
        {"word/document.xml": b"<w:other xmlns:w='urn:x'/>"},
        {"_rels/.rels": b"<Relationships/>"},
    ],
    ids=["not-xml", "foreign-root", "no-office-document"],
)
def test_docx_with_broken_main_part_opens_but_has_no_text(
    tmp_path: Path, parts: dict[str, bytes]
) -> None:
    """`page_count` читает только оглавление; негодную разметку находит `page_text`."""
    path = tmp_path / "doc"
    path.write_bytes(samples.docx_package("", parts=parts))
    reader = _reader(tmp_path)
    assert reader.detect(path, "a.docx") == DOCX
    assert reader.page_count(path, DOCX) is None
    with pytest.raises(UnreadableDocumentError):
        reader.page_text(path, DOCX, 1)
    with pytest.raises(UnreadableDocumentError):
        reader.embedded_images(path, DOCX, 1, min_side_px=1, min_area_px=1)


def test_long_docx_markup_gives_the_whole_text(tmp_path: Path) -> None:
    """Разметка больше прежних 16 МБ читается целиком у читателя без предела текста."""
    line = "абвгдежзий" * 100
    count = 10_000
    source = samples.docx_package("".join(samples.paragraph(line) for _ in range(count)))
    path = tmp_path / "doc"
    path.write_bytes(source)
    with zipfile.ZipFile(path) as archive:
        assert archive.getinfo("word/document.xml").file_size > 16 * 1024 * 1024
    reader = _reader(tmp_path, docx_max_unpacked_bytes=64 << 20, docx_max_xml_bytes=64 << 20)
    assert reader.page_count(path, DOCX) is None
    text = reader.page_text(path, DOCX, 1)
    assert text == "\n".join([line] * count)


def _table(rows: str, columns: int | None) -> str:
    grid = "" if columns is None else "<w:tblGrid>" + "<w:gridCol/>" * columns + "</w:tblGrid>"
    return f"<w:tbl>{grid}{rows}</w:tbl>"


def _cell(text: str = "", *, merge: str | None = None, span: int | None = None) -> str:
    props = ("" if span is None else f'<w:gridSpan w:val="{span}"/>') + (
        ""
        if merge is None
        else "<w:vMerge/>"
        if merge == "continue"
        else f'<w:vMerge w:val="{merge}"/>'
    )
    return f"<w:tc><w:tcPr>{props}</w:tcPr>{samples.paragraph(text) if text else '<w:p/>'}</w:tc>"


def test_docx_merged_table_cells_read_as_python_docx_does(tmp_path: Path) -> None:
    """Объединённые ячейки дают тот же текст, что `_Row.cells` python-docx (§1.5)."""
    document = docx.Document()
    table = document.add_table(rows=4, cols=4)
    for row in range(4):
        for column in range(4):
            table.cell(row, column).text = f" я{row}{column} "
    table.cell(0, 0).merge(table.cell(0, 2))  # по горизонтали
    table.cell(1, 3).merge(table.cell(3, 3))  # по вертикали
    table.cell(2, 0).merge(table.cell(3, 1))  # блок 2×2
    buffer = io.BytesIO()
    document.save(buffer)
    path = tmp_path / "doc"
    path.write_bytes(buffer.getvalue())
    loaded = docx.Document(str(path)).tables[0]
    expected = [" | ".join(cell.text.strip() for cell in row.cells) for row in loaded.rows]
    assert "я13" in expected[3] and "я20" in expected[3]  # объединения действительно есть
    assert _reader(tmp_path).page_text(path, DOCX, 1) == "\n".join(expected)


def test_docx_vertical_merge_over_many_rows_is_read_in_linear_time(tmp_path: Path) -> None:
    """Сплошное вертикальное объединение 900×10: у python-docx — кубическое время (§1.5)."""
    first = "<w:tr>" + _cell("к", merge="restart") * 10 + "</w:tr>"
    rest = ("<w:tr>" + _cell(merge="continue") * 10 + "</w:tr>") * 899
    path = tmp_path / "doc"
    path.write_bytes(samples.docx_package(_table(first + rest, 10)))
    started = time.monotonic()
    text = _reader(tmp_path).page_text(path, DOCX, 1)
    assert time.monotonic() - started < 1
    assert text == "\n".join([" | ".join(["к"] * 10)] * 900)
    # С пределом текста таблица обрывается, не дочитываясь.
    limited = _reader(tmp_path, text_max_chars=50).page_text(path, DOCX, 1)
    assert limited == "\n".join([" | ".join(["к"] * 10)] * 2)[:50]


@pytest.mark.parametrize(("columns", "expected"), [(3, "ш | ш | ш"), (None, "ш")])
def test_docx_cell_span_repeats_no_more_than_grid_columns(
    tmp_path: Path, columns: int | None, expected: str
) -> None:
    """`gridSpan` 10^9 не разворачивается: повтор ячейки — не больше колонок сетки."""
    path = tmp_path / "doc"
    row = "<w:tr>" + _cell("ш", span=10**9) + "</w:tr>"
    path.write_bytes(samples.docx_package(_table(row, columns)))
    started = time.monotonic()
    assert _reader(tmp_path).page_text(path, DOCX, 1) == expected
    assert time.monotonic() - started < 1


def test_docx_wide_merged_cell_stops_at_text_limit(tmp_path: Path) -> None:
    """Длинная ячейка во всю ширину сетки повторяется лишь до предела текста."""
    row = "<w:tr>" + _cell("д" * 1000, span=10_000) + "</w:tr>"
    path = tmp_path / "doc"
    path.write_bytes(samples.docx_package(_table(row, 10_000)))
    text = _reader(tmp_path, text_max_chars=2500).page_text(path, DOCX, 1)
    assert text == " | ".join(["д" * 1000] * 3)[:2500]


@pytest.mark.parametrize(
    "rows",
    [
        "<w:tr>" + _cell(merge="continue") + "</w:tr>",
        "<w:tr>"
        + _cell("а", span=2)
        + "</w:tr><w:tr>"
        + _cell()
        + _cell(merge="continue")
        + "</w:tr>",
    ],
    ids=["first-row", "no-cell-above"],
)
def test_docx_vertical_merge_without_start_is_unreadable(tmp_path: Path, rows: str) -> None:
    """Продолжение объединения без ячейки с тем же смещением строкой выше — как у python-docx."""
    path = tmp_path / "doc"
    path.write_bytes(samples.docx_package(_table(rows, 2)))
    with pytest.raises(UnreadableDocumentError):
        _reader(tmp_path).page_text(path, DOCX, 1)


def test_docx_markup_is_parsed_one_tree_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Дерево разметки — одно на процесс; картинки декодируются уже вне блокировки (§1.5)."""
    from portal.files import reader as module

    real_parse = module._parse_markup

    state = {"active": 0, "peak": 0, "calls": 0}
    counter = threading.Lock()

    def parse(data: bytes) -> Any:
        with counter:
            state["active"] += 1
            state["calls"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.01)
        try:
            return real_parse(data)
        finally:
            with counter:
                state["active"] -= 1

    decoded_under_lock: list[bool] = []
    real_encode = ContentDocumentReader._encode

    def encode(self: ContentDocumentReader, image: Any, image_format: str, sha256: bytes) -> Any:
        decoded_under_lock.append(module._DOCX_LOCK.locked())
        return real_encode(self, image, image_format, sha256)

    monkeypatch.setattr(module, "_parse_markup", parse)
    monkeypatch.setattr(ContentDocumentReader, "_encode", encode)
    path = tmp_path / "doc"
    path.write_bytes(samples.docx_with_pictures(_picture(300, 200)))
    reader = _reader(tmp_path)
    assert len(reader.embedded_images(path, DOCX, 1, min_side_px=1, min_area_px=1)) == 1
    assert decoded_under_lock == [False]

    def work() -> None:
        for _ in range(3):
            reader.page_text(path, DOCX, 1)
            reader.embedded_images(path, DOCX, 1, min_side_px=1, min_area_px=1)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert state["peak"] == 1 and state["calls"] > 40


@pytest.mark.parametrize("zip64", [False, True], ids=["eocd", "zip64-eocd"])
def test_zip_with_oversized_directory_is_refused_before_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, zip64: bool
) -> None:
    """Размер оглавления — из записи конца, как его прочтёт `zipfile`; архив не открывается."""
    source = samples.docx_file("Текст")
    reader = _reader(tmp_path, zip_max_directory_bytes=100_000)
    path = tmp_path / "doc"
    path.write_bytes(source)
    assert reader.detect(path, "a.docx") == DOCX  # обычный DOCX проходит
    path.write_bytes(samples.zip_directory_claims(source, 100_001, zip64=zip64))
    opened: list[object] = []
    monkeypatch.setattr(zipfile, "ZipFile", lambda *args, **kwargs: opened.append(args))
    with pytest.raises(UnreadableDocumentError):
        reader.detect(path, "a.docx")
    with pytest.raises(UnreadableDocumentError):
        reader.page_count(path, DOCX)
    with pytest.raises(UnreadableDocumentError):
        reader.page_text(path, DOCX, 1)
    assert opened == []


def test_docx_pictures_from_vml_and_alternate_content(tmp_path: Path) -> None:
    """Картинки VML; DrawingML с запасным VML даёт одну картинку; внешние не читаются (§8.6)."""
    one, two = _picture(300, 200, "red"), _picture(300, 200, "blue")
    body = (
        "<w:p><w:r><mc:AlternateContent>"
        '<mc:Choice Requires="wps"><w:drawing><a:blip r:embed="rId1"/></w:drawing></mc:Choice>'
        '<mc:Fallback><w:pict><v:shape><v:imagedata r:id="rId2"/></v:shape></w:pict>'
        "</mc:Fallback></mc:AlternateContent></w:r></w:p>"
        '<w:p><w:r><w:pict><v:shape><v:imagedata r:id="rId3"/></v:shape></w:pict></w:r></w:p>'
        '<w:p><w:r><w:drawing><a:blip r:embed="rId4"/></w:drawing></w:r></w:p>'
        '<w:p><w:r><w:drawing><a:blip r:embed="rId5"/></w:drawing></w:r></w:p>'
    )
    relations = {
        "rId1": "media/one.png",
        "rId2": "media/one.png",
        "rId3": "/word/media/two.png",
        "rId4": "../word/media/copy.png",
        "rId5": "http://example.com/x.png",
    }
    media = {"word/media/one.png": one, "word/media/two.png": two, "word/media/copy.png": one}
    path = tmp_path / "doc"
    path.write_bytes(samples.docx_package(body, relations=relations, parts=media))
    images = _reader(tmp_path).embedded_images(path, DOCX, 1, min_side_px=1, min_area_px=1)
    assert [image.sha256 for image in images] == [
        hashlib.sha256(one).digest(),
        hashlib.sha256(two).digest(),
    ]
    # Без файла связей основной части картинок нет.
    path.write_bytes(samples.docx_package(body, parts=media))
    assert _reader(tmp_path).embedded_images(path, DOCX, 1, min_side_px=1, min_area_px=1) == []


def test_many_distinct_picture_references_are_listed_in_linear_time(tmp_path: Path) -> None:
    """100 000 ссылок на разные связи без частей: повтор ищется не по списку (§8.6)."""
    one, two = _picture(300, 200, "red"), _picture(300, 200, "blue")
    count = 100_000
    refs = [f'<a:blip r:embed="rId{index}"/>' for index in range(count)]
    relations = {f"rId{index}": f"media/missing{index}.png" for index in range(count)}
    relations |= {"rIdOne": "media/one.png", "rIdTwo": "media/two.png"}
    body = (
        '<w:p><w:r><a:blip r:embed="rIdTwo"/>'
        + "".join(refs)
        + '<a:blip r:embed="rIdOne"/><a:blip r:embed="rIdTwo"/></w:r></w:p>'
    )
    media = {"word/media/one.png": one, "word/media/two.png": two}
    path = tmp_path / "doc"
    path.write_bytes(samples.docx_package(body, relations=relations, parts=media))
    reader = _reader(tmp_path, docx_max_unpacked_bytes=64 << 20, docx_max_xml_bytes=64 << 20)
    started = time.monotonic()
    images = reader.embedded_images(path, DOCX, 1, min_side_px=1, min_area_px=1)
    assert time.monotonic() - started < 10  # со списком — около 30 с
    assert [image.sha256 for image in images] == [
        hashlib.sha256(two).digest(),
        hashlib.sha256(one).digest(),
    ]


def test_docx_parse_memory_is_returned_before_the_lock_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После каждого разбора разметки память отдаётся системе ещё под блокировкой (§1.5)."""
    from portal.files import reader as module

    trimmed_under_lock: list[bool] = []

    def trim(pad: int) -> int:
        trimmed_under_lock.append(module._DOCX_LOCK.locked())
        return 1

    monkeypatch.setattr(module, "_MALLOC_TRIM", trim)
    reader = _reader(tmp_path)
    path = tmp_path / "doc"
    path.write_bytes(samples.docx_with_pictures(_picture(300, 200)))
    reader.page_text(path, DOCX, 1)
    reader.embedded_images(path, DOCX, 1, min_side_px=1, min_area_px=1)
    path.write_bytes(samples.docx_package("", parts={"word/document.xml": b"not xml"}))
    with pytest.raises(UnreadableDocumentError):
        reader.page_text(path, DOCX, 1)
    assert trimmed_under_lock == [True, True, True]


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _held_trees(error: BaseException | None) -> list[str]:
    """Элементы lxml и крупные байты в кадрах трассировки ошибки и всех её причин."""
    from lxml import etree

    held: list[str] = []
    pending = [error]
    while pending:
        current = pending.pop()
        if current is None:
            continue
        tb = current.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            held += [
                f"{frame.f_code.co_name}.{name}"
                for name, value in frame.f_locals.items()
                if isinstance(value, etree._Element)
                or (isinstance(value, bytes | bytearray) and len(value) >= 1 << 20)
            ]
            tb = tb.tb_next
        pending += [current.__cause__, current.__context__]
    return held


def _broken_markup() -> bytes:
    """Основная часть больше 1 МБ с синтаксической ошибкой в конце."""
    return f"<w:document xmlns:w='{_W}'><w:body>".encode() + b"<w:p/>" * 300_000 + b"<w:p"


@pytest.mark.parametrize("safety_net", [True, False], ids=["cleared", "no-clear"])
@pytest.mark.parametrize(
    ("parts", "calls"),
    [
        (
            {"word/document.xml": f"<w:foo xmlns:w='{_W}'><w:body/></w:foo>".encode()},
            ("page_text", "embedded_images"),
        ),
        (
            {"word/document.xml": f"<w:document xmlns:w='{_W}'><w:p/></w:document>".encode()},
            ("page_text", "embedded_images"),
        ),
        ({"word/_rels/document.xml.rels": b"not xml"}, ("embedded_images",)),
        ({"word/document.xml": _broken_markup()}, ("page_text", "embedded_images")),
    ],
    ids=["foreign-root", "no-body", "broken-relations", "syntax-error"],
)
def test_docx_refusal_does_not_keep_the_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parts: dict[str, bytes],
    calls: tuple[str, ...],
    safety_net: bool,
) -> None:
    """Отказ из-под блокировки разметки не держит дерево в кадрах трассировки (§1.5).

    Иначе `malloc_trim` застаёт дерево живым, и память потока не возвращается системе.
    Без очистки кадров в `_docx_markup` (`no-clear`) дерево не должно попадать в ошибку и так;
    байты части с синтаксической ошибкой держит кадр разбора в трассировке причины — их
    освобождает только очистка по всей цепочке причин.
    """
    import traceback

    if not safety_net:
        monkeypatch.setattr(traceback, "clear_frames", lambda tb: None)
    path = tmp_path / "doc"
    path.write_bytes(samples.docx_package(samples.paragraph("Текст"), parts=parts))
    reader = _reader(tmp_path, docx_max_unpacked_bytes=16 << 20, docx_max_xml_bytes=16 << 20)
    for call in calls:
        with pytest.raises(UnreadableDocumentError) as caught:
            if call == "page_text":
                reader.page_text(path, DOCX, 1)
            else:
                reader.embedded_images(path, DOCX, 1, min_side_px=1, min_area_px=1)
        held = _held_trees(caught.value)
        if safety_net or len(parts.get("word/document.xml", b"")) < 1 << 20:
            assert held == [], call
        else:
            assert held, call  # без очистки причин проверка выше ловит удержание
    if calls == ("embedded_images",):  # связи картинок тексту не нужны
        assert reader.page_text(path, DOCX, 1) == "Текст"


def test_docx_markup_is_parsed_in_a_thread_per_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Разметку разбирает свой поток на документ, и он завершён к возврату (§1.5).

    lxml держит словарь имён на поток всё время его жизни: в потоках пула имена всех
    разобранных документов копились бы. Ошибка из потока доходит до вызывающего.
    """
    from portal.files import reader as module

    real_parse = module._parse_markup
    parsed_in: list[threading.Thread] = []

    def parse(data: bytes) -> Any:
        parsed_in.append(threading.current_thread())
        return real_parse(data)

    monkeypatch.setattr(module, "_parse_markup", parse)
    path = tmp_path / "doc"
    path.write_bytes(samples.docx_with_pictures(_picture(300, 200)))
    reader = _reader(tmp_path)
    reader.page_text(path, DOCX, 1)
    reader.embedded_images(path, DOCX, 1, min_side_px=1, min_area_px=1)
    path.write_bytes(samples.docx_package("", parts={"word/document.xml": b"not xml"}))
    with pytest.raises(UnreadableDocumentError):
        reader.page_text(path, DOCX, 1)
    assert threading.current_thread() not in parsed_in
    assert not any(thread.is_alive() for thread in parsed_in)
    # page_text: связи пакета и тело; embedded_images: ещё связи тела; отказ: связи и тело.
    assert [len({id(t) for t in parsed_in[a:b]}) for a, b in ((0, 2), (2, 5), (5, 7))] == [1] * 3
    assert len({id(thread) for thread in parsed_in}) == 3


def _rss_bytes() -> int:
    """Резидентная память процесса после возврата свободной памяти системе (Linux, glibc)."""
    import ctypes
    import os

    ctypes.CDLL(None).malloc_trim(0)
    pages = int(Path("/proc/self/statm").read_text().split()[1])
    return pages * os.sysconf("SC_PAGE_SIZE")


@pytest.mark.skipif(
    not Path("/proc/self/statm").exists(), reason="RSS и malloc_trim — только Linux с glibc"
)
def test_docx_markup_names_do_not_accumulate(tmp_path: Path) -> None:
    """Документы с уникальными именами элементов не копят память процесса (§1.5).

    Без потока на документ словарь имён lxml вызывающего потока растёт на каждый
    документ: здесь — на ~100 МБ за серию.
    """
    reader = _reader(tmp_path, docx_max_unpacked_bytes=64 << 20, docx_max_xml_bytes=64 << 20)
    path = tmp_path / "doc"

    def read(seed: int) -> None:
        names = "".join(f"<x:e{seed}n{index} x:a{seed}n{index}='1'/>" for index in range(100_000))
        body = f"<w:p xmlns:x='urn:x{seed}'>{names}</w:p>" + samples.paragraph("Текст")
        path.write_bytes(samples.docx_package(body))
        assert reader.page_text(path, DOCX, 1) == "Текст"

    read(0)  # прогрев: библиотеки, потоки, области распределения
    before = _rss_bytes()
    for seed in range(1, 9):
        read(seed)
    assert _rss_bytes() - before < 20 << 20


def test_docx_xpath_limit_means_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Выборка python-docx больше 10 млн узлов (предел XPath libxml2) — файл нечитаем.

    Настоящее тело из 10,5 млн абзацев — это ~2 ГБ памяти на тест; отказ libxml2
    подставлен, проверен он на образе стенда.
    """
    from docx.oxml.document import CT_Body
    from lxml import etree

    def refuse(self: CT_Body) -> list[Any]:
        raise etree.XPathEvalError("Memory allocation failed : growing nodeset hit limit")

    monkeypatch.setattr(CT_Body, "inner_content_elements", property(refuse))
    path = tmp_path / "doc"
    path.write_bytes(samples.docx_file("Текст"))
    with pytest.raises(UnreadableDocumentError):
        _reader(tmp_path).page_text(path, DOCX, 1)
