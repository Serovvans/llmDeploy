"""Чтение документов: тип по содержимому, текст и изображения страниц (docs/portal-api.md §1.5).

PDFium не потокобезопасен (документация pypdfium2: «PDFium is inherently not
thread-safe»), а методы читателя вызываются из пула потоков, поэтому все обращения к
PDF в пределах процесса идут под одной блокировкой (§13.5).
"""

import io
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import docx
import pypdfium2 as pdfium
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table
from PIL import Image, UnidentifiedImageError

from portal.core.settings import FilesSettings
from portal.files.ports import DOCX, MediaType, UnreadableDocumentError
from portal.files.text import looks_like_text, read_text

_PDF_LOCK = threading.Lock()
_EXPECTED_SIGNS = frozenset("«»№§°")
_RASTER_LOCK = threading.Lock()
_XML_SUFFIXES = (".xml", ".rels")
_SIGNATURES: tuple[tuple[bytes, MediaType], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
)
_TEXT_EXTENSIONS: dict[str, MediaType] = {".txt": "text/plain", ".md": "text/markdown"}
_DOCX_MAIN_PART = "word/document.xml"
_IMAGE_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG"}


def _is_expected(char: str) -> bool:
    """Символ, обычный для русского или английского документа (перечень — §1.5)."""
    code = ord(char)
    return (
        0x20 <= code <= 0x7E  # латинские буквы, цифры и знаки ASCII
        or 0x0400 <= code <= 0x04FF  # кириллица
        or 0x2000 <= code <= 0x206F  # общая пунктуация: тире, кавычки, многоточие
        or char in _EXPECTED_SIGNS
        or char.isspace()
    )


@contextmanager
def _pdf(path: Path) -> Iterator[pdfium.PdfDocument]:
    """Открыть PDF и закрыть его явно, под той же блокировкой.

    Любая ошибка PDFium — при открытии или на отдельной странице — становится
    `UnreadableDocumentError`.

    Объекты PDFium нельзя оставлять сборщику мусора: он освободил бы их в произвольном
    потоке, в обход блокировки.
    """
    try:
        document = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as error:
        raise UnreadableDocumentError from error
    try:
        yield document
    except pdfium.PdfiumError as error:
        # Страница, которую PDFium не может прочитать или отрисовать, — такой же
        # нечитаемый файл: повтор ничего не изменит. Сбой ввода-вывода сюда не попадает.
        raise UnreadableDocumentError from error
    finally:
        document.close()


class ContentDocumentReader:
    """Реализация порта `DocumentReader` на `pypdfium2`, `python-docx` и Pillow.

    Объём памяти не зависит от содержимого файла: растр страницы ограничен стороной
    `render_max_side_px`, текст страницы — `text_max_chars`, распаковка DOCX и размер
    изображения — пределами из конфигурации.
    """

    def __init__(
        self, settings: FilesSettings, render_max_side_px: int, text_max_chars: int | None = None
    ) -> None:
        """Запомнить пределы.

        `render_max_side_px` — длинная сторона изображения страницы (больше модели не
        нужно); `text_max_chars` — сколько символов текста брать с одной страницы или из
        документа без страниц, `None` — без предела.
        """
        self._settings = settings
        self._render_max_side_px = render_max_side_px
        self._text_max_chars = text_max_chars

    def detect(self, path: Path, file_name: str) -> MediaType | None:
        """Тип по содержимому; расширение учитывается только у текстовых файлов."""
        with path.open("rb") as file:
            head = file.read(8)
        for signature, media_type in _SIGNATURES:
            if head.startswith(signature):
                return media_type
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                return DOCX if _DOCX_MAIN_PART in archive.namelist() else None
        text_type = _TEXT_EXTENSIONS.get(Path(file_name).suffix.lower())
        if text_type is not None and looks_like_text(path):
            return text_type
        return None

    def page_count(self, path: Path, media_type: MediaType) -> int | None:
        """Число страниц; заодно проверяется, что файл открывается."""
        if media_type == "application/pdf":
            with _PDF_LOCK, _pdf(path) as document:
                return len(document)
        if media_type in _IMAGE_FORMATS:
            with self._image(path, media_type):
                return 1
        if media_type == DOCX:
            self._docx_text(path)
        return None

    def page_text(self, path: Path, media_type: MediaType, page: int) -> str | None:
        """Текст страницы; `None` — у страницы нет текстового слоя (это скан)."""
        if media_type == "application/pdf":
            with _PDF_LOCK, _pdf(path) as document:
                pdf_page = document[page - 1]
                try:
                    text_page = pdf_page.get_textpage()
                    count = text_page.count_chars()
                    if self._text_max_chars is not None:
                        count = min(count, self._text_max_chars)
                    layer = str(text_page.get_text_range(0, count)).strip() if count else ""
                    text_page.close()
                finally:
                    pdf_page.close()
            return layer if self._usable_layer(layer) else None
        if media_type in _IMAGE_FORMATS:
            return None
        if media_type == DOCX:
            return self._docx_text(path)
        text = read_text(path, self._text_max_chars)
        if text is None:
            raise UnreadableDocumentError
        return text

    def page_image(self, path: Path, media_type: MediaType, page: int) -> bytes:
        """Изображение страницы в PNG, не больше `render_max_side_px` по длинной стороне."""
        if media_type == "application/pdf":
            with _PDF_LOCK, _pdf(path) as document:
                pdf_page = document[page - 1]
                try:
                    bitmap = pdf_page.render(scale=self._render_scale(pdf_page))
                    image = bitmap.to_pil().copy()
                    bitmap.close()
                finally:
                    pdf_page.close()
        elif media_type in _IMAGE_FORMATS:
            with self._image(path, media_type) as full:
                full.thumbnail((self._render_max_side_px, self._render_max_side_px))
                image = full.copy()
        else:
            raise UnreadableDocumentError
        buffer = io.BytesIO()
        with image:
            image.save(buffer, format="PNG")
        return buffer.getvalue()

    def _usable_layer(self, layer: str) -> bool:
        """Годен ли текстовый слой страницы; негодный — страница считается сканом (§1.5).

        Слой негоден, если он короче `text_layer_min_chars` или в нём слишком мало
        ожидаемых символов. Мусор, составленный из обычных букв, так не ловится.
        """
        if len(layer) < self._settings.text_layer_min_chars:
            return False
        expected = sum(1 for char in layer if _is_expected(char))
        return expected / len(layer) >= self._settings.text_layer_min_valid_share

    def _render_scale(self, pdf_page: pdfium.PdfPage) -> float:
        """Масштаб отрисовки: по размеру страницы, а не по одной настройке.

        Страница формата А4 рисуется с `pdf_render_scale`, большой лист — мельче, так
        что длинная сторона растра не превышает `render_max_side_px`. Размер растра
        поэтому не зависит от того, какой формат записан в файле, и всегда меньше
        `files.image_max_pixels`: настройки проверяют это при старте.
        """
        width, height = (float(side) for side in pdf_page.get_size())
        long_side = max(width, height, 1.0)
        return min(self._settings.pdf_render_scale, self._render_max_side_px / long_side)

    @contextmanager
    def _image(self, path: Path, media_type: MediaType) -> Iterator[Image.Image]:
        """Раскрытое изображение; размер проверяется по заголовку, до распаковки точек.

        Блокировка одна на процесс и держится, пока растр существует: сколько бы
        загрузок ни шло одновременно, раскрыт только один. JPEG читается сразу
        уменьшенным.
        """
        with _RASTER_LOCK:
            try:
                image = Image.open(path, formats=[_IMAGE_FORMATS[media_type]])
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
                raise UnreadableDocumentError from error
            try:
                if image.width * image.height > self._settings.image_max_pixels:
                    raise UnreadableDocumentError
                try:
                    image.draft("RGB", (self._render_max_side_px, self._render_max_side_px))
                    image.load()
                except (OSError, Image.DecompressionBombError) as error:
                    raise UnreadableDocumentError from error
                yield image
            finally:
                image.close()  # растр освобождается до снятия блокировки

    def _docx_text(self, path: Path) -> str:
        """Текст абзацев и таблиц по порядку; архив сверх пределов распаковки не читается."""
        try:
            with zipfile.ZipFile(path) as archive:
                parts = archive.infolist()
            unpacked = sum(item.file_size for item in parts)
            markup = sum(item.file_size for item in parts if item.filename.endswith(_XML_SUFFIXES))
            if (
                unpacked > self._settings.docx_max_unpacked_bytes
                or markup > self._settings.docx_max_xml_bytes
            ):
                raise UnreadableDocumentError
            document = docx.Document(str(path))
        except (zipfile.BadZipFile, PackageNotFoundError, KeyError, ValueError) as error:
            raise UnreadableDocumentError from error
        lines: list[str] = []
        length = 0
        for block in document.iter_inner_content():
            if isinstance(block, Table):
                rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in block.rows]
            else:
                rows = [block.text]
            lines.extend(rows)
            length += sum(len(row) + 1 for row in rows)
            if self._text_max_chars is not None and length >= self._text_max_chars:
                break
        return "\n".join(lines).strip()[: self._text_max_chars]
