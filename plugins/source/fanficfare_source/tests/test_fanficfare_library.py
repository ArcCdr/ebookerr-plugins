"""FanFicFare run as a library in the plugin's own process (LIB-D25, LIB-D27–LIB-D30)."""

from __future__ import annotations

import datetime
import logging
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fanficfare.adapters import adapter_test1
from fanficfare.epubutils import get_update_data
from fanficfare_source.library import (
    UNICODE_SAFEPATTERN,
    UPDATE_OVERRIDES,
    FanFicFareLibraryGateway,
)
from fanficfare_source.protocol import UNREADABLE_MESSAGE, UNRECOGNISED_MESSAGE

PACKAGED_INI = Path(__file__).resolve().parents[1] / "fanficfare_source" / "personal.ini"
STORY = "http://test1.com?sid=1001"
STORED = "Ann Author/Test Story 1001.epub"
LOGIN_SENTENCE = "the site refused the login — check this site's sign-in in Settings → Credentials"
_VALID_ENTRIES = (
    "valid_entries:title,author_list,authorId_list,authorUrl_list,category_list,genre_list,"
    "status,datePublished,dateUpdated,numWords,description"
)
TEST_STORIES = "\n".join(
    [
        "",
        "[teststory:defaults]",
        _VALID_ENTRIES,
        "title:Test Story {{storyId}}",
        "author_list:Ann Author",
        "authorId_list:a1",
        "authorUrl_list:http://test1.com/a1",
        "status:In-Progress",
        "datePublished:2020-01-01",
        "dateUpdated:2020-01-01",
        "numWords:1000",
        "description:A test story.",
        "chaptertitles:One,Two,Three",
        "",
    ]
)
"""FanFicFare's test-site stories, defined in the ini (a sid of 1000 or more reads them)."""


def write_ini(tmp_path: Path, chapters: str, story: str = "1001") -> Path:
    """Write the packaged personal.ini plus the test stories, *story* holding *chapters*."""
    path = tmp_path / "personal.ini"
    text = PACKAGED_INI.read_text(encoding="utf-8") + TEST_STORIES
    path.write_text(text + f"\n[teststory:{story}]\nchaptertitles:{chapters}\n", encoding="utf-8")
    return path


class _FrozenDatetime(datetime.datetime):
    """``datetime.datetime`` whose ``now()`` always returns the same instant."""

    @classmethod
    def now(cls, tz: datetime.tzinfo | None = None) -> _FrozenDatetime:
        """Return the frozen instant."""
        return cls(2020, 1, 1, 12, 0, 0)


@pytest.fixture(autouse=True)
def frozen_test_site_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the clock FanFicFare's test site writes into every chapter it serves.

    The test site stamps ``datetime.now()``, to the second, into each chapter's text, so a re-check
    that lands in a later second than the download reads as an edit and counts as ``updated``.
    """
    monkeypatch.setattr(adapter_test1, "datetime", SimpleNamespace(datetime=_FrozenDatetime))


@pytest.fixture
def fetched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the chapter numbers the test adapter fetches, in order."""
    calls: list[str] = []
    original = adapter_test1.TestSiteAdapter.getChapterText

    def recording(self: Any, url: str) -> Any:
        """Record the chapter number, then fetch as usual."""
        calls.append(url.split("chapter=")[-1])
        return original(self, url)

    monkeypatch.setattr(adapter_test1.TestSiteAdapter, "getChapterText", recording)
    return calls


def _rename_chapter_files(epub: Path) -> None:
    """Rename every OEBPS/fileNNNN.xhtml to OEBPS/chapter_NN_x.xhtml, as a merge does (LIB-D28)."""
    with zipfile.ZipFile(epub) as zin:
        items = [(info.filename, zin.read(info.filename)) for info in zin.infolist()]
    renames = {
        name: f"OEBPS/chapter_{int(match.group(1)):02d}_x.xhtml"
        for name, _ in items
        if (match := re.fullmatch(r"OEBPS/file(\d{4})\.xhtml", name))
    }
    tmp = epub.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w") as zout:
        for name, data in items:
            if name.endswith((".opf", ".ncx")):
                text = data.decode("utf-8")
                for old, new in renames.items():
                    text = text.replace(old, new)
                data = text.encode("utf-8")
            new_name = renames.get(name, name)
            compress = zipfile.ZIP_STORED if new_name == "mimetype" else zipfile.ZIP_DEFLATED
            zout.writestr(new_name, data, compress_type=compress)
    tmp.replace(epub)


def _stamp_story_url(epub: Path, story_url: str) -> int:
    """Stamp *story_url* on every chapterurl meta, as releases before 2.22.2 did.

    Return the count."""
    with zipfile.ZipFile(epub) as zin:
        items = [(info.filename, zin.read(info.filename)) for info in zin.infolist()]
    stamped = 0
    tmp = epub.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w") as zout:
        for name, data in items:
            if name.endswith(".xhtml"):
                text, count = re.subn(
                    r'(<meta name="chapterurl" content=")[^"]*(")',
                    rf"\g<1>{story_url}\g<2>",
                    data.decode("utf-8"),
                )
                stamped += count
                data = text.encode("utf-8")
            compress = zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
            zout.writestr(name, data, compress_type=compress)
    tmp.replace(epub)
    return stamped


def test_the_update_policy_is_fixed() -> None:
    """Every run sets exactly the five options of the fixed update policy (LIB-D27)."""
    assert dict(UPDATE_OVERRIDES) == {
        "always_overwrite": "true",
        "never_make_cover": "true",
        "update_check_recent_chapters": "1",
        "update_preserve_deleted_chapters": "true",
        "output_filename_safepattern": UNICODE_SAFEPATTERN,
    }


def test_the_safepattern_hides_the_invisible_direction_marks() -> None:
    """The filename pattern strips the direction marks, and holds no % for the ini parser."""
    for mark in ("\u200e", "\u200f", "\u202a", "\u202e", "\u2066", "\u2069"):
        assert mark in UNICODE_SAFEPATTERN
    assert "%" not in UNICODE_SAFEPATTERN


def test_a_new_story_is_created_with_every_chapter(tmp_path: Path, fetched: list[str]) -> None:
    """With no staged file every chapter is fetched and the EPUB is written (LIB-D25, LIB-D29)."""
    work = tmp_path / "w"
    work.mkdir()
    result = FanFicFareLibraryGateway(write_ini(tmp_path, "One,Two,Three")).download(
        STORY, work_dir=work, staged_filename=None
    )
    assert result.outcome == "created"
    assert result.output_filename == STORED
    assert (work / STORED).is_file()
    assert result.site_chapters == 3
    assert result.chapters_before == 0
    assert result.chapters_after == 3
    assert result.distinct_urls_after == 3
    assert result.added == 3
    assert result.json_data["title"] == "Test Story 1001"
    assert result.json_data["numChapters"] == "3"
    assert result.json_data["output_filename"] == STORED
    assert len(result.json_data["zchapters"]) == 3
    assert fetched == ["1", "2", "3"]


def test_an_update_fetches_only_the_new_chapter(tmp_path: Path, fetched: list[str]) -> None:
    """A staged book is rebuilt in place, fetching the re-check window and the new chapter."""
    work = tmp_path / "w"
    work.mkdir()
    gateway = FanFicFareLibraryGateway(write_ini(tmp_path, "One,Two,Three"))
    gateway.download(STORY, work_dir=work, staged_filename=None)
    fetched.clear()
    write_ini(tmp_path, "One,Two,Three,Four")
    result = gateway.download(STORY, work_dir=work, staged_filename=STORED)
    assert result.outcome == "updated"
    assert result.chapters_before == 3
    assert result.chapters_after == 4
    assert result.added == 1
    assert result.updated == 0
    assert result.output_filename == STORED
    # the re-check window is the old EPUB's newest chapter (3); chapter 4 is new
    assert fetched == ["3", "4"]


def test_the_same_count_rebuilds_without_new_chapters(tmp_path: Path, fetched: list[str]) -> None:
    """When the site holds no new chapter only the newest one is re-checked, and kept as it was."""
    work = tmp_path / "w"
    work.mkdir()
    gateway = FanFicFareLibraryGateway(write_ini(tmp_path, "One,Two,Three,Four"))
    gateway.download(STORY, work_dir=work, staged_filename=None)
    fetched.clear()
    result = gateway.download(STORY, work_dir=work, staged_filename=STORED)
    assert result.outcome == "updated"
    assert result.added == 0
    assert result.updated == 0
    assert result.chapters_after == 4
    assert fetched == ["4"]


def test_chapters_the_site_removed_are_kept(tmp_path: Path) -> None:
    """A chapter the site no longer lists stays in the book (update_preserve_deleted_chapters)."""
    work = tmp_path / "w"
    work.mkdir()
    gateway = FanFicFareLibraryGateway(write_ini(tmp_path, "One,Two,Three,Four"))
    gateway.download(STORY, work_dir=work, staged_filename=None)
    write_ini(tmp_path, "One,Two,Three")
    result = gateway.download(STORY, work_dir=work, staged_filename=STORED)
    assert result.outcome == "updated"
    assert result.site_chapters == 3
    assert result.chapters_before == 4
    assert result.chapters_after == 4
    assert result.added == 0


def test_a_file_with_no_recognised_chapter_is_left_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A merged book's file (chapters renamed) is neither rebuilt nor changed (LIB-D28)."""
    work = tmp_path / "w"
    work.mkdir()
    gateway = FanFicFareLibraryGateway(write_ini(tmp_path, "One,Two,Three"))
    gateway.download(STORY, work_dir=work, staged_filename=None)
    _rename_chapter_files(work / STORED)
    assert get_update_data(str(work / STORED))[1] == 0
    before = (work / STORED).read_bytes()
    with caplog.at_level(logging.INFO, logger="fanficfare_source.library"):
        result = gateway.download(STORY, work_dir=work, staged_filename=STORED)
    assert result.outcome == "unrecognised"
    assert result.error == UNRECOGNISED_MESSAGE
    assert (work / STORED).read_bytes() == before
    assert (
        logging.INFO,
        "FanFicFare recognises no chapter in Ann Author/Test Story 1001.epub; it is left as it is",
    ) in [(record.levelno, record.getMessage()) for record in caplog.records]


def test_an_unreadable_file_is_left_alone(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A staged file that is not an EPUB at all is neither rebuilt nor changed (LIB-D28)."""
    work = tmp_path / "w"
    work.mkdir()
    (work / "x.epub").write_bytes(b"not a zip")
    gateway = FanFicFareLibraryGateway(write_ini(tmp_path, "One,Two,Three"))
    with caplog.at_level(logging.DEBUG, logger="fanficfare_source.library"):
        result = gateway.download(STORY, work_dir=work, staged_filename="x.epub")
    assert result.outcome == "unrecognised"
    assert result.error == UNREADABLE_MESSAGE
    assert (work / "x.epub").read_bytes() == b"not a zip"
    assert any(
        record.levelno == logging.DEBUG
        and record.getMessage().startswith("FanFicFare cannot read x.epub for an update (")
        for record in caplog.records
    )


def test_a_stamped_one_shot_grows_without_a_duplicate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A one-chapter book stamped with its own URL grows to two chapters, not three (LIB-D30)."""
    url = "http://test1.com?sid=1002"
    stored = "Ann Author/Test Story 1002.epub"
    work = tmp_path / "w"
    work.mkdir()
    gateway = FanFicFareLibraryGateway(write_ini(tmp_path, "One", story="1002"))
    created = gateway.download(url, work_dir=work, staged_filename=None)
    assert created.output_filename == stored
    assert _stamp_story_url(work / stored, url) == 1
    write_ini(tmp_path, "One,Two", story="1002")
    with caplog.at_level(logging.DEBUG, logger="fanficfare_source.library"):
        result = gateway.download(url, work_dir=work, staged_filename=stored)
    assert result.chapters_after == 2
    assert result.distinct_urls_after == 2
    assert result.added == 1
    assert any(
        record.levelno == logging.DEBUG
        and record.getMessage().startswith(
            "Re-keyed the one chapter of Ann Author/Test Story 1002.epub "
            "from http://test1.com?sid=1002 to "
        )
        for record in caplog.records
    )


def test_progress_is_reported_per_chapter(tmp_path: Path) -> None:
    """on_chapter is called once per assembled chapter with (done, total)."""
    work = tmp_path / "w"
    work.mkdir()
    calls: list[tuple[int, int]] = []
    FanFicFareLibraryGateway(write_ini(tmp_path, "One,Two,Three")).download(
        STORY,
        work_dir=work,
        staged_filename=None,
        on_chapter=lambda done, total: calls.append((done, total)),
    )
    assert calls == [(1, 3), (2, 3), (3, 3)]


def test_fanficfare_prints_nothing_on_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run writes nothing to stdout, which is the core's wire (LIB-D25)."""
    work = tmp_path / "w"
    work.mkdir()
    gateway = FanFicFareLibraryGateway(write_ini(tmp_path, "One,Two,Three"))
    capsys.readouterr()
    gateway.download(STORY, work_dir=work, staged_filename=None)
    assert capsys.readouterr().out == ""


def test_a_missing_staged_file_is_created_at_the_stored_path(tmp_path: Path) -> None:
    """A stored path with no file behind it is written there as a new book."""
    work = tmp_path / "w"
    work.mkdir()
    result = FanFicFareLibraryGateway(write_ini(tmp_path, "One,Two,Three")).download(
        STORY, work_dir=work, staged_filename="kept/Name.epub"
    )
    assert result.outcome == "created"
    assert result.output_filename == "kept/Name.epub"
    assert (work / "kept/Name.epub").is_file()


def test_a_missing_story_fails_with_its_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The site's reason reaches the result, not an empty 'no metadata' (LIB-D25)."""
    url = "http://test1.com?sid=666"
    work = tmp_path / "w"
    work.mkdir()
    with caplog.at_level(logging.DEBUG, logger="fanficfare_source.library"):
        result = FanFicFareLibraryGateway(PACKAGED_INI).download(
            url, work_dir=work, staged_filename=None
        )
    assert result.outcome == "failed"
    assert result.error == "story not found: http://test1.com?sid=666"
    assert (
        logging.DEBUG,
        "FanFicFare failed for http://test1.com?sid=666: "
        "story not found: http://test1.com?sid=666 (StoryDoesNotExist)",
    ) in [(record.levelno, record.getMessage()) for record in caplog.records]


def test_a_refused_login_fails_with_its_reason(tmp_path: Path) -> None:
    """A site that refuses the login says so in the result (LIB-D25)."""
    work = tmp_path / "w"
    work.mkdir()
    result = FanFicFareLibraryGateway(PACKAGED_INI).download(
        "http://test1.com?sid=668", work_dir=work, staged_filename=None
    )
    assert result.outcome == "failed"
    assert result.error == LOGIN_SENTENCE


def test_an_unsupported_site_fails_with_its_reason(tmp_path: Path) -> None:
    """An address no FanFicFare adapter claims is named as unsupported (LIB-D25)."""
    work = tmp_path / "w"
    work.mkdir()
    result = FanFicFareLibraryGateway(PACKAGED_INI).download(
        "https://example.org/x", work_dir=work, staged_filename=None
    )
    assert result.outcome == "failed"
    assert result.error == "unsupported site: FanFicFare does not support https://example.org/x"


def test_fetch_metadata_writes_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Metadata alone fetches no chapter and writes no EPUB, even in the current folder."""
    monkeypatch.chdir(tmp_path)
    meta = FanFicFareLibraryGateway(write_ini(tmp_path, "One,Two,Three")).fetch_metadata(STORY)
    assert meta is not None
    assert meta["title"] == "Test Story 1001"
    assert meta["numChapters"] == "3"
    assert "zchapters" not in meta
    assert list(tmp_path.rglob("*.epub")) == []


def test_fetch_metadata_of_a_missing_story_is_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A story the site does not have yields None, with the reason at DEBUG (LIB-D25)."""
    with caplog.at_level(logging.DEBUG, logger="fanficfare_source.library"):
        meta = FanFicFareLibraryGateway(PACKAGED_INI).fetch_metadata("http://test1.com?sid=666")
    assert meta is None
    assert (
        logging.DEBUG,
        "Metadata fetch failed for http://test1.com?sid=666: "
        "story not found: http://test1.com?sid=666",
    ) in [(record.levelno, record.getMessage()) for record in caplog.records]
