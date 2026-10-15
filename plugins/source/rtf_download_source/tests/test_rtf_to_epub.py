"""Tests for RTF to EPUB conversion."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from ebookerr_sdk.epub import EpubDocument
from ebookerr_sdk.validate.validate import validate_epub
from rtf_download_source.rtf_to_epub import (
    RTF_DAMAGED,
    RTF_NO_TEXT,
    RtfRefusedError,
    convert_rtf_to_epub,
)


def _word_shaped_rtf() -> str:
    r"""An RTF as Word writes it: \info, a bold title line, three chapters of five paragraphs."""
    para = "Paragraph {k} of chapter {n} runs across the frozen map, past the last survey line."
    body = ""
    for n, name in ((1, "The Survey"), (2, "Whiteout"), (3, "The Last Line")):
        body += r"\pard\b Chapter " + f"{n}: {name}" + r"\b0\par" + "\n"
        for k in range(1, 6):
            body += r"\pard " + para.format(k=k, n=n) + r"\par" + "\n"
    head = (
        r"{\rtf1\ansi{\fonttbl{\f0 Times;}}"
        r"{\info{\title Winter Cartography}{\author Ilse Brann}}"
    )
    return head + "\n" + r"\pard\b Winter Cartography\b0\par" + "\n" + body + "}"


def _boom(raw: str) -> str:
    """Fail like striprtf on a broken group."""
    raise ValueError("bad group")


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


@pytest.mark.pins("EXP-302")
def test_every_par_becomes_its_own_paragraph_and_info_names_the_book(tmp_path: Path) -> None:
    """Each \\par is a paragraph and \\info supplies the title and author (EXP-302)."""
    rtf_path = tmp_path / "w.rtf"
    rtf_path.write_text(_word_shaped_rtf(), encoding="utf-8")
    out = tmp_path / "out.epub"

    result = convert_rtf_to_epub(rtf_path, out)

    assert result.title == "Winter Cartography"
    assert result.author == "Ilse Brann"
    assert result.title_from_filename is False
    assert result.source_chapter_count == 3
    assert EpubDocument.open(out).content_chapter_count() == 3
    with zipfile.ZipFile(out) as archive:
        for n in (1, 2, 3):
            chapter = archive.read(f"OEBPS/chapter_{n:03d}.xhtml").decode("utf-8")
            assert chapter.count("<p>") == 5
    report = validate_epub(out)
    assert report.valid
    assert report.findings == []


def test_an_rtf_without_info_falls_back_as_before(tmp_path: Path) -> None:
    """Without \\info the title is the file stem and the author stays empty (EXP-221)."""
    rtf_path = tmp_path / "plain.rtf"
    rtf_path.write_bytes(rb"{\rtf1\ansi Harbour Lights\par\par The tide came in slowly.\par}")

    result = convert_rtf_to_epub(rtf_path, tmp_path / "out.epub")

    assert result.title == "plain"
    assert result.author is None
    assert result.title_from_filename is True
    assert result.source_chapter_count == 1


def test_a_damaged_rtf_is_refused_with_a_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parser failure is refused with one user-facing sentence (EXP-297)."""
    rtf_path = tmp_path / "broken.rtf"
    rtf_path.write_bytes(rb"{\rtf1\ansi Anything}")
    monkeypatch.setattr("rtf_download_source.rtf_to_epub.rtf_to_text", _boom)

    with pytest.raises(RtfRefusedError) as exc:
        convert_rtf_to_epub(rtf_path, tmp_path / "out.epub")

    assert str(exc.value) == RTF_DAMAGED
    assert exc.value.user_facing is True


def test_an_empty_rtf_is_refused_with_a_sentence(tmp_path: Path) -> None:
    """An RTF with no text is refused with one user-facing sentence (EXP-297)."""
    rtf_path = tmp_path / "blank.rtf"
    rtf_path.write_bytes(rb"{\rtf1\ansi }")

    with pytest.raises(RtfRefusedError) as exc:
        convert_rtf_to_epub(rtf_path, tmp_path / "out.epub")

    assert str(exc.value) == RTF_NO_TEXT
