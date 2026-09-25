"""Tests for RTF to EPUB conversion."""

from __future__ import annotations

from pathlib import Path

from rtf_download_source.rtf_to_epub import convert_rtf_to_epub


class TestRtfConversion:
    def test_rtf_reuses_the_prose_split(self, tmp_path: Path) -> None:
        """RTF novels with Chapter headings are split via parse_text_to_book (EXP-221)."""
        body = "Body text. " * 40
        rtf_content = (
            r"{\rtf1\ansi "
            rf"Chapter 1\par {body}\par "
            rf"Chapter 2\par {body}"
            "}"
        )

        rtf_path = tmp_path / "test.rtf"
        epub_path = tmp_path / "out.epub"

        rtf_path.write_text(rtf_content, encoding="utf-8")

        result = convert_rtf_to_epub(rtf_path, epub_path)

        assert result.source_chapter_count == 2
