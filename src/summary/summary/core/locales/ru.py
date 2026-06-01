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
    form_footer_template=(
        "\n\n*[Поделитесь с нами своим мнением об этой транскрипции]({form_link})*\n"
    ),
    hallucination_replacement_text="[Не удалось расшифровать текст]",
    summary_title_template="Резюме {title}",
)
