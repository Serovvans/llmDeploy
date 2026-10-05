"""Оценка числа токенов по коэффициентам из конфигурации (docs/portal-api.md §5.4)."""

import math


class RatioTokenEstimator:
    """Реализация порта `TokenEstimator`: символы на токен и токены на изображение."""

    def __init__(self, chars_per_token: float, tokens_per_image: int) -> None:
        """Запомнить коэффициенты."""
        self._chars_per_token = chars_per_token
        self._tokens_per_image = tokens_per_image

    def text(self, text: str) -> int:
        """Оценка для текста, с округлением вверх."""
        return math.ceil(len(text) / self._chars_per_token)

    def image(self) -> int:
        """Оценка для одного изображения."""
        return self._tokens_per_image
