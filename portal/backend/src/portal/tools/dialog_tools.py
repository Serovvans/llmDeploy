"""Правила диалогов инструментов — реализации порта `DialogTool` (§5.3, §5.4, §7).

Схема базы, текст документа и таблица реквизитов — недоверенные данные: они идут в
размеченных блоках, содержимое которых не может закрыть свой блок.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from portal.core.errors import field_error, not_found, validation_error
from portal.core.settings import CogisSettings, DocparseSettings, SqlSettings
from portal.dialogs.context import Turn, data_block
from portal.dialogs.ports import AnswerPlan, DialogUnitOfWorkFactory
from portal.kb.ports import KnowledgeBase, KnowledgeUnavailableError
from portal.tools import errors
from portal.tools.sql_check import (
    DangerRules,
    SyntaxChecker,
    answer_sql_blocks,
    find_dangers,
    question_dangers,
)
from portal.tools.sql_schemas import SqlSchema, SqlSchemaService


def document_block(file_name: str, text: str) -> str:
    """Блок с текстом документа разбора."""
    return data_block("документ", text or "(в документе нет текста)", file_name)


def fields_block(fields: Sequence[Mapping[str, Any]]) -> str:
    """Блок с таблицей извлечённых реквизитов."""
    lines = [f"{field['title']}: {field['value'] or '—'}" for field in fields]
    return data_block("реквизиты", "\n".join(lines) or "(реквизиты не извлечены)")


class SqlTool:
    """Диалог `sql`: схема и диалект в системном сообщении, проверка запросов."""

    def __init__(
        self, schemas: SqlSchemaService, checker: SyntaxChecker, settings: SqlSettings
    ) -> None:
        """Получить зависимости явно."""
        self._schemas = schemas
        self._checker = checker
        self._settings = settings
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

    async def accept(
        self, owner_id: UUID, dialog_id: UUID, content: str, params: Mapping[str, Any]
    ) -> list[str] | None:
        """Диалект и схема существуют; в запросе из вопроса ищутся опасные операции."""
        if self._settings.dialect(params["dialect"]) is None:
            raise validation_error([field_error("dialect", "unknown_value")])
        await self._schema(owner_id, params)
        # При действии «написать запрос» в вопросе запроса нет — проверять нечего.
        if params["action"] == "write":
            return None
        dialect = self._settings.dialect(params["dialect"])
        return question_dangers(content, self._rules(dialect.sqlglot if dialect else ""))

    async def plan(
        self, owner_id: UUID, dialog_id: UUID, content: str, params: Mapping[str, Any]
    ) -> AnswerPlan:
        """Системное сообщение по действию, с названием диалекта и текстом схемы."""
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
        return AnswerPlan(
            system=system,
            question=Turn("user", content),
            effort=self._settings.reasoning_effort,
            max_tokens=self._settings.max_output_tokens,
            sql_dialect=dialect.sqlglot,
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


class CogisTool:
    """Диалог `cogis`: каждый вопрос ищется в документации CoGIS общей базы."""

    def __init__(self, knowledge: KnowledgeBase, settings: CogisSettings) -> None:
        """Получить зависимости явно."""
        self._knowledge = knowledge
        self._settings = settings

    async def accept(
        self, owner_id: UUID, dialog_id: UUID, content: str, params: Mapping[str, Any]
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
            scope="cogis",
        )

    async def review(self, plan: AnswerPlan, content: str) -> Mapping[str, Any] | None:
        """Проверки готового ответа нет."""
        return None


class DocparseDialogTool:
    """Диалог `docparse`: вопросы по разобранному документу."""

    def __init__(self, uow_factory: DialogUnitOfWorkFactory, settings: DocparseSettings) -> None:
        """Получить зависимости явно."""
        self._uow_factory = uow_factory
        self._settings = settings

    async def accept(
        self, owner_id: UUID, dialog_id: UUID, content: str, params: Mapping[str, Any]
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
