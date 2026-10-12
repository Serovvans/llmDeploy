"""Порты хранилища базы знаний: документы, страницы, фрагменты, очередь заданий.

Методы для маршрутов принимают идентификатор пользователя и включают условие
видимости (docs/portal-api.md §9.15) в сам запрос. Методы воркера пользователя не
знают: воркер обслуживает все документы и ничего пользователю не отдаёт.
"""

from collections.abc import Collection, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from portal.core.pagination import SortOrder
from portal.core.ports import AuditLog
from portal.kb.domain import (
    DocumentStatus,
    DocumentView,
    ErrorCode,
    FoundFragment,
    Fragment,
    FragmentText,
    Job,
    KbDocument,
    KbImage,
    PageText,
    ReprocessStatus,
    Scope,
    SortField,
    TranscriptionJob,
)
from portal.kb.ports import KnowledgeSelection
from portal.kb.tags import Tag


class DocumentRepository(Protocol):
    """Документы, страницы и фрагменты."""

    # --- для маршрутов: условие видимости в каждом запросе ---

    async def add(self, document: KbDocument) -> bool:
        """Создать документ; `False` — такой файл в этой коллекции уже есть."""
        ...

    async def find_duplicate(
        self, user_id: UUID, scope: Scope, sha256: bytes
    ) -> DocumentView | None:
        """Документ с тем же содержимым в этой же коллекции (личная — своя)."""
        ...

    async def get(
        self, user_id: UUID, document_id: UUID, *, lock: bool = False
    ) -> DocumentView | None:
        """Документ, видимый пользователю; `lock` — держать строку до конца транзакции."""
        ...

    async def list_documents(
        self,
        user_id: UUID,
        scope: Scope,
        *,
        q: str | None,
        tags: Sequence[str],
        sort: SortField,
        order: SortOrder,
        offset: int,
        limit: int,
    ) -> tuple[list[DocumentView], int]:
        """Страница документов коллекции и их общее число.

        `tags` — ключи тегов: не пусто — только документы, у которых есть хотя бы один.
        """
        ...

    async def mark_deleted(self, user_id: UUID, document_id: UUID, now: datetime) -> None:
        """Пометить документ удалённым: с этого момента его нет ни в одной выборке."""
        ...

    async def set_tags(self, document_id: UUID, tags: Sequence[Tag]) -> None:
        """Заменить теги документа целиком; право проверяет сценарий под блокировкой строки."""
        ...

    async def suggest_tags(
        self, user_id: UUID, scopes: Collection[Scope], q: str, limit: int
    ) -> list[str]:
        """Теги документов, видимых пользователю (§8.5).

        `scopes` — сузить до коллекций, пусто — все видимые; `q` — подстрока ключа.
        """
        ...

    async def page(self, user_id: UUID, document_id: UUID, number: int) -> PageText | None:
        """Текст страницы видимого документа; у записи — часть расшифровки с отметками."""
        ...

    async def page_count(self, user_id: UUID, document_id: UUID) -> int:
        """Сколько страниц сохранено у видимого документа: у записи — число частей."""
        ...

    async def page_at(self, user_id: UUID, document_id: UUID, timecode_seconds: int) -> int | None:
        """Номер части видимой записи с наибольшим временем начала не позже момента (§8.3)."""
        ...

    async def image(self, user_id: UUID, document_id: UUID, image_id: UUID) -> KbImage | None:
        """Картинка видимого документа; чужая, удалённая и не этого документа — `None`."""
        ...

    async def fragment(
        self, user_id: UUID, document_id: UUID, fragment_id: UUID
    ) -> Fragment | None:
        """Фрагмент видимого документа."""
        ...

    async def found_fragments(
        self, user_id: UUID, selection: KnowledgeSelection, fragment_ids: Sequence[UUID]
    ) -> list[FoundFragment]:
        """Повторная проверка найденного (§8.4): готовые, доступные, из выбранных коллекций.

        Если выбраны теги — только документы, у которых сейчас есть хотя бы один из них.
        """
        ...

    async def neighbor_fragments(
        self, user_id: UUID, selection: KnowledgeSelection, places: Sequence[tuple[UUID, int]]
    ) -> list[FoundFragment]:
        """Соседние фрагменты по местам (документ, порядковый номер) — под той же проверкой.

        Фрагменты-картинки соседями не бывают (§8.4, шаг 7).
        """
        ...

    async def has_cogis_documentation(self) -> bool:
        """Есть ли в общей базе готовый документ с отметкой «документация CoGIS»."""
        ...

    # --- для воркера и команд на ВМ ---

    async def lock(self, document_id: UUID, *, include_deleted: bool = False) -> KbDocument | None:
        """Документ под блокировкой строки; помеченный удалённым — только по запросу."""
        ...

    async def set_status(
        self, document_id: UUID, status: DocumentStatus, error_code: ErrorCode | None = None
    ) -> None:
        """Сменить состояние документа."""
        ...

    async def requeue_abandoned(self, now: datetime) -> int:
        """Вернуть в `queued` документы `processing`, чьё задание никто не держит в аренде.

        Результат — сколько документов затронуто.
        """
        ...

    async def set_recognizing(self, document_id: UUID) -> None:
        """Отметить, что среди страниц есть сканы."""
        ...

    async def tag_keys(self, document_id: UUID) -> list[str]:
        """Ключи тегов документа — поле `tags` его точек в Qdrant."""
        ...

    async def stored_pages(self, document_id: UUID) -> dict[int, int]:
        """Уже прочитанные страницы: номер → длина текста в символах."""
        ...

    async def add_page(self, document_id: UUID, page: PageText) -> None:
        """Сохранить страницу и увеличить счётчик хода; повтор той же страницы — без изменений."""
        ...

    async def set_duration(self, document_id: UUID, seconds: int) -> None:
        """Записать измеренную длительность записи."""
        ...

    async def set_seconds_done(self, document_id: UUID, seconds: int) -> None:
        """Сколько секунд записи уже расшифровано."""
        ...

    async def set_audio_key(self, document_id: UUID, key: str) -> None:
        """Ключ сжатого звука: он же признак завершённой расшифровки (§8.7)."""
        ...

    async def clear_source(self, document_id: UUID, error_code: ErrorCode | None = None) -> None:
        """Отметить, что исходный файл записи удалён; `error_code` — сменить причину ошибки."""
        ...

    async def recordings_to_clean(self, expired_before: datetime) -> list[UUID]:
        """Записи с исходником, который пора убрать (§8.7).

        Готовые — исходник не удалён из-за сбоя; с ошибкой — загружены раньше
        `expired_before`.
        """
        ...

    async def recording_keys(self) -> set[str]:
        """Ключи хранения всех записей: исходники и сжатый звук, включая удаляемые."""
        ...

    async def images(self, document_id: UUID) -> list[KbImage]:
        """Картинки документа по порядку."""
        ...

    async def add_image(self, image: KbImage) -> None:
        """Сохранить строку картинки."""
        ...

    async def describe_image(self, image_id: UUID, description: str) -> None:
        """Записать описание картинки."""
        ...

    async def delete_image(self, image_id: UUID) -> None:
        """Удалить строку картинки; её фрагмент уходит каскадом."""
        ...

    async def set_images_truncated(self, document_id: UUID, truncated: bool) -> None:
        """Отметить, что картинок в документе больше лимита (§8.6)."""
        ...

    async def drop_unreferenced_images(self, document_id: UUID) -> list[str]:
        """Удалить картинки, на которые не ссылается ни один фрагмент; вернуть ключи их файлов."""
        ...

    async def pages(self, document_id: UUID) -> list[PageText]:
        """Все страницы документа по порядку."""
        ...

    async def replace_fragments(self, document_id: UUID, fragments: Sequence[Fragment]) -> None:
        """Удалить прежние фрагменты документа и записать новые."""
        ...

    async def fragment_ids(self, document_id: UUID) -> list[UUID]:
        """Идентификаторы текущих фрагментов документа — они же его действующие точки (§10.6)."""
        ...

    async def mark_indexed(self, document_id: UUID) -> None:
        """Индекс построен текущим порядком обработки; отметка о сбое переобработки снята."""
        ...

    async def set_reprocess_error(self, document_id: UUID, code: ErrorCode | None) -> None:
        """Поставить или снять отметку о сбое переобработки (§11.6)."""
        ...

    async def fragments_without_context(self, document_id: UUID, limit: int) -> list[FragmentText]:
        """Первые по порядку текстовые фрагменты документа без пояснения (§11.5)."""
        ...

    async def fragment_texts(self, document_id: UUID, first: int, last: int) -> list[FragmentText]:
        """Текстовые фрагменты документа с порядковыми номерами от `first` до `last`."""
        ...

    async def set_contexts(self, document_id: UUID, contexts: Mapping[UUID, str]) -> set[UUID]:
        """Записать пояснения; результат — фрагменты, которые ещё существуют."""
        ...

    async def delete(self, document_id: UUID) -> None:
        """Стереть строку документа; страницы, фрагменты и задания уходят каскадом."""
        ...

    async def ids_for_reindex(self, *, only_errors: bool) -> list[UUID]:
        """Неудалённые документы, которые `portal reindex` вернёт в очередь."""
        ...

    async def ids_for_reprocess(self, *, all_ready: bool) -> list[UUID]:
        """Готовые документы для массовой переобработки (§11.6).

        Без `all_ready` — только с прежним `index_version` или с отметкой о сбое.
        """
        ...

    async def reprocess_status(self) -> ReprocessStatus:
        """Числа хода переобработки; названий и содержимого документов в них нет."""
        ...


class JobQueue(Protocol):
    """Очередь заданий в таблице `kb_jobs` (§11)."""

    async def add(self, job: Job) -> None:
        """Поставить задание."""
        ...

    async def reset_or_add(self, job: Job) -> None:
        """Поставить задание, а если оно уже есть — начать счёт попыток заново."""
        ...

    async def add_if_absent(self, job: Job) -> bool:
        """Поставить задание; `False` — такое у документа уже есть и остаётся как есть."""
        ...

    async def add_or_prioritize(self, job: Job) -> None:
        """Поставить задание; у существующего поднять приоритет до приоритета нового (§11.6)."""
        ...

    async def remove(self, document_id: UUID, kind: str) -> None:
        """Снять задание документа."""
        ...

    async def claim(
        self, now: datetime, lease_until: datetime, busy_documents: Collection[UUID] = ()
    ) -> Job | None:
        """Взять в аренду одно доступное задание документа: по приоритету, затем по времени.

        Основное место (§11.1): задания `transcribe` не берутся; при паузе — и задания
        массовой переобработки. Задания `index` и `reprocess` документов из
        `busy_documents` ждут: по ним ещё не закончилось другое такое задание.
        """
        ...

    async def claim_transcription(
        self, now: datetime, lease_until: datetime
    ) -> TranscriptionJob | None:
        """Взять в аренду одно доступное задание `transcribe` — место под вложения чата."""
        ...

    async def unleased_transcriptions(self, now: datetime) -> list[UUID]:
        """Вложения, чьи задания `transcribe` никто не держит в аренде."""
        ...

    async def exists(self, job_id: UUID) -> bool:
        """Есть ли ещё задание: снятое (документ удалён) выполнять дальше незачем."""
        ...

    async def renew(self, job_id: UUID, lease_until: datetime) -> bool:
        """Продлить аренду; `False` — задания больше нет, работу надо прекратить."""
        ...

    async def postpone(self, job_id: UUID, run_after: datetime) -> None:
        """Снять аренду и отложить задание до повтора."""
        ...

    async def release(self, job_id: UUID) -> bool:
        """Вернуть взятое задание в очередь, не считая попытку (остановка воркера).

        `False` — задания уже нет: оно выполнено или снято.
        """
        ...

    async def requeue(self, job_id: UUID) -> None:
        """Вернуть задание в очередь после удачной порции: счёт попыток — с нуля (§11.5)."""
        ...

    async def finish(self, job_id: UUID) -> None:
        """Удалить выполненное задание."""
        ...

    async def has_leased(self, now: datetime) -> bool:
        """Есть ли задания в аренде: признак работающего цикла воркера."""
        ...

    async def set_bulk_paused(self, paused: bool) -> None:
        """Приостановить или возобновить массовую переобработку (§11.6)."""
        ...


class KbUnitOfWork(Protocol):
    """Хранилище одной транзакции."""

    documents: DocumentRepository
    jobs: JobQueue
    audit: AuditLog

    async def commit(self) -> None:
        """Зафиксировать сделанное."""
        ...


class KbUnitOfWorkFactory(Protocol):
    """Открывает единицу работы: фиксация при выходе без ошибки, иначе откат."""

    def __call__(self) -> AbstractAsyncContextManager[KbUnitOfWork]:
        """Начать транзакцию."""
        ...
