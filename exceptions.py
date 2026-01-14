"""
Совместимость для старого пакета `docx`, который ожидает модуль `exceptions`.
В стандартной библиотеке Python 3 класс PendingDeprecationWarning живет в builtins,
поэтому экспортируем его здесь, чтобы импорт прошел успешно.
"""

try:  # Python 3 standard location
    from builtins import PendingDeprecationWarning  # type: ignore
except Exception:
    class PendingDeprecationWarning(Warning):
        """Fallback, если builtins не содержит PendingDeprecationWarning."""
        pass
