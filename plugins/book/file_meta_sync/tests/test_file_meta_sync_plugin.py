"""Tests for the FileMetaSyncPlugin serving on the SDK host."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest
from ebookerr_sdk.spi import AssetView, AssetWrite, CustomValueView, InvocationMode, PluginEventType
from ebookerr_sdk.testing import FakeContext, FakeCore, make_book_view, make_request, run_wire
from ebookerr_sdk.wire import encode_book_view
from file_meta_sync.plugin import FileMetaSyncPlugin, _book_dict, _book_patch

_CONFLICT_HASH = hashlib.sha256(b"Old").hexdigest()


def _library(tmp_path: Path) -> Path:
    """Return a library root holding ``books/book.epub`` (empty)."""
    root = tmp_path / "library"
    (root / "books").mkdir(parents=True)
    (root / "books" / "book.epub").write_bytes(b"")
    return root


def test_the_plugin_reads_its_own_manifest() -> None:
    """The plugin manifest identifies the plugin and its settings schema."""
    assert FileMetaSyncPlugin.manifest.id == "file_meta_sync"
    assert FileMetaSyncPlugin().settings_schema().fields[0].key == "delete_on_book_delete"


def test_a_sidecar_synopsis_is_adopted(tmp_path: Path) -> None:
    """A synopsis file on disk is adopted into the database with a synced-hash record."""
    root = _library(tmp_path)
    (root / "books" / "book.back_cover.txt").write_text("FILE SIDE")
    view = make_book_view(book_id="b1", output_filename="books/book.epub")
    ctx = FakeContext(library_root=root)
    patches = FileMetaSyncPlugin().enrich((view,), ctx)
    assert len(patches) == 1
    assert patches[0].book_id == "b1"
    assert patches[0].fields == {"synopsis": "FILE SIDE"}
    assert (
        patches[0].custom_values["synopsis.synced_hash"].value
        == hashlib.sha256(b"FILE SIDE").hexdigest()
    )
    assert ctx.reports == [(100.0, None, None)]


def test_a_cover_candidate_is_sent_as_stored_bytes(tmp_path: Path) -> None:
    """A cover candidate file is read and sent as an asset write."""
    root = _library(tmp_path)
    (root / "books" / "book.alt.png").write_bytes(b"png-bytes")
    view = make_book_view(book_id="b1", output_filename="books/book.epub")
    ctx = FakeContext(library_root=root)
    patches = FileMetaSyncPlugin().enrich((view,), ctx)
    assert len(patches) == 1
    expected_sha = hashlib.sha256(b"png-bytes").hexdigest()
    assert patches[0].assets == (
        AssetWrite(
            kind="cover_candidate",
            name="book.alt.png",
            media_type="image/png",
            data=b"png-bytes",
            meta={"source_file": "book.alt.png", "sha256": expected_sha},
        ),
    )


def test_book_deleted_unlinks_the_sidecars(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """On BookDeleted, the plugin unlinks sidecar files when the setting is on."""
    root = _library(tmp_path)
    sidecar = root / "books" / "book.back_cover.txt"
    sidecar.write_text("FILE SIDE")
    view = make_book_view(book_id="b1", output_filename="books/book.epub")
    ctx = FakeContext(
        library_root=root,
        event_type=PluginEventType.BOOK_DELETED,
        logger=logging.getLogger("plugin.file_meta_sync"),
    )
    with caplog.at_level(logging.INFO, logger="plugin.file_meta_sync"):
        patches = FileMetaSyncPlugin().enrich((view,), ctx)
    assert patches == []
    assert not sidecar.exists()
    assert ctx.reports == [(100.0, None, None)]
    assert "book: deleted 1 sidecar file(s) on BookDeleted" in caplog.text


def test_book_deleted_keeps_the_sidecars_when_the_setting_is_off(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """On BookDeleted with delete_on_book_delete off, sidecars are kept."""
    root = _library(tmp_path)
    sidecar = root / "books" / "book.back_cover.txt"
    sidecar.write_text("FILE SIDE")
    view = make_book_view(book_id="b1", output_filename="books/book.epub")
    ctx = FakeContext(
        library_root=root,
        event_type=PluginEventType.BOOK_DELETED,
        settings={"delete_on_book_delete": False},
        logger=logging.getLogger("plugin.file_meta_sync"),
    )
    with caplog.at_level(logging.INFO, logger="plugin.file_meta_sync"):
        patches = FileMetaSyncPlugin().enrich((view,), ctx)
    assert patches == []
    assert sidecar.exists()
    assert ctx.reports == []
    assert "delete_on_book_delete is off: sidecar files kept" in caplog.text


def test_an_abandoned_prompt_stops_without_writing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When a prompt is abandoned, the plugin stops without writing any changes."""
    root = _library(tmp_path)
    sidecar = root / "books" / "book.back_cover.txt"
    sidecar.write_text("FILE SIDE")
    view = make_book_view(
        book_id="b1",
        output_filename="books/book.epub",
        synopsis="APP SIDE",
        custom_values={
            "synopsis.synced_hash": CustomValueView(value=_CONFLICT_HASH, value_type="string")
        },
    )
    ctx = FakeContext(
        library_root=root,
        mode=InvocationMode.HEADED,
        dialog_answers=[None],
        logger=logging.getLogger("plugin.file_meta_sync"),
    )
    with caplog.at_level(logging.WARNING, logger="plugin.file_meta_sync"):
        patches = FileMetaSyncPlugin().enrich((view,), ctx)
    assert patches == []
    assert sidecar.read_text() == "FILE SIDE"
    assert "prompt abandoned — stopped without writing" in caplog.text


def test_a_headless_conflict_keeps_the_app_value_with_a_durable_notice(tmp_path: Path) -> None:
    """In headless mode, a conflict keeps the app value and writes a durable notice."""
    root = _library(tmp_path)
    sidecar = root / "books" / "book.back_cover.txt"
    sidecar.write_text("FILE SIDE")
    view = make_book_view(
        book_id="b1",
        output_filename="books/book.epub",
        synopsis="APP SIDE",
        custom_values={
            "synopsis.synced_hash": CustomValueView(value=_CONFLICT_HASH, value_type="string")
        },
    )
    ctx = FakeContext(library_root=root)
    patches = FileMetaSyncPlugin().enrich((view,), ctx)
    assert len(patches) == 1
    assert sidecar.read_text() == "APP SIDE"
    expected_hash = hashlib.sha256(b"APP SIDE").hexdigest()
    assert patches[0].custom_values["synopsis.synced_hash"].value == expected_hash
    assert len(ctx.notices) == 1
    assert ctx.notices[0][0] == "warning"
    assert "Sidecar conflict" in ctx.notices[0][1]
    assert ctx.notices[0][2] is True


def test_runs_on_the_sdk_host(tmp_path: Path) -> None:
    """The plugin runs on the SDK host, accepting and responding via the wire protocol."""
    root = _library(tmp_path)
    sidecar = root / "books" / "book.back_cover.txt"
    sidecar.write_text("FILE SIDE")
    request = make_request(
        "enrich",
        event="BookUpdated",
        books=[
            encode_book_view(
                make_book_view(book_id="b1", output_filename="books/book.epub"),
                plugin_id="file_meta_sync",
            )
        ],
        library_root=str(root),
    )
    terminal, frames = run_wire(FileMetaSyncPlugin(), request)
    assert terminal["ok"] is True
    assert terminal["result"][0]["book_id"] == "b1"
    assert terminal["result"][0]["fields"] == {"synopsis": "FILE SIDE"}
    progress_frames = [f["percent"] for f in frames if f.get("op") == "progress"]
    assert progress_frames == [100.0]
    assert any(e["message"] == "book: back_cover.txt adopted into DB" for e in terminal["logs"])


def test_an_abandoned_prompt_over_the_wire_returns_nothing(tmp_path: Path) -> None:
    """Over the wire, an abandoned prompt returns no results."""
    root = _library(tmp_path)
    sidecar = root / "books" / "book.back_cover.txt"
    sidecar.write_text("FILE SIDE")
    view = make_book_view(
        book_id="b1",
        output_filename="books/book.epub",
        synopsis="APP SIDE",
        custom_values={
            "file_meta_sync.synopsis.synced_hash": CustomValueView(
                value=_CONFLICT_HASH, value_type="string"
            )
        },
    )
    request = make_request(
        "enrich",
        event="BookUpdated",
        interactive=True,
        books=[encode_book_view(view, plugin_id="file_meta_sync")],
        library_root=str(root),
    )
    terminal, frames = run_wire(FileMetaSyncPlugin(), request, core=FakeCore(dialog_answers=[None]))
    assert terminal["ok"] is True
    assert terminal["result"] == []
    dialog_frames = [f for f in frames if f.get("op") == "dialog"]
    assert len(dialog_frames) == 1
    assert dialog_frames[0]["kind"] == "yes_no"
    assert sidecar.read_text() == "FILE SIDE"


def test_the_book_mapping_carries_what_the_sync_reads() -> None:
    """The _book_dict function converts a BookView to the plain mapping the sync functions read."""
    view = make_book_view(
        book_id="b1",
        output_filename="books/book.epub",
        assets=(
            AssetView(
                kind="cover_candidate",
                name="x.png",
                path="",
                storage="store",
                namespace="file_meta_sync",
                meta={"source_file": "x.png", "sha256": "h"},
            ),
        ),
        custom_values={"cover.synced_hash": CustomValueView(value="h2", value_type="string")},
    )
    d = _book_dict(view)
    assert d["output_filename"] == "books/book.epub"
    assert d["assets"] == [
        {
            "kind": "cover_candidate",
            "name": "x.png",
            "path": "",
            "media_type": None,
            "updated_at": None,
            "storage": "store",
            "namespace": "file_meta_sync",
            "meta": {"source_file": "x.png", "sha256": "h"},
        }
    ]
    assert d["custom_values"] == {
        "cover.synced_hash": {"value": "h2", "value_type": "string", "updated_at": None}
    }


def test_a_delete_write_becomes_a_delete_asset() -> None:
    """A delete write in the result mapping is converted to an AssetWrite with delete=True."""
    patch = _book_patch(
        {
            "book_id": "b1",
            "fields": {},
            "custom_values": {},
            "assets": [{"kind": "cover_candidate", "name": "x.png", "delete": True}],
        }
    )
    assert patch.assets == (AssetWrite(kind="cover_candidate", name="x.png", delete=True),)
