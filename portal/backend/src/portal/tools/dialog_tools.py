"""Правила диалогов инструментов — реализации порта `DialogTool` (§5.3, §5.4, §7).

Схема базы, текст документа и таблица реквизитов — недоверенные данные: они идут в
размеченных блоках, содержимое которых не может закрыть свой блок.
"""

import re
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any
from uuid import UUID

from portal.core.errors import feature_disabled, field_error, not_found, validation_error
from portal.core.settings import CogisSettings, DocparseSettings, SqlSettings
from portal.databases import errors as db_errors
from portal.databases.domain import Selection, selection_of
from portal.databases.service import DbService
from portal.dialogs.context import Turn, data_block, fields_block
from portal.dialogs.ports import AnswerPlan, DialogUnitOfWorkFactory, RunReport
from portal.kb.ports import KnowledgeBase, KnowledgeSelection, KnowledgeUnavailableError
from portal.tools import errors
from portal.tools.sql_check import (
    DangerRules,
    SyntaxChecker,
    answer_sql_blocks,
    find_dangers,
    question_dangers,
)
from portal.tools.sql_run import DbCheck, SampleSource, SqlTestRunner
from portal.tools.sql_schemas import SqlSchema, SqlSchemaService


def document_block(file_name: str, text: str) -> str:
    """Блок с текстом документа разбора."""
    return data_block("документ", text or "(в документе нет текста)", file_name)


class SqlTool:
    """Диалог `sql`: схема и диалект в системном сообщении, проверка запросов.

    С 1.0 — выбор подключения у диалога (§7.6.9): отмеченные таблицы первым блоком вопроса
    и проверка на образце (§7.4).
    """

    def __init__(
        self,
        schemas: SqlSchemaService,
        checker: SyntaxChecker,
        settings: SqlSettings,
        runner: SqlTestRunner | None = None,
        *,
        databases: DbService | None = None,
        dialogs: DialogUnitOfWorkFactory | None = None,
    ) -> None:
        """Получить зависимости явно.

        `runner` — проверка на тестовых данных; `None` — она выключена флагом
        `sql.sandbox.enabled`, и к песочнице портал не обращается (§7.4). `databases` и
        `dialogs` — выбор подключения диалога; без них выбора нет.
        """
        self._schemas = schemas
        self._checker = checker
        self._settings = settings
        self._runner = runner
        self._databases = databases
        self._dialogs = dialogs
        self._error_line_patterns = tuple(
            re.compile(pattern, re.IGNORECASE) for pattern in settings.error_line_patterns
        )

    def _rules(self, sqlglot_dialect: str) -> DangerRules:
        """Настройки поиска опасных операций для диалекта."""
        return DangerRules(
            self._error_line_patterns,
            bool(sqlglot_dialect) and self._checker.backslash_escapes(sqlglot_dialect),
            self._settings.error_line_check_chars,
        )

    async def _schema(self, owner_id: UUID, params: Mapping[str, Any]) -> SqlSchema | None:
        raw = params["schema_id"]
        if raw is None:
            return None
        schema = await self._schemas.find(owner_id, UUID(str(raw)))
        if schema is None:
            raise errors.schema_not_found()
        return schema

    def _db_enabled(self) -> bool:
        return self._databases is not None and self._databases.enabled

    async def _dialog_selection(self, owner_id: UUID, dialog_id: UUID) -> Selection | None:
        """Выбор подключения у диалога; при `db.enabled: false` он не читается (§7.6.10)."""
        if not self._db_enabled() or self._dialogs is None:
            return None
        async with self._dialogs() as uow:
            dialog = await uow.dialogs.get_dialog(owner_id, dialog_id)
        if dialog is None:
            return None
        return selection_of(dialog.db_connection_id, dialog.db_connection_title, dialog.db_tables)

    async def with_selection(
        self,
        owner_id: UUID,
        dialog_id: UUID,
        params: Mapping[str, Any],
        *,
        question: bool,
    ) -> dict[str, Any]:
        """Параметры вопроса или оценки с выбором диалога в `db` (§5.3, §7.6.9).

        С выбором `schema_id` — `null` (§5.3, §5.10.3); у вопроса ещё диалект —
        `sql.sandbox.dialect` и выбор доступен (иначе `409 db_connection_unavailable`).
        """
        selection = await self._dialog_selection(owner_id, dialog_id)
        if selection is None:
            return {**params, "db": None}
        if params["schema_id"] is not None:
            raise validation_error([field_error("schema_id", "invalid_format")])
        if question and params["dialect"] != self._settings.sandbox.dialect:
            raise validation_error([field_error("dialect", "invalid_format")])
        assert self._databases is not None  # выбор читается только при нём
        if question and not await self._databases.available(owner_id, selection):
            raise db_errors.connection_unavailable()
        return {**params, "db": _db_params(selection)}

    def _testable(self, params: Mapping[str, Any]) -> bool:
        """Можно ли проверить ответ на тестовых данных: схема или подключение, диалект."""
        source = params["schema_id"] is not None or params.get("db") is not None
        return source and params["dialect"] == self._settings.sandbox.dialect

    async def accept(
        self,
        owner_id: UUID,
        dialog_id: UUID,
        content: str,
        params: Mapping[str, Any],
        *,
        regenerate: bool = False,
    ) -> list[str] | None:
        """Диалект и схема существуют; в запросе из вопроса ищутся опасные операции.

        Отказы по полю `test_run` — только у нового вопроса: при повторной генерации
        невозможная проверка просто не выполняется (§5.6). Выбор подключения вопроса
        недоступен — `409 db_connection_unavailable` (§7.6.9).
        """
        if self._settings.dialect(params["dialect"]) is None:
            raise validation_error([field_error("dialect", "unknown_value")])
        if params["test_run"] and not regenerate:
            if self._runner is None:
                raise feature_disabled()
            if not self._testable(params):
                raise validation_error([field_error("test_run", "invalid_format")])
        await self._schema(owner_id, params)
        db = params.get("db")
        if db is not None and not await self._available(owner_id, db):
            raise db_errors.connection_unavailable()
        # При действии «написать запрос» в вопросе запроса нет — проверять нечего.
        if params["action"] == "write":
            return None
        dialect = self._settings.dialect(params["dialect"])
        return question_dangers(content, self._rules(dialect.sqlglot if dialect else ""))

    async def _available(self, owner_id: UUID, db: Mapping[str, Any]) -> bool:
        if self._databases is None:
            return False
        return await self._databases.available(owner_id, _selection(db))

    async def _db_schema(self, owner_id: UUID, db: Mapping[str, Any]) -> str | None:
        """DDL-подобный текст отмеченных таблиц по текущему снимку; `None` — блока нет."""
        if self._databases is None:
            return None
        return await self._databases.schema_text(owner_id, _selection(db))

    async def plan(
        self, owner_id: UUID, dialog_id: UUID, content: str, params: Mapping[str, Any]
    ) -> AnswerPlan:
        """Системное сообщение по действию, с названием диалекта и текстом схемы.

        С выбором подключения отмеченные таблицы — первым блоком вопроса (роль `user`):
        это данные из базы заказчика, а не текст сотрудника (§7.6.9).
        """
        dialect = self._settings.dialect(params["dialect"])
        if dialect is None:
            raise validation_error([field_error("dialect", "unknown_value")])
        prompt: str = getattr(self._settings.system_prompts, params["action"])
        system = prompt.replace("{dialect}", dialect.title).strip()
        schema = await self._schema(owner_id, params)
        if schema is not None:
            # Схему пользователь вводит сам и видит только он, поэтому она в системном
            # сообщении (§5.4) — но всё равно размеченным блоком данных.
            system = f"{system}\n\n{data_block('схема', schema.content, schema.name)}"
        question = content
        test_schema = None if schema is None else schema.content
        db = params.get("db")
        if db is not None:
            text = await self._db_schema(owner_id, db)
            if text is not None:
                question = f"{data_block('схема', text, db['title'])}\n\n{content}"
            test_schema = text or ""
        testable = params["test_run"] and self._runner is not None and self._testable(params)
        return AnswerPlan(
            system=system,
            question=Turn("user", question),
            effort=self._settings.reasoning_effort,
            max_tokens=self._settings.max_output_tokens,
            sql_dialect=dialect.sqlglot,
            sql_test_schema=test_schema if testable else None,
            sql_test_db=db if testable else None,
        )

    async def recheck_plan(self, owner_id: UUID, params: Mapping[str, Any]) -> AnswerPlan:
        """План «Проверить снова» по параметрам вопроса (§7.4): до потока — отказы.

        `403 feature_disabled` — песочница выключена; диалект не `sql.sandbox.dialect` —
        `validation_error` по `index`; выбор недоступен — `409 db_connection_unavailable`.
        """
        if self._runner is None:
            raise feature_disabled()
        if params["dialect"] != self._settings.sandbox.dialect:
            raise validation_error([field_error("index", "invalid_format")])
        db = params.get("db")
        if db is not None and not await self._available(owner_id, db):
            raise db_errors.connection_unavailable()
        dialect = self._settings.dialect(params["dialect"])
        schema = await self._schema(owner_id, params)
        test_schema = "" if schema is None else schema.content
        if db is not None:
            test_schema = await self._db_schema(owner_id, db) or ""
        return AnswerPlan(
            system="",
            question=Turn("user", ""),
            effort=self._settings.reasoning_effort,
            max_tokens=self._settings.max_output_tokens,
            sql_dialect=None if dialect is None else dialect.sqlglot,
            sql_test_schema=test_schema,
            sql_test_db=db,
        )

    async def review(self, plan: AnswerPlan, content: str) -> Mapping[str, Any] | None:
        """`SqlCheck`: синтаксис и опасные операции каждого закрытого блока `sql`.

        Незакрытый блок (ответ упёрся в предел длины) записи не получает. Блок, чей
        синтаксис не проверялся, получает запись с `valid: null` и причиной в `unchecked`.
        """
        blocks = []
        for block in answer_sql_blocks(content):
            if not block.closed or plan.sql_dialect is None:
                continue
            verdict = await self._checker.check(block.text, plan.sql_dialect)
            checked = not isinstance(verdict, str)
            error = None
            if not isinstance(verdict, str) and not verdict.valid:
                error = {"line": verdict.line, "column": verdict.column, "near": verdict.near}
            blocks.append(
                {
                    "index": block.index,
                    "line": block.line,
                    "valid": verdict.valid if not isinstance(verdict, str) else None,
                    "error": error,
                    "unchecked": None if checked else verdict,
                    # Опасные операции от разбора синтаксиса не зависят.
                    "dangers": find_dangers([block.text], self._rules(plan.sql_dialect)),
                }
            )
        return {"blocks": blocks}

    async def db_check(self, owner_id: UUID, selection: Selection) -> DbCheck:
        """Проверка на образце по выбору: подключение и снимок — текущие (§7.4, §7.5.18)."""
        source = None
        sample_rows = 0
        max_tables = 0
        if self._databases is not None:
            sample_rows = self._databases.sample_rows_check
            max_tables = self._databases.sample_max_tables
            found = await self._databases.source(owner_id, selection)
            if found is not None:
                source = SampleSource(
                    found.connection,
                    found.snapshot,
                    partial(self._databases.sample, owner_id, found.connection, preview=False),
                )
        return DbCheck(selection.title, sample_rows, source, max_tables)

    async def test_run(
        self,
        owner_id: UUID,
        plan: AnswerPlan,
        content: str,
        report: Callable[[RunReport], None],
        *,
        only: int | None = None,
    ) -> None:
        """Проверить блоки `sql` ответа в песочнице (§7.4); с подключением — на образце.

        `only` — «Проверить снова»: только блок с этим `index`.
        """
        if self._runner is None or plan.sql_test_schema is None or plan.sql_dialect is None:
            return
        db = None
        if plan.sql_test_db is not None:
            db = await self.db_check(owner_id, _selection(plan.sql_test_db))
        await self._runner.run(
            owner_id, plan.sql_test_schema, plan.sql_dialect, content, report, db=db, only=only
        )


def _selection(db: Mapping[str, Any]) -> Selection:
    """Выбор из `messages.params.db`."""
    raw = db.get("connection_id")
    return Selection(
        None if raw is None else UUID(str(raw)),
        str(db["title"]),
        tuple((item["schema"], item["name"]) for item in db["tables"]),
    )


def _db_params(selection: Selection) -> dict[str, Any]:
    """`messages.params.db` — выбор на момент вопроса (§5.3)."""
    return {
        "connection_id": None if selection.connection_id is None else str(selection.connection_id),
        "title": selection.title,
        "tables": selection.tables_data(),
    }


class CogisTool:
    """Диалог `cogis`: каждый вопрос ищется в документации CoGIS общей базы."""

    def __init__(self, knowledge: KnowledgeBase, settings: CogisSettings) -> None:
        """Получить зависимости явно."""
        self._knowledge = knowledge
        self._settings = settings

    async def accept(
        self,
        owner_id: UUID,
        dialog_id: UUID,
        content: str,
        params: Mapping[str, Any],
        *,
        regenerate: bool = False,
    ) -> list[str] | None:
        """Своих проверок до потока у помощника нет."""
        return None

    async def plan(
        self, owner_id: UUID, dialog_id: UUID, content: str, params: Mapping[str, Any]
    ) -> AnswerPlan:
        """Системное сообщение по действию; без документации — требование сказать об этом."""
        system: str = getattr(self._settings.system_prompts, params["action"]).strip()
        try:
            documented = await self._knowledge.has_cogis_documentation()
        except KnowledgeUnavailableError:
            # Недоступность базы покажет сам поиск событием `knowledge_unavailable`.
            documented = True
        if not documented:
            system = f"{system}\n\n{self._settings.no_documentation_notice.strip()}"
        return AnswerPlan(
            system=system,
            question=Turn("user", content),
            effort=self._settings.reasoning_effort,
            max_tokens=self._settings.max_output_tokens,
            # «Министерства» и личные документы в этот поиск не входят (§5.4).
            selection=KnowledgeSelection(frozenset({"shared"}), cogis_only=True),
        )

    async def review(self, plan: AnswerPlan, content: str) -> Mapping[str, Any] | None:
        """Проверки готового ответа нет."""
        return None

    async def test_run(
        self,
        owner_id: UUID,
        plan: AnswerPlan,
        content: str,
        report: Callable[[RunReport], None],
        *,
        only: int | None = None,
    ) -> None:
        """Проверки на тестовых данных у этого вида нет."""


class DocparseDialogTool:
    """Диалог `docparse`: вопросы по разобранному документу."""

    def __init__(self, uow_factory: DialogUnitOfWorkFactory, settings: DocparseSettings) -> None:
        """Получить зависимости явно."""
        self._uow_factory = uow_factory
        self._settings = settings

    async def accept(
        self,
        owner_id: UUID,
        dialog_id: UUID,
        content: str,
        params: Mapping[str, Any],
        *,
        regenerate: bool = False,
    ) -> list[str] | None:
        """Своих проверок до потока нет."""
        return None

    async def plan(
        self, owner_id: UUID, dialog_id: UUID, content: str, params: Mapping[str, Any]
    ) -> AnswerPlan:
        """В каждый запрос входят текст документа и таблица реквизитов — блоками данных."""
        async with self._uow_factory() as uow:
            docparse = await uow.dialogs.get_docparse(owner_id, dialog_id)
        if docparse is None:
            raise not_found()
        blocks = [
            document_block(docparse.file_name, docparse.document_text),
            fields_block(docparse.fields),
            content,
        ]
        return AnswerPlan(
            system=self._settings.dialog_system_prompt.strip(),
            question=Turn("user", "\n\n".join(blocks)),
            effort=self._settings.reasoning_effort,
            max_tokens=self._settings.max_output_tokens,
        )

    async def review(self, plan: AnswerPlan, content: str) -> Mapping[str, Any] | None:
        """Проверки готового ответа нет."""
        return None

    async def test_run(
        self,
        owner_id: UUID,
        plan: AnswerPlan,
        content: str,
        report: Callable[[RunReport], None],
        *,
        only: int | None = None,
    ) -> None:
        """Проверки на тестовых данных у этого вида нет."""
