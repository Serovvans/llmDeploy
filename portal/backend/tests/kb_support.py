"""Помощники тестов базы знаний: стенд воркера на локальном Qdrant и подменных портах."""

import hashlib
import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from llm_stub import replies
from qdrant_client import AsyncQdrantClient

from portal.core.settings import KbSettings, Settings
from portal.files.reader import ContentDocumentReader
from portal.files.storage import DiskFileStorage
from portal.kb.indexing import Indexer
from portal.kb.ports import EmbeddingsUnavailableError, RecognitionFailedError
from portal.kb.qdrant import QdrantVectorIndex
from portal.kb.reindex import Reindexer
from portal.kb.repositories import SqlKbUnitOfWorkFactory
from portal.kb.retrieval import KnowledgeRetriever
from portal.llm.estimator import RatioTokenEstimator
from portal.worker.loop import Worker
from tests.support import Portal


class WordHashEmbedder:
    """Подмена порта `Embedder`: те же векторы, что у заглушки стенда (хеши слов)."""

    def __init__(self) -> None:
        self.batches: list[int] = []
        self.queries: list[str] = []
        self.failures = 0

    def _fail_if_scripted(self) -> None:
        if self.failures:
            self.failures -= 1
            raise EmbeddingsUnavailableError

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self._fail_if_scripted()
        self.batches.append(len(texts))
        return [replies.embedding(text) or [] for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        self._fail_if_scripted()
        self.queries.append(text)
        return replies.embedding(text) or []


class ScriptedRecognizer:
    """Подмена порта `PageRecognizer`: текст по хешу изображения, сбои по сценарию."""

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.failures = 0
        self.text: str | None = None

    async def recognize(self, images: Sequence[bytes]) -> list[str]:
        if self.failures:
            self.failures -= 1
            raise RecognitionFailedError
        self.calls.append(len(images))
        if self.text is not None:
            return [self.text for _ in images]
        return [
            f"Распознанный текст скана {hashlib.sha256(image).hexdigest()[:8]}" for image in images
        ]


@dataclass
class KbBench:
    """Портал, воркер и поиск на одной тестовой базе и одном локальном Qdrant."""

    portal: Portal
    settings: KbSettings
    qdrant: AsyncQdrantClient
    index: QdrantVectorIndex
    uow: SqlKbUnitOfWorkFactory
    embedder: WordHashEmbedder
    recognizer: ScriptedRecognizer
    reader: ContentDocumentReader
    indexer: Indexer
    worker: Worker
    reindexer: Reindexer
    knowledge: KnowledgeRetriever

    async def drain(self) -> int:
        """Выполнить все доступные сейчас задания по одному; вернуть их число."""
        done = 0
        while await self.worker.run_once():
            done += 1
        return done

    async def points(self) -> int:
        """Сколько точек в коллекции."""
        return (await self.qdrant.count(self.settings.qdrant.collection)).count


def kb_settings(settings: Settings, tmp_path: Path, **kb: Any) -> Settings:
    """Настройки теста: отметка воркера во временном каталоге, повторы без пауз."""
    worker = settings.kb.worker.model_copy(
        update={
            "heartbeat_file": tmp_path / "worker.heartbeat",
            "call_retry_pause_seconds": 0,
            **kb.pop("worker", {}),
        }
    )
    return settings.model_copy(
        update={"kb": settings.kb.model_copy(update={"worker": worker, **kb})}
    )


async def make_bench(portal: Portal, settings: Settings) -> KbBench:
    """Собрать воркер и поиск вручную: порты эмбеддингов и распознавания подменены."""
    kb = settings.kb
    qdrant = AsyncQdrantClient(location=":memory:")
    index = QdrantVectorIndex(qdrant, kb)
    await index.ensure_collection()
    uow = SqlKbUnitOfWorkFactory(portal.container.engine, portal.clock)
    embedder = WordHashEmbedder()
    recognizer = ScriptedRecognizer()
    reader = ContentDocumentReader(
        settings.files, settings.llm.image_max_side_px, kb.indexing.document_max_chars + 1
    )
    indexer = Indexer(
        uow, DiskFileStorage(settings.files.root), reader, recognizer, embedder, index, kb, 2
    )
    estimator = RatioTokenEstimator(settings.llm.chars_per_token, settings.llm.tokens_per_image)
    return KbBench(
        portal=portal,
        settings=kb,
        qdrant=qdrant,
        index=index,
        uow=uow,
        embedder=embedder,
        recognizer=recognizer,
        reader=reader,
        indexer=indexer,
        worker=Worker(uow, indexer, index, portal.clock, kb.worker),
        reindexer=Reindexer(uow, index, portal.clock),
        knowledge=KnowledgeRetriever(uow, embedder, index, estimator, kb),
    )


async def add_document(
    client: httpx.AsyncClient,
    name: str,
    content: bytes,
    scope: str = "shared",
    is_cogis: bool = False,
) -> httpx.Response:
    """Загрузить документ в базу знаний."""
    return await client.post(
        "/api/kb/documents",
        files={"file": (name, content, "application/octet-stream")},
        data={"scope": scope, "is_cogis": "true" if is_cogis else "false"},
    )


async def added(
    client: httpx.AsyncClient,
    name: str,
    content: bytes,
    scope: str = "shared",
    is_cogis: bool = False,
) -> str:
    """Загрузить документ и вернуть его идентификатор."""
    response = await add_document(client, name, content, scope, is_cogis)
    assert response.status_code == 201, response.text
    document_id: str = response.json()["id"]
    return document_id


def cyrillic_pdf(pages: Sequence[Sequence[str]], *, to_unicode: bool = True) -> bytes:
    """PDF с русским текстовым слоем: страница — список строк. Строится в памяти.

    Шрифт простой, без встроенной программы; соответствие кодов символам задаёт таблица
    `ToUnicode` — так же, как в PDF, сохранённых из текстовых редакторов. Сторонних
    библиотек не нужно; для показа файл не годится, только для извлечения текста.

    `to_unicode=False` — файл без таблицы соответствия (старые PDF, экспорт из САПР):
    те же коды читаются как посторонние знаки, слой негоден (§1.5).
    """
    chars = sorted({c for page in pages for line in page for c in line})
    assert len(chars) <= 94, "в одном образце помещается не больше 94 разных символов"
    # Коды из верхней половины таблицы: без `ToUnicode` это знаки вроде «¡¢£», не буквы.
    codes = {char: code for code, char in enumerate(chars, 0xA1)}
    mapping = "\n".join(f"<{code:02X}> <{ord(char):04X}>" for char, code in codes.items())
    cmap = (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        "/CMapName /Adobe-Identity-UCS def /CMapType 2 def\n"
        "1 begincodespacerange <00> <FF> endcodespacerange\n"
        f"{len(codes)} beginbfchar\n{mapping}\nendbfchar\n"
        "endcmap CMapName currentdict /CMap defineresource pop end end"
    ).encode()
    kids = " ".join(f"{5 + 2 * index} 0 R" for index in range(len(pages)))
    widths = " ".join(["500"] * 254)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode(),
        (
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /FirstChar 1 /LastChar 254 "
            f"/Widths [{widths}]{' /ToUnicode 4 0 R' if to_unicode else ''} >>"
        ).encode(),
        b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream",
    ]
    for index, lines in enumerate(pages):
        shown = [f"<{''.join(f'{codes[c]:02X}' for c in line)}> Tj T*" for line in lines]
        stream = "\n".join(["BT /F1 11 Tf 14 TL 50 780 Td", *shown, "ET"]).encode()
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                f"/Contents {6 + 2 * index} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
            ).encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    )
    return out.getvalue()
