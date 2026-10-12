"""Чтение документов: тип по содержимому, текст и изображения страниц (docs/portal-api.md §1.5).

PDFium не потокобезопасен (документация pypdfium2: «PDFium is inherently not
thread-safe»), а методы читателя вызываются из пула потоков, поэтому все обращения к
PDF в пределах процесса идут под одной блокировкой (§13.5).
"""

import hashlib
import io
import threading
import zipfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Literal

import docx
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from docx.document import Document as DocxDocument
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table
from PIL import Image, UnidentifiedImageError

from portal.core.settings import FilesSettings
from portal.files.ports import DOCX, DocumentImage, MediaType, UnreadableDocumentError
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
# Режимы растра, которые Pillow записывает в PNG без приведения.
_PNG_MODES = frozenset({"1", "L", "LA", "P", "RGB", "RGBA", "I;16"})
_ImageType = Literal["image/png", "image/jpeg"]
_IMAGE_SIGNATURES: tuple[tuple[bytes, _ImageType], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)
_PDF_JPEG_FILTER = "DCTDecode"
_HASH_CHUNK_BYTES = 1024 * 1024
# Сколько раз подряд картинка PDF с теми же размерами и длиной потока может оказаться
# повтором уже найденной, прежде чем остальные такие считаются повторами без хеширования.
_PDF_REPEAT_CHECKS = 4
# Ссылки на встроенные картинки в разметке DOCX, в порядке документа.
_DOCX_PICTURE_REFS = ".//a:blip/@r:embed"
# Чем Pillow отвечает на негодные и враждебные данные картинки; `ValueError` — например,
# текстовый блок PNG, который распаковывается сверх предела библиотеки.
_IMAGE_ERRORS = (
    UnidentifiedImageError,
    OSError,
    ValueError,
    SyntaxError,
    Image.DecompressionBombError,
)


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
        self,
        settings: FilesSettings,
        render_max_side_px: int,
        text_max_chars: int | None = None,
        max_images: int | None = None,
    ) -> None:
        """Запомнить пределы.

        `render_max_side_px` — длинная сторона изображения страницы (больше модели не
        нужно); `text_max_chars` — сколько символов текста брать с одной страницы или из
        документа без страниц, `None` — без предела; `max_images` — сколько разных
        картинок отдаёт один вызов `embedded_images` (дальше файл не разбирается),
        `None` — без предела.
        """
        self._settings = settings
        self._render_max_side_px = render_max_side_px
        self._text_max_chars = text_max_chars
        self._max_images = max_images

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
                    return self._pdf_layer(pdf_page)
                finally:
                    pdf_page.close()
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
            # JPEG со сканера бывает в CMYK, а такой режим в PNG не записывается.
            writable = image if image.mode in _PNG_MODES else image.convert("RGB")
            writable.save(buffer, format="PNG")
        return buffer.getvalue()

    def embedded_images(
        self, path: Path, media_type: MediaType, page: int, *, min_side_px: int, min_area_px: int
    ) -> list[DocumentImage]:
        """Встроенные растровые картинки страницы по порядку, готовые к хранению (§8.6).

        Картинка ниже порогов или крупнее `files.image_max_pixels` пропускается по
        размеру из заголовка, без декодирования; повреждённая — тоже пропускается.
        Повторы внутри одного вызова (по SHA-256 исходных байтов) сливаются в первую —
        до декодирования: картинка, нарисованная на странице много раз, раскрывается один.
        Разных картинок — не больше `max_images`: остальные не читаются.
        """
        if media_type == "application/pdf":
            return self._pdf_images(path, page, min_side_px, min_area_px)
        if media_type == DOCX:
            return self._docx_images(path, min_side_px, min_area_px)
        if media_type in _IMAGE_FORMATS:
            digest = hashlib.sha256()
            with path.open("rb") as file:
                while chunk := file.read(_HASH_CHUNK_BYTES):
                    digest.update(chunk)
                file.seek(0)
                image = self._stored_image(
                    file, _IMAGE_FORMATS[media_type], digest.digest(), min_side_px, min_area_px
                )
            return [] if image is None else [image]
        return []

    def _pdf_images(
        self, path: Path, page: int, min_side_px: int, min_area_px: int
    ) -> list[DocumentImage]:
        """Картинки страницы PDF; страница-скан картинок не даёт — она распознаётся целиком."""
        images: list[DocumentImage] = []
        seen: set[bytes] = set()
        # Повторы по дешёвому признаку — размеры и длина потока: страница с сотнями тысяч
        # ссылок на одну картинку не хешируется столько же раз.
        repeats: Counter[tuple[int, int, int]] = Counter()
        with _PDF_LOCK, _pdf(path) as document:
            pdf_page = document[page - 1]
            try:
                if self._pdf_layer(pdf_page) is None:
                    return []
                for item in pdf_page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_IMAGE]):
                    if len(images) == self._max_images:
                        break
                    width, height = item.get_px_size()
                    if not self._worth_keeping(width, height, min_side_px, min_area_px):
                        continue
                    # Длина потока без его копирования.
                    look = (width, height, pdfium_raw.FPDFImageObj_GetImageDataRaw(item, None, 0))
                    if repeats[look] >= _PDF_REPEAT_CHECKS:
                        continue
                    sha256 = hashlib.sha256(bytes(item.get_data())).digest()
                    if sha256 in seen:
                        repeats[look] += 1
                        continue
                    seen.add(sha256)
                    jpeg = _PDF_JPEG_FILTER in item.get_filters()
                    with _RASTER_LOCK:
                        bitmap = item.get_bitmap()
                        try:
                            decoded = bitmap.to_pil().copy()
                        finally:
                            # Закрытие безопасно: точки скопированы, а временный образ
                            # Pillow, деливший буфер с растром, уже освобождён. Без
                            # этого pypdfium2 пишет предупреждение на каждую картинку.
                            bitmap.warn_on_close = False
                            bitmap.close()
                        with decoded:
                            images.append(self._encode(decoded, "JPEG" if jpeg else "PNG", sha256))
            finally:
                pdf_page.close()
        return images

    def _docx_images(self, path: Path, min_side_px: int, min_area_px: int) -> list[DocumentImage]:
        """Картинки основного текста DOCX: только части с сигнатурой JPEG или PNG."""
        document = self._docx(path)
        images: list[DocumentImage] = []
        seen_refs: set[str] = set()
        seen: set[bytes] = set()
        for ref in document.element.body.xpath(_DOCX_PICTURE_REFS):
            if len(images) == self._max_images:
                break
            part = document.part.related_parts.get(ref)
            if part is None or ref in seen_refs:
                continue
            seen_refs.add(ref)
            blob: bytes = part.blob
            media_type = next(
                (kind for sign, kind in _IMAGE_SIGNATURES if blob.startswith(sign)), None
            )
            sha256 = hashlib.sha256(blob).digest()
            if media_type is None or sha256 in seen:
                continue
            seen.add(sha256)
            image = self._stored_image(
                io.BytesIO(blob), _IMAGE_FORMATS[media_type], sha256, min_side_px, min_area_px
            )
            if image is not None:
                images.append(image)
        return images

    def _worth_keeping(self, width: int, height: int, min_side_px: int, min_area_px: int) -> bool:
        """Проходит ли картинка пороги размера и предел точек (§8.6)."""
        area = width * height
        return (
            min(width, height) >= min_side_px
            and area >= min_area_px
            and area <= self._settings.image_max_pixels
        )

    def _stored_image(
        self, source: BinaryIO, image_format: str, sha256: bytes, min_side_px: int, min_area_px: int
    ) -> DocumentImage | None:
        """Картинка из файла JPEG или PNG; `None` — не проходит пороги либо не читается.

        Декодер открывается с одним явно названным форматом: байты из чужого документа
        не попадают в остальные декодеры библиотеки. Размер проверяется по заголовку.
        """
        try:
            image = Image.open(source, formats=[image_format])
        except _IMAGE_ERRORS:
            return None
        with image:
            if not self._worth_keeping(image.width, image.height, min_side_px, min_area_px):
                return None
            with _RASTER_LOCK:
                try:
                    image.draft("RGB", (self._render_max_side_px, self._render_max_side_px))
                    image.load()
                except _IMAGE_ERRORS:
                    return None
                return self._encode(image, image_format, sha256)

    def _encode(self, image: Image.Image, image_format: str, sha256: bytes) -> DocumentImage:
        """Уменьшить до `render_max_side_px` по длинной стороне и записать в PNG или JPEG."""
        modes = ("L", "RGB") if image_format == "JPEG" else ("L", "RGB", "RGBA")
        picture = image if image.mode in modes else image.convert("RGB")
        picture.thumbnail((self._render_max_side_px, self._render_max_side_px))
        buffer = io.BytesIO()
        picture.save(buffer, format=image_format)
        return DocumentImage(
            data=buffer.getvalue(),
            media_type="image/jpeg" if image_format == "JPEG" else "image/png",
            width=picture.width,
            height=picture.height,
            sha256=sha256,
        )

    def _pdf_layer(self, pdf_page: pdfium.PdfPage) -> str | None:
        """Годный текстовый слой страницы PDF; `None` — страница считается сканом."""
        text_page = pdf_page.get_textpage()
        try:
            count = text_page.count_chars()
            if self._text_max_chars is not None:
                count = min(count, self._text_max_chars)
            layer = str(text_page.get_text_range(0, count)).strip() if count else ""
        finally:
            text_page.close()
        return layer if self._usable_layer(layer) else None

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
            except _IMAGE_ERRORS as error:
                raise UnreadableDocumentError from error
            try:
                if image.width * image.height > self._settings.image_max_pixels:
                    raise UnreadableDocumentError
                try:
                    image.draft("RGB", (self._render_max_side_px, self._render_max_side_px))
                    image.load()
                except _IMAGE_ERRORS as error:
                    raise UnreadableDocumentError from error
                yield image
            finally:
                image.close()  # растр освобождается до снятия блокировки

    def _docx(self, path: Path) -> DocxDocument:
        """Открытый DOCX; архив сверх пределов распаковки не читается."""
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
            return docx.Document(str(path))
        except (zipfile.BadZipFile, PackageNotFoundError, KeyError, ValueError) as error:
            raise UnreadableDocumentError from error

    def _docx_text(self, path: Path) -> str:
        """Текст абзацев и таблиц по порядку."""
        document = self._docx(path)
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
