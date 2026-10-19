"""Помощники тестов базы знаний: стенд воркера на локальном Qdrant и подменных портах."""

import asyncio
import hashlib
import io
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from llm_stub import replies
from qdrant_client import AsyncQdrantClient

from portal.core.settings import KbSettings, KbWorkerSettings, Settings
from portal.dialogs.transcripts import SqlAttachmentTranscripts
from portal.files.media import DiskMediaStorage
from portal.files.ports import RecordingType
from portal.files.reader import ContentDocumentReader
from portal.files.storage import DiskFileStorage
from portal.kb.explaining import Contextualizer, FragmentExplainer
from portal.kb.images import ImageCollector
from portal.kb.indexing import Indexer
from portal.kb.ports import (
    EmbeddingsUnavailableError,
    KnowledgeSelection,
    RecognitionFailedError,
    RerankUnavailableError,
)
from portal.kb.qdrant import QdrantVectorIndex
from portal.kb.recordings import RecordingProcessor
from portal.kb.reindex import Reindexer
from portal.kb.repositories import SqlKbUnitOfWorkFactory
from portal.kb.reprocess import Reprocessor
from portal.kb.retrieval import KnowledgeRetriever
from portal.kb.rewrite import QueryRewriter
from portal.llm.estimator import RatioTokenEstimator
from portal.llm.ports import (
    ChatRequest,
    Completion,
    ContentDelta,
    Finished,
    ImagePart,
    ReasoningDelta,
    TextPart,
)
from portal.media.files import stale_recording_keys
from portal.media.ports import (
    AudioInfo,
    AudioToolsError,
    MediaRejectedError,
    RejectionCode,
    SpeechRecognitionError,
    SpeechSegment,
)
from portal.media.stub import StubSpeechRecognizer
from portal.media.transcription import SpeechPool
from portal.worker.loop import Worker
from portal.worker.sweep import RecordingJanitor
from portal.worker.transcribe import AttachmentTranscriber
from tests.support import Portal

# Выбор базы знаний в вопросе: прежние «общая», «общая и личная», документация CoGIS и все три.
SHARED = KnowledgeSelection(frozenset({"shared"}))
SHARED_AND_PERSONAL = KnowledgeSelection(frozenset({"shared", "personal"}))
ALL_COLLECTIONS = KnowledgeSelection(frozenset({"shared", "personal", "ministries"}))
COGIS = KnowledgeSelection(frozenset({"shared"}), cogis_only=True)


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


class ProtocolModel:
    """Подмена портов `ChatModel` и `CompletionModel`: отвечает по правилам заглушки стенда.

    Переписывание — последний вопрос пользователя из истории и новый вопрос; пояснение —
    «Контекстная справка <хеш фрагмента>.»; описание картинки — с особыми ширинами PNG
    701–703 (§12.2). `failures` — сколько ближайших запросов закончатся сбоем `error`;
    `fail_when` — сбоем кончается всякий запрос с этой подстрокой; `gate` — запрос ждёт,
    пока тест его не отпустит; `completions` — ответы с причиной окончания по очереди,
    вместо правил заглушки.
    """

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.failures = 0
        self.fail_when: str | None = None
        self.error: Exception = RuntimeError("model down")
        self.answer: str | None = None
        self.gate: asyncio.Event | None = None
        self.completions: list[Completion] = []
        self.max_tokens: list[int | None] = []

    def stream(
        self, request: ChatRequest
    ) -> AsyncIterator[ReasoningDelta | ContentDelta | Finished]:
        raise NotImplementedError("в базе знаний модель вызывается только без потока")

    async def complete(self, request: ChatRequest) -> str:
        parts = request.messages[-1].parts
        text = "".join(p.text for p in parts if isinstance(p, TextPart))
        images = [p.data for p in parts if isinstance(p, ImagePart)]
        self.prompts.append(text)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_when is not None and self.fail_when in text:
            raise self.error
        if self.failures:
            self.failures -= 1
            raise self.error
        if self.answer is not None:
            return self.answer
        reply = replies.protocol_reply(replies.UserTurn(text, images))
        assert reply is not None, "запрос не по протоколу §12.1"
        return reply

    async def complete_with_reason(self, request: ChatRequest) -> Completion:
        text = await self.complete(request)
        self.max_tokens.append(request.max_tokens)
        if self.completions:
            return self.completions.pop(0)
        images = [p.data for p in request.messages[-1].parts if isinstance(p, ImagePart)]
        if self.answer is not None or len(images) != 1:
            return Completion(text, "stop")
        described, reason = replies.image_description(images[0], request.max_tokens)
        return Completion(described, "length" if reason == "length" else "stop")


class WordOverlapReranker:
    """Подмена порта `Reranker`: оценки заглушки стенда — доля слов запроса в документе.

    `reverse` — оценка растёт с номером документа: реранкер переворачивает порядок.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.failures = 0
        self.reverse = False

    async def scores(self, query: str, documents: Sequence[str]) -> list[float]:
        if self.failures:
            self.failures -= 1
            raise RerankUnavailableError
        self.calls.append((query, list(documents)))
        if self.reverse:
            return [float(position) for position in range(len(documents))]
        ranked = replies.rerank(query, documents)
        by_index = {item["index"]: item["relevance_score"] for item in ranked}
        return [by_index[index] for index in range(len(documents))]


class FakeAudioTools:
    """Подмена порта `AudioTools`: ffmpeg не запускается, итог задаёт тест.

    `duration_ms` — длительность любой записи; `rejection` — отказ проверки; `failures` —
    сколько ближайших извлечений звука кончатся сбоем.
    """

    def __init__(self) -> None:
        self.duration_ms = 70_000
        self.rejection: RejectionCode | None = None
        self.failures = 0
        self.probed: list[tuple[Path, RecordingType]] = []

    async def probe(self, source: Path, media_type: RecordingType) -> AudioInfo:
        assert source.is_file() and list(source.parent.iterdir()) == [source]
        self.probed.append((source, media_type))
        if self.rejection is not None:
            raise MediaRejectedError(self.rejection)
        return AudioInfo(self.duration_ms)

    async def to_wav(self, source: Path, media_type: RecordingType, target: Path) -> None:
        if self.failures:
            self.failures -= 1
            raise AudioToolsError("ffmpeg")
        target.write_bytes(b"RIFF-wav")

    async def to_opus(self, wav: Path, target: Path) -> None:
        target.write_bytes(b"OggS" + bytes(range(96)))


class ScriptedSpeech(StubSpeechRecognizer):
    """Подмена порта `SpeechRecognizer`: текст заглушки стенда, сбои и тишина по сценарию.

    `failures` — сколько ближайших вызовов кончатся сбоем; `fail_from_ms` — сбоем кончается
    всякий участок записи, начинающийся не раньше этого момента.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []
        self.failures = 0
        self.fail_from_ms: int | None = None
        self.silent = False

    def transcribe(
        self, wav: Path, start_ms: int, end_ms: int, threads: int
    ) -> list[SpeechSegment]:
        if self.failures:
            self.failures -= 1
            raise SpeechRecognitionError
        if self.fail_from_ms is not None and start_ms >= self.fail_from_ms:
            raise SpeechRecognitionError
        self.calls.append((start_ms, end_ms, threads))
        return [] if self.silent else super().transcribe(wav, start_ms, end_ms, threads)


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
    model: ProtocolModel
    reranker: WordOverlapReranker
    reader: ContentDocumentReader
    media: DiskMediaStorage
    audio: FakeAudioTools
    speech: ScriptedSpeech
    transcripts: SqlAttachmentTranscripts
    janitor: RecordingJanitor
    transcriber: AttachmentTranscriber
    indexer: Indexer
    contextualizer: Contextualizer
    worker: Worker
    reindexer: Reindexer
    reprocessor: Reprocessor
    estimator: RatioTokenEstimator
    knowledge: KnowledgeRetriever

    def retriever(
        self, settings: KbSettings | None = None, index: QdrantVectorIndex | None = None
    ) -> KnowledgeRetriever:
        """Поиск стенда с другими настройками или другой коллекцией."""
        settings = settings or self.settings
        return KnowledgeRetriever(
            self.uow,
            self.embedder,
            index or self.index,
            QueryRewriter(self.model, settings.rewrite),
            self.reranker,
            self.estimator,
            settings,
        )

    async def drain(self) -> int:
        """Выполнить все доступные сейчас задания по одному; вернуть их число."""
        done = 0
        while await self.worker.run_once():
            done += 1
        return done

    def make_worker(
        self,
        *,
        contextualizer: Contextualizer | None = None,
        index: QdrantVectorIndex | None = None,
        settings: KbWorkerSettings | None = None,
    ) -> Worker:
        """Воркер стенда с другим этапом контекста, другой коллекцией или настройками."""
        return Worker(
            self.uow,
            self.indexer,
            contextualizer or self.contextualizer,
            self.transcriber,
            self.transcripts,
            self.media,
            self.janitor,
            index or self.index,
            self.portal.clock,
            settings or self.settings.worker,
        )

    def old_files(self, modified_before: datetime) -> list[str]:
        """Файлы записей на диске, которые уборка считает старыми."""
        return stale_recording_keys(self.portal.container.settings.files.root, modified_before)

    async def points(self) -> int:
        """Сколько точек в коллекции."""
        return (await self.qdrant.count(self.settings.qdrant.collection)).count


def kb_settings(settings: Settings, tmp_path: Path, **kb: Any) -> Settings:
    """Настройки теста: отметка воркера во временном каталоге, повторы без пауз.

    Задание `context` по умолчанию выключено: тесты очереди считают задания индексации.
    Тесты пояснений включают его сами (`context={"enabled": True}`).
    """
    worker = settings.kb.worker.model_copy(
        update={
            "heartbeat_file": tmp_path / "worker.heartbeat",
            "call_retry_pause_seconds": 0,
            **kb.pop("worker", {}),
        }
    )
    explained = settings.kb.context.model_copy(update={"enabled": False, **kb.pop("context", {})})
    return settings.model_copy(
        update={"kb": settings.kb.model_copy(update={"worker": worker, "context": explained, **kb})}
    )


async def make_bench(portal: Portal, settings: Settings) -> KbBench:
    """Собрать воркер и поиск вручную: порты эмбеддингов и распознавания подменены."""
    kb = settings.kb
    # Тот же локальный Qdrant, что у приложения: правка тегов из API видна поиску стенда.
    qdrant = portal.container.qdrant
    index = QdrantVectorIndex(qdrant, kb)
    await index.ensure_collection()
    uow = SqlKbUnitOfWorkFactory(portal.container.engine, portal.clock)
    embedder = WordHashEmbedder()
    recognizer = ScriptedRecognizer()
    reader = ContentDocumentReader(
        settings.files, settings.llm.image_max_side_px, kb.indexing.document_max_chars + 1
    )
    storage = DiskFileStorage(settings.files.root)
    media_settings = portal.container.settings.media
    media = DiskMediaStorage(settings.files.root, media_settings)
    audio = FakeAudioTools()
    speech = ScriptedSpeech()
    asr = media_settings.asr
    model = ProtocolModel()
    background_llm = asyncio.Semaphore(kb.worker.background_llm_parallel)
    llm = settings.llm.model_copy(update={"recognition_retry_pause_seconds": 0})
    indexer = Indexer(
        uow,
        storage,
        media,
        reader,
        recognizer,
        ImageCollector(
            uow,
            storage,
            reader,
            model,
            background_llm,
            kb.images,
            llm,
            kb.worker.background_llm_parallel,
        ),
        RecordingProcessor(
            uow, media, audio, SpeechPool(speech, asr.threads - asr.chat_threads), media_settings
        ),
        embedder,
        index,
        portal.clock,
        kb,
        2,
    )
    estimator = RatioTokenEstimator(settings.llm.chars_per_token, settings.llm.tokens_per_image)
    reranker = WordOverlapReranker()
    explainer = FragmentExplainer(model, estimator, kb.context, background_llm)
    contextualizer = Contextualizer(uow, explainer, embedder, index, kb)
    transcripts = SqlAttachmentTranscripts(portal.container.engine)
    janitor = RecordingJanitor(
        uow,
        media,
        transcripts,
        partial(stale_recording_keys, settings.files.root),
        portal.clock,
        media_settings,
    )
    transcriber = AttachmentTranscriber(
        uow,
        transcripts,
        media,
        audio,
        SpeechPool(speech, asr.chat_threads),
        portal.clock,
        media_settings,
    )
    return KbBench(
        portal=portal,
        settings=kb,
        qdrant=qdrant,
        index=index,
        uow=uow,
        embedder=embedder,
        recognizer=recognizer,
        model=model,
        reranker=reranker,
        reader=reader,
        media=media,
        audio=audio,
        speech=speech,
        transcripts=transcripts,
        janitor=janitor,
        transcriber=transcriber,
        indexer=indexer,
        contextualizer=contextualizer,
        worker=Worker(
            uow,
            indexer,
            contextualizer,
            transcriber,
            transcripts,
            media,
            janitor,
            index,
            portal.clock,
            kb.worker,
        ),
        reindexer=Reindexer(uow, index, portal.clock),
        reprocessor=Reprocessor(uow, portal.clock),
        estimator=estimator,
        knowledge=KnowledgeRetriever(
            uow, embedder, index, QueryRewriter(model, kb.rewrite), reranker, estimator, kb
        ),
    )


async def add_document(
    client: httpx.AsyncClient,
    name: str,
    content: bytes,
    scope: str = "shared",
    is_cogis: bool = False,
    tags: Sequence[str] = (),
) -> httpx.Response:
    """Загрузить документ в базу знаний."""
    return await client.post(
        "/api/kb/documents",
        files={"file": (name, content, "application/octet-stream")},
        data={"scope": scope, "is_cogis": "true" if is_cogis else "false", "tags": list(tags)},
    )


async def added(
    client: httpx.AsyncClient,
    name: str,
    content: bytes,
    scope: str = "shared",
    is_cogis: bool = False,
    tags: Sequence[str] = (),
) -> str:
    """Загрузить документ и вернуть его идентификатор."""
    response = await add_document(client, name, content, scope, is_cogis, tags)
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
