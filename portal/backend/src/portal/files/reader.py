"""Чтение документов: тип по содержимому, текст и изображения страниц (docs/portal-api.md §1.5).

PDFium не потокобезопасен (документация pypdfium2: «PDFium is inherently not
thread-safe»), а методы читателя вызываются из пула потоков, поэтому все обращения к
PDF в пределах процесса идут под одной блокировкой (§13.5).

DOCX читается без `docx.Document`, который загружает в память все части пакета вместе
с картинками: по связям пакета берутся только нужные части разметки, картинки — по одной
(§1.5, «Как читается DOCX»). Дерево разметки в несколько раз больше её самой, поэтому
разметка DOCX в процессе разбирается строго по одной — под своей блокировкой, а память
освобождённого дерева возвращается системе до снятия блокировки. Разбирает её отдельный
поток на каждый документ: словарь имён, который lxml держит на поток, уходит вместе с ним.
"""

import ctypes
import hashlib
import io
import posixpath
import threading
import traceback
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Literal, cast

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from docx.exceptions import PythonDocxError
from docx.opc.constants import RELATIONSHIP_TARGET_MODE, RELATIONSHIP_TYPE
from docx.oxml.document import CT_Body, CT_Document
from docx.oxml.ns import qn
from docx.oxml.parser import element_class_lookup
from docx.oxml.simpletypes import ST_Merge
from docx.oxml.table import CT_Tbl, CT_Tc
from docx.oxml.xmlchemy import BaseOxmlElement
from docx.parts.story import StoryPart
from docx.text.paragraph import Paragraph
from lxml import etree
from PIL import Image, UnidentifiedImageError

from portal.core.settings import FilesSettings
from portal.files.ports import (
    DOCX,
    DocumentImage,
    DocxTooLargeError,
    MediaType,
    UnreadableDocumentError,
)
from portal.files.text import looks_like_text, read_text

_PDF_LOCK = threading.Lock()
# Разметка DOCX: одно дерево на процесс (§1.5); под ней других блокировок не берут.
_DOCX_LOCK = threading.Lock()
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
_DOCX_MEDIA = "word/media/"
_PACKAGE_RELS_TAG = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
# Ссылки на картинки основного текста DOCX: DrawingML и VML (документы из `.doc`), §8.6.
_DOCX_PICTURE_REFS = {
    qn("a:blip"): qn("r:embed"),
    "{urn:schemas-microsoft-com:vml}imagedata": qn("r:id"),
}
# Чем `zipfile` отвечает на испорченный архив или часть: неверная контрольная сумма,
# поток сжатия, обрыв, неизвестный способ сжатия, шифрование.
_ZIP_ERRORS = (
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    KeyError,
    NotImplementedError,
    RuntimeError,
    ValueError,
)
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
# Чем Pillow отвечает на негодные и враждебные данные картинки; `ValueError` — например,
# текстовый блок PNG, который распаковывается сверх предела библиотеки.
_IMAGE_ERRORS = (
    UnidentifiedImageError,
    OSError,
    ValueError,
    SyntaxError,
    Image.DecompressionBombError,
)


def _load_malloc_trim() -> Callable[[int], int] | None:
    """`malloc_trim` из glibc; `None` — его нет (musl, macOS): память отдаёт сам распределитель."""
    try:
        trim = ctypes.CDLL(None).malloc_trim
    except (AttributeError, OSError):
        return None
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return trim


_MALLOC_TRIM = _load_malloc_trim()


def _clear_frames(error: BaseException) -> None:
    """Очищает кадры трассировки ошибки и всей цепочки её причин (`__cause__`, `__context__`).

    `traceback.clear_frames` в причины не заходит, а кадр разбора в трассировке причины
    держит дерево или байты части.
    """
    pending, seen = [error], set[int]()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        traceback.clear_frames(current.__traceback__)
        pending += [link for link in (current.__cause__, current.__context__) if link is not None]


def _docx_markup[T](work: Callable[[], T]) -> T:
    """Разбор разметки DOCX: одно дерево на процесс, его память — системе до снятия блокировки.

    glibc не возвращает системе освобождённую память дерева, а держит её в области
    распределения потока; потоки пула разбирают по очереди, но каждый в своей области,
    и удержанное складывается — четыре потока держали бы четыре дерева (§1.5).
    `work` выполняется в своём потоке, который завершается до возврата памяти: lxml держит
    словарь имён элементов и атрибутов на поток всё время его жизни, и в потоках пула
    имена из всех разобранных документов копились бы без предела освобождения.
    Ошибку `work` держат кадры её трассировки, а с ними — дерево: кадры очищаются до
    возврата памяти.
    """
    results: list[T] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(work())
        except BaseException as error:  # передаётся вызывающему ниже
            errors.append(error)

    with _DOCX_LOCK:
        try:
            thread = threading.Thread(target=run, name="docx-markup")
            thread.start()
            thread.join()
            if errors:
                _clear_frames(errors[0])
                raise errors.pop()  # без локального имени: оно замкнуло бы ошибку на кадр
            return results.pop()
        finally:
            if _MALLOC_TRIM is not None:
                _MALLOC_TRIM(0)


def _parse_markup(data: bytes) -> BaseOxmlElement:
    """Разобранная разметка с классами элементов python-docx.

    Парсер — как `docx.oxml.parse_xml`, но свой на каждый разбор: общий парсер python-docx
    держал бы словарь имён последнего потока, разбиравшего им, и после завершения потока.
    """
    parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False)
    parser.set_element_class_lookup(element_class_lookup)
    return cast(BaseOxmlElement, etree.fromstring(data, parser))


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


class _Detached:
    """Родитель абзацев и таблиц, разобранных без пакета: для текста часть пакета не нужна."""

    @property
    def part(self) -> StoryPart:
        """Части пакета нет: обращение к ней — ошибка чтения."""
        raise UnreadableDocumentError


_DETACHED = _Detached()


def _relations_name(part: str) -> str:
    """Имя файла связей части; у пакета (`part` пустой) — `_rels/.rels`."""
    folder, _, name = part.rpartition("/")
    return f"{folder}/_rels/{name}.rels" if folder else f"_rels/{name}.rels"


def _resolve(source: str, target: str) -> str:
    """Имя части в архиве по цели связи — относительно каталога части-источника."""
    joined = posixpath.join("/", posixpath.dirname(source), target)  # "/…" — от корня пакета
    return posixpath.normpath(joined).lstrip("/")


def _docx_body(root: BaseOxmlElement) -> CT_Body | None:
    """Тело основной части DOCX; `None` — другой корень или тела нет, файл нечитаем.

    Ошибка не поднимается отсюда: её трассировка держала бы дерево под блокировкой разметки.
    """
    body: CT_Body | None = root.body if isinstance(root, CT_Document) else None
    return body


def _known_image(image_format: str, width: int, height: int, sha256: bytes) -> DocumentImage:
    """Место уже сохранённой картинки в порядке документа: без данных, значим `sha256`."""
    media_type: _ImageType = "image/jpeg" if image_format == "JPEG" else "image/png"
    return DocumentImage(b"", media_type, width, height, sha256)


def _cell_text(tc: CT_Tc) -> str:
    """Текст ячейки, как `_Cell.text` python-docx: её абзацы через перевод строки."""
    return "\n".join(Paragraph(p, _DETACHED).text for p in tc.p_lst).strip()


def _table_rows(table: CT_Tbl, limit: int | None) -> Iterator[str]:
    """Строки таблицы, ячейки через « | », — тот же текст, что у `_Row.cells` python-docx.

    `_Row.cells` ищет начало вертикального объединения подъёмом по строкам с поиском по
    дереву (на сплошном объединении — время куб от числа строк) и повторяет ячейку по
    `gridSpan` без предела. Здесь начало объединения берётся из предыдущей строки по
    смещению в сетке, а повтор ячейки ограничен числом колонок сетки: время линейно по
    числу ячеек, таблица обрывается, набрав в строке `limit` символов. Ячейка-продолжение без
    ячейки с тем же смещением строкой выше — `ValueError`, как у python-docx.
    """
    grid = table.find(qn("w:tblGrid"))
    columns = max(1, len(grid.findall(qn("w:gridCol")))) if grid is not None else 1
    above: dict[int, tuple[str, int]] = {}  # смещение в сетке -> текст и ширина начала
    for tr in table.tr_lst:
        current: dict[int, tuple[str, int]] = {}
        cells: list[str] = []
        length = 0
        offset = tr.grid_before
        for tc in tr.tc_lst:
            if tc.vMerge == ST_Merge.CONTINUE:
                if offset not in above:
                    raise ValueError("vertical merge without a starting cell")
                text, span = above[offset]
            else:
                text, span = _cell_text(tc), tc.grid_span
            current[offset] = (text, span)
            offset += tc.grid_span
            repeat = min(span, max(1, columns - len(cells)))
            if limit is not None:  # повторы сверх предела всё равно были бы отрезаны
                repeat = min(repeat, max(1, -(-(limit - length) // (len(text) + 3))))
            cells.extend([text] * repeat)
            length += max(repeat, 0) * (len(text) + 3)
            if limit is not None and length >= limit:
                yield " | ".join(cells)
                return  # текста набрано на весь документ
        above = current
        yield " | ".join(cells)


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
        """Тип по содержимому; расширение учитывается только у текстовых файлов.

        ZIP с оглавлением больше `files.zip_max_directory_bytes` — `UnreadableDocumentError`.
        """
        with path.open("rb") as file:
            head = file.read(8)
        for signature, media_type in _SIGNATURES:
            if head.startswith(signature):
                return media_type
        if self._zip_checked(path):
            try:
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
            except _ZIP_ERRORS as error:
                raise UnreadableDocumentError from error
            return DOCX if _DOCX_MAIN_PART in names else None
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
            # Только оглавление архива и пределы: разметку разбирают page_text и
            # embedded_images (§1.5).
            with self._docx_archive(path):
                pass
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
        self,
        path: Path,
        media_type: MediaType,
        page: int,
        *,
        min_side_px: int,
        min_area_px: int,
        known: frozenset[bytes] = frozenset(),
    ) -> list[DocumentImage]:
        """Встроенные растровые картинки страницы по порядку, готовые к хранению (§8.6).

        Картинка ниже порогов или крупнее `files.image_max_pixels` пропускается по
        размеру из заголовка, без декодирования; повреждённая — тоже пропускается.
        Повторы внутри одного вызова (по SHA-256 исходных байтов) сливаются в первую —
        до декодирования: картинка, нарисованная на странице много раз, раскрывается один.
        Разных картинок — не больше `max_images`: остальные не читаются. Картинка из
        `known` не декодируется: на её месте `DocumentImage` с `data = b""` и размером
        из заголовка; в предел `max_images` она входит.
        """
        if media_type == "application/pdf":
            return self._pdf_images(path, page, min_side_px, min_area_px, known)
        if media_type == DOCX:
            return self._docx_images(path, min_side_px, min_area_px, known)
        if media_type in _IMAGE_FORMATS:
            digest = hashlib.sha256()
            with path.open("rb") as file:
                while chunk := file.read(_HASH_CHUNK_BYTES):
                    digest.update(chunk)
                file.seek(0)
                image = self._stored_image(
                    file,
                    _IMAGE_FORMATS[media_type],
                    digest.digest(),
                    min_side_px,
                    min_area_px,
                    known,
                )
            return [] if image is None else [image]
        return []

    def _pdf_images(
        self, path: Path, page: int, min_side_px: int, min_area_px: int, known: frozenset[bytes]
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
                    if sha256 in known:
                        images.append(
                            _known_image("JPEG" if jpeg else "PNG", width, height, sha256)
                        )
                        continue
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

    def _docx_images(
        self, path: Path, min_side_px: int, min_area_px: int, known: frozenset[bytes]
    ) -> list[DocumentImage]:
        """Картинки основного текста DOCX: только части с сигнатурой JPEG или PNG.

        Части картинок читаются по одной и уже вне блокировки разметки.
        """
        images: list[DocumentImage] = []
        seen: set[bytes] = set()
        with self._docx_archive(path) as archive:
            parts = _docx_markup(lambda: self._docx_picture_parts(archive))
            if parts is None:
                raise UnreadableDocumentError
            for name in parts:
                if len(images) == self._max_images:
                    break
                image = self._docx_image(archive, name, seen, min_side_px, min_area_px, known)
                if image is not None:
                    images.append(image)
        return images

    def _docx_image(
        self,
        archive: zipfile.ZipFile,
        name: str,
        seen: set[bytes],
        min_side_px: int,
        min_area_px: int,
        known: frozenset[bytes],
    ) -> DocumentImage | None:
        """Картинка из части архива; `None` — не JPEG и не PNG, повтор, ниже порогов, испорчена."""
        try:
            with archive.open(name) as part:
                head = part.read(8)
                media_type = next(
                    (kind for sign, kind in _IMAGE_SIGNATURES if head.startswith(sign)), None
                )
                if media_type is None:
                    return None
                digest = hashlib.sha256(head)
                while chunk := part.read(_HASH_CHUNK_BYTES):
                    digest.update(chunk)
                sha256 = digest.digest()
                if sha256 in seen:
                    return None
                seen.add(sha256)
                part.seek(0)
                return self._stored_image(
                    part, _IMAGE_FORMATS[media_type], sha256, min_side_px, min_area_px, known
                )
        except _ZIP_ERRORS:
            return None

    def _worth_keeping(self, width: int, height: int, min_side_px: int, min_area_px: int) -> bool:
        """Проходит ли картинка пороги размера и предел точек (§8.6)."""
        area = width * height
        return (
            min(width, height) >= min_side_px
            and area >= min_area_px
            and area <= self._settings.image_max_pixels
        )

    def _stored_image(
        self,
        source: IO[bytes],
        image_format: str,
        sha256: bytes,
        min_side_px: int,
        min_area_px: int,
        known: frozenset[bytes],
    ) -> DocumentImage | None:
        """Картинка из файла JPEG или PNG; `None` — не проходит пороги либо не читается.

        Декодер открывается с одним явно названным форматом: байты из чужого документа
        не попадают в остальные декодеры библиотеки. Размер проверяется по заголовку;
        картинка из `known` дальше заголовка не читается.
        """
        try:
            image = Image.open(source, formats=[image_format])
        except _IMAGE_ERRORS:
            return None
        with image:
            if not self._worth_keeping(image.width, image.height, min_side_px, min_area_px):
                return None
            if sha256 in known:
                return _known_image(image_format, image.width, image.height, sha256)
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

    def _zip_checked(self, path: Path) -> bool:
        """ZIP ли файл; оглавление больше `files.zip_max_directory_bytes` — нечитаемый (§1.5).

        Запись конца оглавления читает та же функция, что затем вызывает
        `zipfile.ZipFile`, и у ZIP64 она берёт размер из записи ZIP64: собственный разбор
        мог бы найти другую запись конца, и проверка обходилась бы (А-77). `zipfile`
        держит в памяти объект на каждую запись оглавления, а не на число из записи конца.
        """
        with path.open("rb") as file:
            try:
                end = zipfile._EndRecData(file)  # type: ignore[attr-defined]
            except zipfile.BadZipFile as error:
                raise UnreadableDocumentError from error
            except OSError:
                return False  # как zipfile.is_zipfile: записи конца нет
        if end is None:
            return False
        size: int = end[zipfile._ECD_SIZE]  # type: ignore[attr-defined]
        if size > self._settings.zip_max_directory_bytes:
            raise UnreadableDocumentError
        return True

    @contextmanager
    def _docx_archive(self, path: Path) -> Iterator[zipfile.ZipFile]:
        """Архив DOCX, прошедший пределы §1.5 по оглавлению, — до чтения любой части.

        Разметка — части `.xml` и `.rels`, где бы они ни лежали; картинки — части под
        `word/media/`, кроме разметки; остальное — всё, кроме картинок.
        """
        if not self._zip_checked(path):
            raise UnreadableDocumentError
        try:
            archive = zipfile.ZipFile(path)
        except _ZIP_ERRORS as error:
            raise UnreadableDocumentError from error
        with archive:
            markup = media = rest = 0
            for item in archive.infolist():
                is_markup = item.filename.endswith(_XML_SUFFIXES)
                markup += item.file_size if is_markup else 0
                if item.filename.startswith(_DOCX_MEDIA) and not is_markup:
                    media += item.file_size
                else:
                    rest += item.file_size
            if (
                rest > self._settings.docx_max_unpacked_bytes
                or markup > self._settings.docx_max_xml_bytes
            ):
                raise DocxTooLargeError("markup")
            if media > self._settings.docx_max_media_bytes:
                raise DocxTooLargeError("images")
            yield archive

    def _docx_xml(self, archive: zipfile.ZipFile, name: str) -> BaseOxmlElement:
        """Разобранная часть разметки; вызывается под блокировкой разметки.

        Часть не `.xml` и не `.rels` не разбирается: её размер не вошёл в предел
        разметки. `ZipFile.read` читает не больше заявленного размера и сверяет
        контрольную сумму; парсер `_parse_markup` (настройки python-docx) не
        разворачивает сущности и не ходит в сеть.
        """
        if not name.endswith(_XML_SUFFIXES):
            raise UnreadableDocumentError
        try:
            return _parse_markup(archive.read(name))
        except (*_ZIP_ERRORS, SyntaxError) as error:
            raise UnreadableDocumentError from error

    def _docx_relations(
        self, archive: zipfile.ZipFile, source: str
    ) -> Iterator[tuple[str, str, str]]:
        """Внутренние связи части (`source` пустой — пакета): идентификатор, тип, имя цели."""
        for relation in self._docx_xml(archive, _relations_name(source)).iter(_PACKAGE_RELS_TAG):
            target = relation.get("Target")
            if target is None or relation.get("TargetMode") == RELATIONSHIP_TARGET_MODE.EXTERNAL:
                continue
            yield relation.get("Id", ""), relation.get("Type", ""), _resolve(source, target)

    def _docx_main_part(self, archive: zipfile.ZipFile) -> str:
        """Основная часть — цель связи `officeDocument` пакета, как у python-docx."""
        for _, kind, name in self._docx_relations(archive, ""):
            if kind == RELATIONSHIP_TYPE.OFFICE_DOCUMENT:
                return name
        raise UnreadableDocumentError

    def _docx_picture_parts(self, archive: zipfile.ZipFile) -> list[str] | None:
        """Части картинок по ссылкам тела основной части, в порядке документа, без повторов.

        `None` — тела нет. Вызывается под блокировкой разметки; деревья освобождаются при
        возврате. Связи разбираются до тела: ошибка в них не застанет тело живым.
        """
        main = self._docx_main_part(archive)
        targets: dict[str, str] = {}
        if _relations_name(main) in archive.NameToInfo:  # нет файла связей — нет и картинок
            targets = {ref: name for ref, _, name in self._docx_relations(archive, main)}
        body = _docx_body(self._docx_xml(archive, main))
        if body is None:
            return None
        if not targets:
            return []
        # Словарь, а не список: проверка повтора — за постоянное время при сотнях тысяч
        # связей, порядок документа сохраняется.
        parts: dict[str, None] = {}
        for element in body.iter(*_DOCX_PICTURE_REFS):
            name = targets.get(element.get(_DOCX_PICTURE_REFS[element.tag], ""))
            if name is not None:
                parts.setdefault(name)
        return list(parts)

    def _docx_text(self, path: Path) -> str:
        """Текст абзацев и таблиц тела по порядку; части `word/media/` не читаются."""
        with self._docx_archive(path) as archive:

            def body_text() -> str | None:
                main = self._docx_main_part(archive)
                # Дерево живёт только внутри вызова и освобождается до снятия блокировки.
                return self._docx_body_text(_docx_body(self._docx_xml(archive, main)))

            text = _docx_markup(body_text)
        if text is None:
            raise UnreadableDocumentError
        return text

    def _docx_body_text(self, body: CT_Body | None) -> str | None:
        """Абзацы и строки таблиц (ячейки через « | ») до `text_max_chars`.

        `None` — тела нет или разметка негодна. Ошибка не поднимается отсюда: её
        трассировка держала бы дерево и после снятия блокировки.
        """
        if body is None:
            return None
        limit = self._text_max_chars
        lines: list[str] = []
        length = 0
        try:
            for element in body.inner_content_elements:
                if isinstance(element, CT_Tbl):
                    rows: Iterator[str] = _table_rows(element, limit)
                else:
                    rows = iter([Paragraph(element, _DETACHED).text])
                for row in rows:
                    lines.append(row)
                    length += len(row) + 1
                    if limit is not None and length >= limit:
                        break
                if limit is not None and length >= limit:
                    break
        except (ValueError, PythonDocxError, etree.Error):
            # Негодные значения атрибутов, вертикальные объединения ячеек без начала и
            # предел XPath libxml2 (больше 10 млн узлов в выборке python-docx).
            return None
        return "\n".join(lines).strip()[:limit]
