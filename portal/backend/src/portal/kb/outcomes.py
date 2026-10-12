"""Исходы задания обработки документа, которые не являются временным сбоем (§11.2)."""

from portal.kb.domain import ErrorCode


class PermanentIndexError(Exception):
    """Причина в самом файле: повтор задания не поможет."""

    def __init__(self, code: ErrorCode) -> None:
        """Запомнить код ошибки документа."""
        super().__init__(code)
        self.code: ErrorCode = code


class JobAbandonedError(Exception):
    """Документа или задания больше нет: работу надо прекратить."""
