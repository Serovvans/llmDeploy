"""Порты хранилища базы знаний: документы, страницы, фрагменты, очередь заданий.

Методы для маршрутов принимают идентификатор пользователя и включают условие
видимости (docs/portal-api.md §9.15) в сам запрос. Методы воркера пользователя не
знают: воркер обслуживает все документы и ничего пользователю не отдаёт.
"""

from collections.abc import Sequence
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
    Job,
    KbDocument,
    PageText,
    Scope,
    SortField,
)
from portal.kb.ports import KnowledgeScope


class DocumentRepository(Protocol):
    """Документы, страницы и фрагменты."""

    # --- для маршрутов: условие видимости в каждом запросе ---

    async def add(self, document: KbDocument) -> bool:
        """Создать документ; `False` — такой файл в этой коллекции уже есть."""
        ...

    async def find_duplicate(
        self, user_id: UUID, scope: Scope, sha256: bytes
    ) -> DocumentView | None:
        """Документ с тем же содержимым в общей базе или в личной базе пользователя."""
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
        sort: SortField,
        order: SortOrder,
        offset: int,
        limit: int,
    ) -> tuple[list[DocumentView], int]:
        """Страница документов коллекции и их общее число."""
        ...

    async def mark_deleted(self, user_id: UUID, document_id: UUID, now: datetime) -> None:
        """Пометить документ удалённым: с этого момента его нет ни в одной выборке."""
        ...

    async def page(self, user_id: UUID, document_id: UUID, number: int) -> PageText | None:
        """Текст страницы видимого документа."""
        ...

    async def fragment(
        self, user_id: UUID, document_id: UUID, fragment_id: UUID
    ) -> Fragment | None:
        """Фрагмент видимого документа."""
        ...

    async def found_fragments(
        self, user_id: UUID, scope: KnowledgeScope, fragment_ids: Sequence[UUID]
    ) -> list[FoundFragment]:
        """Повторная проверка найденного (§8.4): только готовые и доступные документы."""
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

    async def stored_pages(self, document_id: UUID) -> dict[int, int]:
        """Уже прочитанные страницы: номер → длина текста в символах."""
        ...

    async def add_page(self, document_id: UUID, page: PageText) -> None:
        """Сохранить страницу и увеличить счётчик хода; повтор той же страницы — без изменений."""
        ...

    async def pages(self, document_id: UUID) -> list[PageText]:
        """Все страницы документа по порядку."""
        ...

    async def replace_fragments(self, document_id: UUID, fragments: Sequence[Fragment]) -> None:
        """Удалить прежние фрагменты документа и записать новые."""
        ...

    async def delete(self, document_id: UUID) -> None:
        """Стереть строку документа; страницы, фрагменты и задания уходят каскадом."""
        ...

    async def ids_for_reindex(self, *, only_errors: bool) -> list[UUID]:
        """Неудалённые документы, которые `portal reindex` вернёт в очередь."""
        ...


class JobQueue(Protocol):
    """Очередь заданий в таблице `kb_jobs` (§11)."""

    async def add(self, job: Job) -> None:
        """Поставить задание."""
        ...

    async def reset_or_add(self, job: Job) -> None:
        """Поставить задание, а если оно уже есть — начать счёт попыток заново."""
        ...

    async def remove(self, document_id: UUID, kind: str) -> None:
        """Снять задание документа."""
        ...

    async def claim(self, now: datetime, lease_until: datetime) -> Job | None:
        """Взять одно доступное задание в аренду: `delete` раньше `index`, затем по времени."""
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

    async def release(self, job_id: UUID) -> None:
        """Вернуть взятое задание в очередь, не считая попытку (остановка воркера)."""
        ...

    async def finish(self, job_id: UUID) -> None:
        """Удалить выполненное задание."""
        ...

    async def has_leased(self, now: datetime) -> bool:
        """Есть ли задания в аренде: признак работающего цикла воркера."""
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
