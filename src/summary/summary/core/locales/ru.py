"""Russian locale strings."""

from summary.core.locales.strings import LocaleStrings

STRINGS = LocaleStrings(
    empty_transcription="""
**В вашей транскрипции не обнаружено аудиоконтента.**

*Если вы считаете, что это ошибка, пожалуйста, не стесняйтесь обратиться
в нашу техническую поддержку: visio@numerique.gouv.fr*

.

.

.

Несколько моментов, которые мы рекомендуем проверить:
- Был ли включён микрофон?
- Были ли вы достаточно близко к микрофону?
- Микрофон хорошего качества?
- Длится ли запись дольше 30 секунд?

""",
    download_header_template=(
        "\n*[Скачать вашу запись (внешняя ссылка)]({download_link})*\n"
    ),
    hallucination_replacement_text="[Не удалось расшифровать текст]",
    document_default_title="Транскрипция",
    document_title_template=(
        'Встреча «{room}» {room_recording_date} в {room_recording_time}'
    ),
)
