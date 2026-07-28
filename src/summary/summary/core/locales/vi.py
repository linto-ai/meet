"""Vietnamese locale strings."""

from summary.core.locales.strings import LocaleStrings

STRINGS = LocaleStrings(
    empty_transcription="""
**Không phát hiện được nội dung âm thanh nào trong bản ghi của bạn.**

*Nếu bạn cho rằng đây là lỗi, vui lòng đừng ngần ngại liên hệ
với bộ phận hỗ trợ kỹ thuật của chúng tôi: visio@numerique.gouv.fr*

.

.

.

Một vài điều chúng tôi khuyên bạn nên kiểm tra:
- Micro đã được bật chưa?
- Bạn có ở đủ gần micro không?
- Micro có chất lượng tốt không?
- Bản ghi có dài hơn 30 giây không?

""",
    download_header_template=(
        "\n*[Tải xuống bản ghi của bạn (liên kết bên ngoài)]({download_link})*\n"
    ),
    form_footer_template=(
        "\n\n*[Hãy cho chúng tôi biết ý kiến của bạn về bản chép lời này]({form_link})*\n"
    ),
    hallucination_replacement_text="[Không thể chuyển văn bản]",
    summary_title_template="Tóm tắt {title}",
)
