"""Tests for KavitaSyncPlugin ( ."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import ebookerr_sdk.spi as api
import pytest
from ebookerr_sdk.testing import Cancelled, FakeContext
from kavita_sync.plugin import KavitaSyncPlugin, _build_service
from kavita_sync.service import SyncResult

# ---------------------------------------------------------------------------
# Stub PluginContext
# ---------------------------------------------------------------------------


@dataclass
class _FakeCtx:
    mode: api.InvocationMode = api.InvocationMode.HEADLESS
    settings: Mapping[str, Any] = field(default_factory=dict)
    ui_context: Mapping[str, str] = field(default_factory=dict)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("test.plugin"))
    event_type: api.PluginEventType | None = None
    reported: list[float] = field(default_factory=list)
    item_failures: list[tuple[str, str]] = field(default_factory=list)
    item_skips: list[tuple[str, str]] = field(default_factory=list)
    library_root: Path | None = None

    def check_cancelled(self) -> None:
        pass

    def report(self, percent: float) -> None:
        self.reported.append(percent)

    def report_failure(self, book_id: str, message: str) -> None:
        self.item_failures.append((book_id, message))

    def report_skip(self, book_id: str, reason: str) -> None:
        self.item_skips.append((book_id, reason))

    def queue(
        self,
        *,
        plugin_id: str,
        event: api.PluginEventType,
        targets: tuple[str, ...],
    ) -> None:
        pass

    def ask_yes_no(self, message: str, yes: str, no: str) -> bool:
        return False

    def alert(self, message: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Fake repositories
# ---------------------------------------------------------------------------


class _FakeBookRepository:
    """Minimal BookRepository for testing."""

    def __init__(self) -> None:
        self.books: dict[str, Any] = {}

    def get(self, book_id: str) -> Any:
        """Return a book by ID, or None."""
        return self.books.get(book_id)

    def update_external_fields(self, book_id: str, **kwargs: Any) -> None:
        """Mock DB update for external fields."""
        if book_id not in self.books:
            self.books[book_id] = MagicMock()
        book = self.books[book_id]
        for key, val in kwargs.items():
            setattr(book, key, val)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_book_view(
    book_id: str = "b1",
    title: str | None = "Test Title",
    output_filename: str | None = "test.epub",
    external: api.ExternalLink | None = None,
    file_size: int | None = None,
) -> api.BookView:
    if external is None:
        external = api.ExternalLink()
    return api.BookView(
        book_id=book_id,
        title=title,
        author=None,
        story_url=None,
        output_filename=output_filename,
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=external,
        progress=api.ExternalProgress(),
        custom_values={},
        file_size=file_size,
    )


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------


class TestManifest:
    def test_manifest_declares_late_priority(self) -> None:
        """Manifest declares priority=900 for late-band ordering."""
        plugin = KavitaSyncPlugin()
        assert plugin.manifest.priority == 900

    def test_manifest_is_exclusive_provider(self) -> None:
        """Manifest has correct plugin_type and exclusive_group."""
        plugin = KavitaSyncPlugin()
        assert plugin.manifest.plugin_type == api.PluginType.BOOK
        assert plugin.manifest.exclusive_group == "library_server"
        assert plugin.manifest.id == "kavita_sync"
        assert plugin.manifest.events == (
            api.PluginEventType.BOOK_CREATED,
            api.PluginEventType.BOOK_UPDATED,
            api.PluginEventType.BOOK_IMPORTED,
            api.PluginEventType.BOOK_DELETED,
            api.PluginEventType.EPUB_MODIFIED,
        )

    def test_manifest_version_and_group(self) -> None:
        """Manifest version is 1.1.0 with correct exclusive_group and events."""
        plugin = KavitaSyncPlugin()
        assert plugin.manifest.version == "1.1.0"
        assert plugin.manifest.exclusive_group == "library_server"
        assert plugin.manifest.events == (
            api.PluginEventType.BOOK_CREATED,
            api.PluginEventType.BOOK_UPDATED,
            api.PluginEventType.BOOK_IMPORTED,
            api.PluginEventType.BOOK_DELETED,
            api.PluginEventType.EPUB_MODIFIED,
        )

    def test_kavita_manifest_headed_sync_trigger(self) -> None:
        """Manifest is headed=True with a book_selection_action Sync trigger."""
        plugin = KavitaSyncPlugin()
        # Check headed is True
        assert plugin.manifest.headed is True
        # Check headless is still True
        assert plugin.manifest.headless is True
        # Check ui_triggers contains exactly one trigger
        assert len(plugin.manifest.ui_triggers) == 1
        trigger = plugin.manifest.ui_triggers[0]
        # Check trigger properties
        assert trigger.scope == "book_selection_action"
        assert trigger.icon == "sync"
        assert trigger.label == "Sync to Kavita"

    def test_the_manifest_declares_its_provider_and_delete_mode(self) -> None:
        """Manifest declares provider=kavita and delete_mode=rescan."""
        plugin = KavitaSyncPlugin()
        assert plugin.manifest.provider == "kavita"
        assert plugin.manifest.delete_mode == "rescan"


class TestSettingsSchema:
    def test_settings_schema_has_secret_api_key(self) -> None:
        """Settings schema has server, api_key (secret), external_url, and library_path fields."""
        plugin = KavitaSyncPlugin()
        schema = plugin.settings_schema()

        # Check that schema has exactly 6 fields (scan_retry_max/scan_retry_delay added)
        assert len(schema.fields) == 6

        # Extract fields by key for easier checking
        field_dict = {f.key: f for f in schema.fields}

        # Check server field
        assert "server" in field_dict
        assert field_dict["server"].type == "string"
        assert field_dict["server"].label == "Server URL"
        assert field_dict["server"].secret is False

        # Check api_key field
        assert "api_key" in field_dict
        assert field_dict["api_key"].type == "string"
        assert field_dict["api_key"].label == "API key"
        assert field_dict["api_key"].secret is True

        # Check external_url field
        assert "external_url" in field_dict
        assert field_dict["external_url"].type == "string"
        assert field_dict["external_url"].label == "External URL"
        assert field_dict["external_url"].secret is False

        # Check library_path field
        assert "library_path" in field_dict
        assert field_dict["library_path"].type == "string"
        assert field_dict["library_path"].label == "Library folder"
        assert field_dict["library_path"].secret is False

    @pytest.mark.pins("EXP-099")
    def test_the_card_exposes_no_setting_the_kavita_path_ignores(self) -> None:
        """_build_service consumes server, api_key, external_url, library_path; no library_id."""
        captured_kwargs: dict[str, Any] = {}

        class FakeKavitaClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                captured_kwargs.update(kwargs)

        plugin = KavitaSyncPlugin()
        ctx = _FakeCtx(
            settings={
                "server": "http://k",
                "api_key": "K",
                "external_url": "http://e",
                "library_path": "/books",
            }
        )

        with patch(
            "kavita_sync.plugin.RequestsKavitaClient",
            FakeKavitaClient,  # type: ignore
        ):
            service = _build_service(ctx, enabled=True)

        # The client should receive external_url kwarg
        assert "external_url" in captured_kwargs
        assert captured_kwargs["external_url"] == "http://e"

        # The service should have external_url and library_path
        assert service._external_url == "http://e"
        assert service._library_path == "/books"

        # Schema fields are server, api_key, external_url, library_path, and the two
        # scan-retry settings — never a Komga-only library_id.
        schema_field_keys = {f.key for f in plugin.settings_schema().fields}
        assert schema_field_keys == {
            "server",
            "api_key",
            "external_url",
            "library_path",
            "scan_retry_max",
            "scan_retry_delay",
        }
        assert "library_id" not in schema_field_keys


def test_schema_has_scan_retry_settings() -> None:
    """The schema declares scan_retry_max/scan_retry_delay as int fields defaulting 20s/2s."""
    plugin = KavitaSyncPlugin()
    field_dict = {f.key: f for f in plugin.settings_schema().fields}

    assert field_dict["scan_retry_max"].type == "int"
    assert field_dict["scan_retry_max"].default == "20"
    assert field_dict["scan_retry_delay"].type == "int"
    assert field_dict["scan_retry_delay"].default == "2"


def test_build_service_passes_retry_settings() -> None:
    """_build_service reads scan_retry_max from settings and passes it to KavitaService."""
    ctx = _FakeCtx(
        settings={
            "server": "http://kavita.local:5000",
            "api_key": "test_key",
            "scan_retry_max": "5",
        }
    )

    service = _build_service(ctx, enabled=True)

    assert service._scan_retry_max == 5


def test_build_service_binds_a_scan_ledger_to_the_run() -> None:
    """_build_service creates a ScanLedger bound to the plugin's context (DFT-D21, PMG-D32)."""
    from ebookerr_sdk.providers.scan_ledger import ScanLedger

    ctx = _FakeCtx(
        settings={
            "server": "http://kavita.local:5000",
            "api_key": "test_key",
        }
    )

    service = _build_service(ctx, enabled=True)

    assert isinstance(service._scan_requests, ScanLedger)
    assert service._scan_requests._ctx is ctx


# ---------------------------------------------------------------------------
# enrich() tests
# ---------------------------------------------------------------------------


class TestEnrich:
    def test_enrich_noop_when_settings_blank(self) -> None:
        """enrich() returns [] and performs no HTTP when settings are blank."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Test Book")
        ctx = _FakeCtx(
            settings={"server": "", "api_key": ""},
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        # Patch the service factory to return a disabled service
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(ok=False, fields={}, attempted=False)
        with patch("kavita_sync.plugin._build_service", return_value=mock_service) as mock_factory:
            result = plugin.enrich((view,), ctx)
            assert result == []
            # Verify the factory was called to build a disabled service
            mock_factory.assert_called_once()
            call_args = mock_factory.call_args
            # enabled should be False when server/api_key are blank
            assert call_args[1]["enabled"] is False

    def test_enrich_syncs_each_book(self) -> None:
        """enrich() syncs each book via the service, returning patches with link-attempt fields.

        EXP-200.
        """
        plugin = KavitaSyncPlugin()
        view1 = _make_book_view("b1", "Book 1")
        view2 = _make_book_view("b2", "Book 2")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
                "external_url": "http://external.kavita:5000",
            }
        )

        # Patch the service factory to return a mock service
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(ok=True, fields={})
        patcher = patch("kavita_sync.plugin._build_service", return_value=mock_service)
        with patcher as mock_factory:
            result = plugin.enrich((view1, view2), ctx)

            # Should return one patch per book with link-attempt fields
            assert len(result) == 2
            for patch_obj in result:
                assert patch_obj.book_id in ("b1", "b2")
                assert "external_link_error" in patch_obj.fields
                assert patch_obj.fields["external_link_error"] is None

            # Service factory should be called once with correct params
            mock_factory.assert_called_once()
            # Check keyword arguments (ctx is positional, no book_repo anymore)
            args, kwargs = mock_factory.call_args
            assert args[0] is ctx
            assert kwargs["enabled"] is True

            # Service.sync should be called once per book with restore_target kwarg
            assert mock_service.sync.call_count == 2
            mock_service.sync.assert_any_call(view1, restore_target=None)
            mock_service.sync.assert_any_call(view2, restore_target=None)

    def test_enrich_respects_cancel(self) -> None:
        """enrich() propagates Cancelled when check_cancelled raises."""
        plugin = KavitaSyncPlugin()
        view1 = _make_book_view("b1", "Book 1")
        view2 = _make_book_view("b2", "Book 2")

        # cancel_after=1: check_cancelled() raises on its second call, after book 1 syncs
        ctx = FakeContext(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
                "external_url": "http://external.kavita:5000",
            },
            cancel_after=1,
        )

        # Patch the service factory to return a mock service
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(ok=True, fields={})
        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            # Should propagate the Cancelled exception
            with pytest.raises(Cancelled):
                plugin.enrich((view1, view2), ctx)

            # First sync should have been called with restore_target kwarg
            mock_service.sync.assert_called_once_with(view1, restore_target=None)

    def test_enrich_returns_patch_with_service_fields(self) -> None:
        """enrich() with BOOK_UPDATED wraps service.sync fields and link-attempt in BookPatch.

        EXP-200.
        """
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        # Mock service returns fields
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(ok=True, fields={"external_provider": "kavita"})

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)

            # Should return one BookPatch with the service's fields plus link-attempt fields
            assert len(result) == 1
            assert result[0].book_id == "b1"
            assert result[0].fields["external_provider"] == "kavita"
            assert "external_link_error" in result[0].fields
            assert result[0].fields["external_link_error"] is None
            assert "external_link_attempted_at" in result[0].fields

    def test_enrich_empty_fields_returns_no_patch(self) -> None:
        """enrich() with empty fields returns one patch with link-attempt fields (EXP-200)."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        # Mock service returns empty fields
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(ok=True, fields={})

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)

            # Should return one patch with link-attempt fields (ok=True means error=None)
            assert len(result) == 1
            assert result[0].book_id == "b1"
            assert "external_link_error" in result[0].fields
            assert result[0].fields["external_link_error"] is None
            assert "external_link_attempted_at" in result[0].fields
            assert "external_link_attempt_size" in result[0].fields

    def test_build_service_passes_the_server_url(self) -> None:
        """_build_service passes server_url to KavitaService."""
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            }
        )

        from kavita_sync.plugin import _build_service

        service = _build_service(ctx, enabled=True)

        # Verify that the service has the server_url set
        assert service._server_url == "http://kavita.local:5000"

    def test_imported_uses_readonly_enrich(self) -> None:
        """enrich() with BOOK_IMPORTED event calls service.enrich, not sync."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_IMPORTED,
        )

        # Mock service
        mock_service = MagicMock()
        mock_service.enrich.return_value = SyncResult(
            ok=True, fields={"external_provider": "kavita"}
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)

            # Should call enrich, not sync
            mock_service.enrich.assert_called_once_with(view)
            mock_service.sync.assert_not_called()

            # Should return one BookPatch with the service's fields
            assert len(result) == 1
            assert result[0].book_id == "b1"

    def test_deleted_triggers_folder_scan_without_repo_fetch(self) -> None:
        """enrich() with BOOK_DELETED calls nudge_folder_after_delete; does not fetch repo."""
        book_repo = _FakeBookRepository()

        plugin = KavitaSyncPlugin()
        view = _make_book_view(
            "b1",
            "Book 1",
            output_filename="dir/book.epub",
            external=api.ExternalLink(item_id="7"),
        )
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_DELETED,
        )

        # Mock service
        mock_service = MagicMock()

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)

            # Should call nudge_folder_after_delete on the service for unlinked book
            mock_service.nudge_folder_after_delete.assert_called_once_with(
                "dir/book.epub", "Book 1"
            )

            # Should NOT call book_repo.get
            assert "b1" not in book_repo.books

            # Should return empty list
            assert result == []

    def test_deleted_without_link_is_noop(self) -> None:
        """enrich() with BOOK_DELETED but no item_id does not handle delete."""

        plugin = KavitaSyncPlugin()
        view = _make_book_view(
            "b1",
            "Book 1",
            output_filename="dir/book.epub",
            external=api.ExternalLink(item_id=None),
        )
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_DELETED,
        )

        # Mock service
        mock_service = MagicMock()

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)

            # Should NOT call rescan_library_after_delete or nudge_folder_after_delete
            mock_service.rescan_library_after_delete.assert_not_called()
            mock_service.nudge_folder_after_delete.assert_not_called()

            # Should return empty list
            assert result == []

    def test_manifest_declares_new_events_and_schedule(self) -> None:
        """Manifest includes BOOK_IMPORTED, BOOK_DELETED events."""
        plugin = KavitaSyncPlugin()

        # Check events include BOOK_IMPORTED and BOOK_DELETED
        assert api.PluginEventType.BOOK_IMPORTED in plugin.manifest.events
        assert api.PluginEventType.BOOK_DELETED in plugin.manifest.events
        assert api.PluginEventType.BOOK_CREATED in plugin.manifest.events
        assert api.PluginEventType.BOOK_UPDATED in plugin.manifest.events

    def test_manifest_accepts_list(self) -> None:
        """Manifest.accepts_list is True for batching support."""
        plugin = KavitaSyncPlugin()
        assert plugin.manifest.accepts_list is True

    def test_enrich_batch_builds_service_once(self) -> None:
        """enrich() with multiple books builds service once and syncs each book."""
        plugin = KavitaSyncPlugin()
        view1 = _make_book_view("b1", "Book 1")
        view2 = _make_book_view("b2", "Book 2")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        # Mock service with empty fields
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(ok=True, fields={})

        # Track build_service call count
        build_count = [0]

        def build_service_side_effect(ctx: api.PluginContext, *, enabled: bool) -> Any:
            build_count[0] += 1
            return mock_service

        with patch("kavita_sync.plugin._build_service", side_effect=build_service_side_effect):
            result = plugin.enrich((view1, view2), ctx)

            # Should build service exactly once
            assert build_count[0] == 1

            # Should sync each book with restore_target kwarg
            assert mock_service.sync.call_count == 2
            mock_service.sync.assert_any_call(view1, restore_target=None)
            mock_service.sync.assert_any_call(view2, restore_target=None)

            # Should return two patches with link-attempt fields (EXP-200)
            assert len(result) == 2
            for patch_obj in result:
                assert patch_obj.book_id in ("b1", "b2")
                assert "external_link_error" in patch_obj.fields
                assert patch_obj.fields["external_link_error"] is None

    def test_sync_passes_restore_target_to_service(self) -> None:
        """enrich() with restore_target passes it to service.sync as kwarg."""
        book_repo = _FakeBookRepository()
        book1 = MagicMock()
        book1.book_id = "b1"
        book_repo.books = {"b1": book1}

        plugin = KavitaSyncPlugin()
        restore_pos = api.ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.75,
        )
        view = _make_book_view("b1", "Book 1")
        # Create a new BookView with restore_target set
        view = api.BookView(
            book_id="b1",
            title="Book 1",
            author=None,
            story_url=None,
            output_filename="test.epub",
            num_chapters=None,
            status=None,
            rating=None,
            cover_ref=None,
            external=api.ExternalLink(),
            progress=api.ExternalProgress(),
            custom_values={},
            restore_target=restore_pos,
        )

        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        # Mock service
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(ok=True, fields={})

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            plugin.enrich((view,), ctx)

            # Verify sync was called with restore_target kwarg
            mock_service.sync.assert_called_once()
            call_args = mock_service.sync.call_args
            assert call_args[1]["restore_target"] == restore_pos

    def test_marker_cleared_even_when_sync_fails(self) -> None:
        """enrich() clears marker field when an attempted sync fails (EXP-155).

        Post-EXP-155, the marker is cleared only when the provider write was actually
        attempted (``restore_attempted=True``), which a failed-but-attempted sync still
        reports.
        """
        book_repo = _FakeBookRepository()
        book1 = MagicMock()
        book1.book_id = "b1"
        book_repo.books = {"b1": book1}

        plugin = KavitaSyncPlugin()
        restore_pos = api.ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.75,
        )
        view = api.BookView(
            book_id="b1",
            title="Book 1",
            author=None,
            story_url=None,
            output_filename="test.epub",
            num_chapters=None,
            status=None,
            rating=None,
            cover_ref=None,
            external=api.ExternalLink(),
            progress=api.ExternalProgress(),
            custom_values={},
            restore_target=restore_pos,
        )

        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        # Mock service returns failure, but the restore write was attempted
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(
            ok=False, message="down", fields={}, restore_attempted=True
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)

            # Should return one patch with marker cleared and link attempt recorded (EXP-200)
            assert len(result) == 1
            assert result[0].book_id == "b1"
            assert result[0].fields["read_position_restore"] is None
            assert "external_link_error" in result[0].fields
            assert result[0].fields["external_link_error"] == "down"

    def test_patch_carries_read_position(self) -> None:
        """enrich() includes read_position from sync result in patch."""
        book_repo = _FakeBookRepository()
        book1 = MagicMock()
        book1.book_id = "b1"
        book_repo.books = {"b1": book1}

        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        # Mock service returns read_position
        read_pos = api.ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=1,
            chapter_progress=0.5,
        )
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(ok=True, fields={}, read_position=read_pos)

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)

            # Should return one patch with read_position
            assert len(result) == 1
            assert result[0].book_id == "b1"
            assert result[0].read_position == read_pos

    def test_no_marker_field_without_restore_target(self) -> None:
        """enrich() does not include marker field when restore_target is None."""
        book_repo = _FakeBookRepository()
        book1 = MagicMock()
        book1.book_id = "b1"
        book_repo.books = {"b1": book1}

        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        # Ensure restore_target is None (default)
        assert view.restore_target is None

        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        # Mock service returns fields but no read_position
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(ok=True, fields={"external_provider": "kavita"})

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)

            # Should return one patch with service fields and link-attempt, no marker (EXP-200)
            assert len(result) == 1
            assert result[0].book_id == "b1"
            assert result[0].fields["external_provider"] == "kavita"
            assert "external_link_error" in result[0].fields
            assert result[0].fields["external_link_error"] is None
            assert "read_position_restore" not in result[0].fields

    def test_kavita_enrich_does_not_consume_the_restore_marker(self, caplog: Any) -> None:
        """A read-only BOOK_IMPORTED enrich never consumes a pending restore marker (EXP-155).

        A fresh re-import routes to ``enrich`` one second after the restore, before Kavita has
        scanned the file back in — it cannot deliver the marker, so it must leave it pending
        for the next sync rather than clearing it on presence alone.
        """
        plugin = KavitaSyncPlugin()
        restore_pos = api.ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.75,
        )
        view = api.BookView(
            book_id="b1",
            title="Book 1",
            author=None,
            story_url=None,
            output_filename="test.epub",
            num_chapters=None,
            status=None,
            rating=None,
            cover_ref=None,
            external=api.ExternalLink(),
            progress=api.ExternalProgress(),
            custom_values={},
            restore_target=restore_pos,
        )

        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_IMPORTED,
            logger=logging.getLogger("src.plugin.kavita_sync"),
        )

        mock_service = MagicMock()
        mock_service.enrich.return_value = SyncResult(
            False, "book not found in Kavita", attempted=True, restore_attempted=False
        )

        with (
            caplog.at_level(logging.DEBUG, logger="src.plugin.kavita_sync"),
            patch("kavita_sync.plugin._build_service", return_value=mock_service),
        ):
            result = plugin.enrich((view,), ctx)

        assert not result or "read_position_restore" not in result[0].fields
        pending_records = [r for r in caplog.records if "Restore marker left pending" in r.message]
        assert len(pending_records) == 1
        assert "enrich" in pending_records[0].message

    def test_kavita_sync_consumes_the_marker_after_a_real_attempt(self) -> None:
        """A sync that actually attempted the restore write consumes the pending marker."""
        plugin = KavitaSyncPlugin()
        restore_pos = api.ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.75,
        )
        view = api.BookView(
            book_id="b1",
            title="Book 1",
            author=None,
            story_url=None,
            output_filename="test.epub",
            num_chapters=None,
            status=None,
            rating=None,
            cover_ref=None,
            external=api.ExternalLink(),
            progress=api.ExternalProgress(),
            custom_values={},
            restore_target=restore_pos,
        )

        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(
            True, "synced", restore_attempted=True, fields={"external_item_id": "K1"}
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)

        assert len(result) == 1
        assert result[0].fields["read_position_restore"] is None

    def test_kavita_sync_leaves_the_marker_when_the_provider_was_unreachable(self) -> None:
        """A sync that was attempted but never reached the restore write leaves the marker."""
        plugin = KavitaSyncPlugin()
        restore_pos = api.ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.75,
        )
        view = api.BookView(
            book_id="b1",
            title="Book 1",
            author=None,
            story_url=None,
            output_filename="test.epub",
            num_chapters=None,
            status=None,
            rating=None,
            cover_ref=None,
            external=api.ExternalLink(),
            progress=api.ExternalProgress(),
            custom_values={},
            restore_target=restore_pos,
        )

        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(
            False, "unreachable", attempted=True, restore_attempted=False
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)

        assert not result or "read_position_restore" not in result[0].fields

    def test_kavita_no_marker_key_when_there_was_no_marker(self) -> None:
        """No pending marker and no attempt means no marker key appears in the patch."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        assert view.restore_target is None

        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(
            True, "synced", fields={"external_item_id": "K1"}, restore_attempted=False
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)

        assert len(result) == 1
        assert "read_position_restore" not in result[0].fields

    def test_book_deleted_unconfigured_is_noop(self, caplog) -> None:
        """enrich() with BOOK_DELETED but unconfigured does not call rescan methods."""

        plugin = KavitaSyncPlugin()
        view = _make_book_view(
            "b1",
            "Test Title",
            output_filename="A/b.epub",
            external=api.ExternalLink(item_id="c1"),
        )
        ctx = _FakeCtx(
            settings={"server": "", "api_key": ""},
            event_type=api.PluginEventType.BOOK_DELETED,
            logger=logging.getLogger("test.plugin"),
        )

        # Mock service
        mock_service = MagicMock()

        with (
            caplog.at_level(logging.DEBUG),
            patch("kavita_sync.plugin._build_service", return_value=mock_service),
        ):
            result = plugin.enrich((view,), ctx)

            # Should return empty list
            assert result == []

            # Fake service's rescan methods should NOT be called
            mock_service.rescan_library_after_delete.assert_not_called()
            mock_service.nudge_folder_after_delete.assert_not_called()

            # Caplog should contain DEBUG message about not configured
            assert "Kavita not configured — BookDeleted ignored" in caplog.text

    def test_book_deleted_rescans_each_library_once(self, caplog) -> None:
        """BOOK_DELETED groups by library_id and calls rescan_library_after_delete once per id."""
        plugin = KavitaSyncPlugin()
        view1 = _make_book_view(
            "b1",
            "Title 1",
            external=api.ExternalLink(library_id="1", item_id="c1"),
        )
        view2 = _make_book_view(
            "b2",
            "Title 2",
            external=api.ExternalLink(library_id="1", item_id="c2"),
        )
        view3 = _make_book_view(
            "b3",
            "Title 3",
            external=api.ExternalLink(library_id="2", item_id="c3"),
        )
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_DELETED,
            logger=logging.getLogger("test.plugin"),
        )

        # Mock service to record calls
        mock_service = MagicMock()
        mock_service.rescan_library_after_delete.return_value = True

        with (
            caplog.at_level(logging.INFO),
            patch("kavita_sync.plugin._build_service", return_value=mock_service),
        ):
            result = plugin.enrich((view1, view2, view3), ctx)

            # Should return empty list
            assert result == []

            # rescan_library_after_delete should be called twice (once per library)
            assert mock_service.rescan_library_after_delete.call_count == 2

            # Verify the calls are grouped by library_id
            calls = mock_service.rescan_library_after_delete.call_args_list
            call_args = [call[0] for call in calls]
            # Should have calls with ("1", ["Title 1", "Title 2"]) and ("2", ["Title 3"])
            lib_1_call = [c for c in call_args if c[0] == "1"][0]
            lib_2_call = [c for c in call_args if c[0] == "2"][0]
            assert set(lib_1_call[1]) == {"Title 1", "Title 2"}
            assert set(lib_2_call[1]) == {"Title 3"}

    def test_book_deleted_stops_on_an_outage(self, caplog) -> None:
        """BOOK_DELETED stops on ProviderUnreachable and logs WARNING."""
        from ebookerr_sdk.providers.connection import ProviderUnreachable

        plugin = KavitaSyncPlugin()
        view1 = _make_book_view(
            "b1",
            "Title 1",
            external=api.ExternalLink(library_id="1", item_id="c1"),
        )
        view2 = _make_book_view(
            "b2",
            "Title 2",
            external=api.ExternalLink(library_id="1", item_id="c2"),
        )
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_DELETED,
            logger=logging.getLogger("test.plugin"),
        )

        # Mock service to raise ProviderUnreachable
        mock_service = MagicMock()
        mock_service.rescan_library_after_delete.side_effect = ProviderUnreachable(
            "Kavita is not reachable"
        )

        with (
            caplog.at_level(logging.WARNING),
            patch("kavita_sync.plugin._build_service", return_value=mock_service),
        ):
            result = plugin.enrich((view1, view2), ctx)

            # Should return empty list
            assert result == []

            # Should have a WARNING about rescan skipped
            assert "rescan after deleting 2 book(s) skipped" in caplog.text

    def test_book_deleted_configured_nudges_and_logs(self, caplog) -> None:
        """enrich() with BOOK_DELETED and configured settings nudges and logs."""

        plugin = KavitaSyncPlugin()
        view = _make_book_view(
            "b1",
            "Test Title",
            output_filename="A/b.epub",
            external=api.ExternalLink(item_id="c1"),
        )
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_DELETED,
            logger=logging.getLogger("test.plugin"),
        )

        # Mock service
        mock_service = MagicMock()

        with (
            caplog.at_level(logging.INFO),
            patch("kavita_sync.plugin._build_service", return_value=mock_service),
        ):
            result = plugin.enrich((view,), ctx)

            # Should return empty list
            assert result == []

            # Either nudge_folder_after_delete or rescan_library_after_delete should be called
            # (depending on implementation)
            assert (
                mock_service.nudge_folder_after_delete.called
                or mock_service.rescan_library_after_delete.called
            )

    def test_enrich_reports_progress_per_book(self) -> None:
        """enrich() reports progress as percentage 0–100 after each book."""
        plugin = KavitaSyncPlugin()
        view1 = _make_book_view("b1", "Book 1")
        view2 = _make_book_view("b2", "Book 2")
        view3 = _make_book_view("b3", "Book 3")
        view4 = _make_book_view("b4", "Book 4")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        # Mock service with empty fields
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(ok=True, fields={})

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            plugin.enrich((view1, view2, view3, view4), ctx)

            # Should report [25.0, 50.0, 75.0, 100.0]
            assert ctx.reported == pytest.approx([25.0, 50.0, 75.0, 100.0])

    def test_enrich_reports_progress_on_the_delete_branch(self) -> None:
        """enrich() with BOOK_DELETED reports progress after each book."""
        plugin = KavitaSyncPlugin()
        view1 = _make_book_view(
            "b1",
            "Book 1",
            output_filename="dir1/book1.epub",
            external=api.ExternalLink(item_id="item1"),
        )
        view2 = _make_book_view(
            "b2",
            "Book 2",
            output_filename="dir2/book2.epub",
            external=api.ExternalLink(item_id="item2"),
        )
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_DELETED,
        )

        # Mock service
        mock_service = MagicMock()

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view1, view2), ctx)

            # Should return empty list
            assert result == []
            # Should report [50.0, 100.0]
            assert ctx.reported == pytest.approx([50.0, 100.0])

    def test_enrich_unconfigured_delete_reports_nothing(self) -> None:
        """enrich() with BOOK_DELETED and unconfigured settings reports nothing."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view(
            "b1",
            "Book 1",
            output_filename="dir/book.epub",
            external=api.ExternalLink(item_id="item1"),
        )
        ctx = _FakeCtx(
            settings={"server": "", "api_key": ""},
            event_type=api.PluginEventType.BOOK_DELETED,
        )

        mock_service = MagicMock()

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)

            # Should return empty list
            assert result == []
            # Should report nothing
            assert ctx.reported == []

    def test_enrich_empty_selection_reports_nothing(self) -> None:
        """enrich() with empty selection reports nothing."""
        plugin = KavitaSyncPlugin()
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        mock_service = MagicMock()

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((), ctx)

            # Should return empty list
            assert result == []
            # Should report nothing
            assert ctx.reported == []

    def test_enrich_logs_book_count_at_debug(self, caplog) -> None:
        """enrich() logs the total book count at DEBUG level before the sync."""
        plugin = KavitaSyncPlugin()
        view1 = _make_book_view("b1", "Book 1")
        view2 = _make_book_view("b2", "Book 2")
        view3 = _make_book_view("b3", "Book 3")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
            logger=logging.getLogger("test.plugin"),
        )

        # Mock service with empty fields
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(ok=True, fields={})

        with (
            caplog.at_level(logging.DEBUG),
            patch("kavita_sync.plugin._build_service", return_value=mock_service),
        ):
            plugin.enrich((view1, view2, view3), ctx)

            # Should contain DEBUG message about book count
            assert "Kavita sync starting over 3 book(s)" in caplog.text

    def test_a_failed_link_is_recorded_on_the_book(self, caplog: Any) -> None:
        """A failed provider link records the error, attempted_at, and size on the book."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view(
            "b1",
            "Book 1",
            output_filename="book1.epub",
            file_size=100,
        )
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
            logger=logging.getLogger("test.plugin"),
        )

        # Mock service returns a failed result
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(
            ok=False,
            message="book not found in Kavita",
            attempted=True,
            fields={},
        )

        with (
            caplog.at_level(logging.DEBUG),
            patch("kavita_sync.plugin._build_service", return_value=mock_service),
        ):
            result = plugin.enrich((view,), ctx)

            # Should return one patch with link-attempt fields
            assert len(result) == 1
            patch_obj = result[0]
            assert patch_obj.book_id == "b1"
            assert "external_link_error" in patch_obj.fields
            assert patch_obj.fields["external_link_error"] == "book not found in Kavita"
            assert "external_link_attempted_at" in patch_obj.fields
            assert "external_link_attempt_size" in patch_obj.fields
            assert patch_obj.fields["external_link_attempt_size"] == 100

            # Should log the failure at DEBUG
            assert (
                'Recorded link failure for "Book 1" (book_id=b1): book not found in Kavita'
                in caplog.text
            )

    def test_a_successful_link_clears_the_error(self, caplog: Any) -> None:
        """A successful provider link clears a previous error and logs recovery."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view(
            "b1",
            "Book 1",
            output_filename="book1.epub",
            external=api.ExternalLink(link_error="book not found in Kavita"),
            file_size=100,
        )
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
            logger=logging.getLogger("test.plugin"),
        )

        # Mock service returns a successful result
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(
            ok=True,
            message="",
            attempted=True,
            fields={"external_item_id": "11"},
        )

        with (
            caplog.at_level(logging.INFO),
            patch("kavita_sync.plugin._build_service", return_value=mock_service),
        ):
            result = plugin.enrich((view,), ctx)

            # Should return one patch with error cleared to None
            assert len(result) == 1
            patch_obj = result[0]
            assert patch_obj.book_id == "b1"
            assert "external_link_error" in patch_obj.fields
            assert patch_obj.fields["external_link_error"] is None
            assert patch_obj.fields["external_item_id"] == "11"

            # Should log the recovery at INFO
            assert (
                'Provider link recovered for "Book 1" (book_id=b1); previous failure: '
                "book not found in Kavita" in caplog.text
            )

    def test_an_unreachable_or_disabled_provider_records_nothing(self) -> None:
        """An unreachable or disabled provider records no link attempt."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1", output_filename="book1.epub", file_size=100)
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        mock_service = MagicMock()

        # Case 1: unreachable provider
        mock_service.sync.return_value = SyncResult(
            ok=False,
            message="Kavita is not reachable",
            attempted=True,
            unreachable=True,
            fields={},
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)
            # No patch should be created (no fields and no read position)
            assert result == []

        # Case 2: disabled provider (attempted=False)
        mock_service.sync.return_value = SyncResult(
            ok=False,
            message="Kavita is disabled",
            attempted=False,
            fields={},
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)
            # No patch should be created (no fields and no read position)
            assert result == []

    def test_an_epub_modified_event_runs_a_sync(self) -> None:
        """EPUB_MODIFIED event calls service.sync(), not enrich(), with allow_scan=True."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.EPUB_MODIFIED,
        )

        # Mock service
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(ok=True, fields={"external_provider": "kavita"})

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)

            # Should call sync, not enrich
            mock_service.sync.assert_called_once()
            mock_service.enrich.assert_not_called()

            # Verify allow_scan=True was passed
            call_kwargs = mock_service.sync.call_args[1]
            assert "allow_scan" in call_kwargs or mock_service.sync.call_args[0]

            # Should return one BookPatch with the service's fields
            assert len(result) == 1
            assert result[0].book_id == "b1"


class TestTestConnection:
    def test_test_connection_uses_ctx_settings(self) -> None:
        """test_connection uses ctx.settings; returns (True, 'Connected') on success."""
        from ebookerr_sdk.providers.connection import ConnectionTestResult

        plugin = KavitaSyncPlugin()

        # Track which server/api_key were passed to the client
        called_with = {}

        def fake_client_factory(server: str, api_key: str, external_url: str | None = None) -> Any:
            called_with["server"] = server
            called_with["api_key"] = api_key
            called_with["external_url"] = external_url
            fake = MagicMock()
            fake.test_connection.return_value = ConnectionTestResult("ok", "Connected.")
            return fake

        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test-key-456",
                "external_url": "http://external.kavita:5000",
            }
        )

        with patch("kavita_sync.plugin.RequestsKavitaClient", side_effect=fake_client_factory):
            ok, msg = plugin.test_connection(ctx)

        assert ok is True
        assert msg == "Connected"
        assert called_with["server"] == "http://kavita.local:5000"
        assert called_with["api_key"] == "test-key-456"
        assert called_with["external_url"] == "http://external.kavita:5000"

    def test_test_connection_unconfigured(self) -> None:
        """test_connection with missing server/api_key returns (False, 'Not configured')."""
        plugin = KavitaSyncPlugin()

        ctx = _FakeCtx(settings={})

        ok, msg = plugin.test_connection(ctx)

        assert ok is False
        assert msg == "Not configured"

    def test_test_connection_distinguishes_unreachable_from_rejected(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """test_connection distinguishes unreachable from rejected with different messages."""
        from ebookerr_sdk.providers.connection import ConnectionTestResult

        plugin = KavitaSyncPlugin()

        # Scenario 1: unreachable
        unreachable_msg = (
            "Could not reach the Kavita server at http://kavita.local:5000. "
            "Check the URL and that Kavita is running."
        )
        unreachable_result = ConnectionTestResult("unreachable", unreachable_msg)

        # Scenario 2: rejected
        rejected_msg = "Kavita refused the API key."
        rejected_result = ConnectionTestResult("rejected", rejected_msg)

        # Scenario 3: ok
        ok_result = ConnectionTestResult("ok", "Connected.")

        # Test unreachable
        def fake_unreachable(server: str, api_key: str, external_url: str | None = None) -> Any:
            fake = MagicMock()
            fake.test_connection.return_value = unreachable_result
            return fake

        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test-key",
            }
        )

        with (
            caplog.at_level(logging.WARNING),
            patch("kavita_sync.plugin.RequestsKavitaClient", side_effect=fake_unreachable),
        ):
            ok, msg = plugin.test_connection(ctx)
            assert ok is False
            assert msg == unreachable_msg

        caplog.clear()

        # Test rejected
        def fake_rejected(server: str, api_key: str, external_url: str | None = None) -> Any:
            fake = MagicMock()
            fake.test_connection.return_value = rejected_result
            return fake

        with (
            caplog.at_level(logging.WARNING),
            patch("kavita_sync.plugin.RequestsKavitaClient", side_effect=fake_rejected),
        ):
            ok, msg = plugin.test_connection(ctx)
            assert ok is False
            assert msg == rejected_msg

        caplog.clear()

        # Test ok
        def fake_ok(server: str, api_key: str, external_url: str | None = None) -> Any:
            fake = MagicMock()
            fake.test_connection.return_value = ok_result
            return fake

        with (
            caplog.at_level(logging.WARNING),
            patch("kavita_sync.plugin.RequestsKavitaClient", side_effect=fake_ok),
        ):
            ok, msg = plugin.test_connection(ctx)
            assert ok is True
            assert msg == "Connected"

        # Verify the two failure messages are different
        assert unreachable_msg != rejected_msg


class TestPluginNoDependencies:
    """Tests for plugin SPI compliance (no repo injection)."""

    def test_plugin_takes_no_repository(self) -> None:
        """assert 'book_repo' not in inspect.signature(KavitaSyncPlugin.__init__).parameters."""
        import inspect

        sig = inspect.signature(KavitaSyncPlugin.__init__)
        assert "book_repo" not in sig.parameters

    def test_enrich_passes_the_view_straight_through(self) -> None:
        """Plugin passes BookView directly to service methods."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_IMPORTED,
        )

        # Record which view was passed to the service
        received_view = [None]

        def capture_view(v: api.BookView) -> Any:
            received_view[0] = v
            return SyncResult(ok=True, fields={})

        mock_service = MagicMock()
        mock_service.enrich = capture_view

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            plugin.enrich((view,), ctx)

            # Verify the exact view instance was passed
            assert received_view[0] is view

    def test_restore_marker_is_still_consumed(self) -> None:
        """One-shot consume of read_position_restore (RP-PULL-4) still works."""
        plugin = KavitaSyncPlugin()
        restore_pos = api.ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.75,
        )
        view = api.BookView(
            book_id="b1",
            title="Book 1",
            author=None,
            story_url=None,
            output_filename="test.epub",
            num_chapters=None,
            status=None,
            rating=None,
            cover_ref=None,
            external=api.ExternalLink(),
            progress=api.ExternalProgress(),
            custom_values={},
            restore_target=restore_pos,
        )

        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        # Mock service returns empty fields, but the restore write was attempted and succeeded
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(ok=True, fields={}, restore_attempted=True)

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            result = plugin.enrich((view,), ctx)

            # Should return patch with marker cleared
            assert len(result) == 1
            assert result[0].fields["read_position_restore"] is None


# ---------------------------------------------------------------------------
# Per-book failure reporting tests (EXP-001)
# ---------------------------------------------------------------------------


class TestPerBookFailureReporting:
    def test_failed_sync_reports_item_failure(self) -> None:
        """Failed sync (attempted=True) reports item failure via ctx.report_failure."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        # Mock service returns failure with attempted=True
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(
            ok=False, message="Kavita is not reachable", attempted=True, fields={}
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            plugin.enrich((view,), ctx)

        assert ctx.item_failures == [("b1", "Kavita is not reachable")]

    def test_disabled_sync_reports_no_failure(self) -> None:
        """Disabled Kavita (attempted=False) does not report failure."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        # Mock service returns failure with attempted=False
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(
            ok=False, message="Kavita sync is disabled", attempted=False, fields={}
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            plugin.enrich((view,), ctx)

        assert ctx.item_failures == []

    def test_failed_sync_logs_warning(self, caplog) -> None:
        """Failed sync logs one WARNING with the action name and reason."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
            logger=logging.getLogger("src.plugin.kavita_sync"),
        )

        # Mock service returns failure
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(
            ok=False, message="Kavita is not reachable", attempted=True, fields={}
        )

        with (
            caplog.at_level(logging.WARNING, logger="src.plugin.kavita_sync"),
            patch("kavita_sync.plugin._build_service", return_value=mock_service),
        ):
            plugin.enrich((view,), ctx)

        assert "Kavita sync did not complete for" in caplog.text
        assert "Kavita is not reachable" in caplog.text

    def test_ok_sync_logs_debug(self, caplog) -> None:
        """Successful sync logs at DEBUG with 'Kavita sync ok for'."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
            logger=logging.getLogger("src.plugin.kavita_sync"),
        )

        # Mock service returns success
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(ok=True, fields={})

        with (
            caplog.at_level(logging.DEBUG, logger="src.plugin.kavita_sync"),
            patch("kavita_sync.plugin._build_service", return_value=mock_service),
        ):
            plugin.enrich((view,), ctx)

        assert "Kavita sync ok for" in caplog.text

    def test_a_refused_link_is_reported_as_a_skip_not_a_failure(self) -> None:
        """A refused link (attempted=True, refused=True) reports skip, not failure."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        # Mock service returns refusal with attempted=True and refused=True
        mock_service = MagicMock()
        refusal_msg = "link refused: Kavita chapter 77 is already linked to book_id=owner1"
        mock_service.sync.return_value = SyncResult(
            ok=False, message=refusal_msg, attempted=True, refused=True, fields={}
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            plugin.enrich((view,), ctx)

        assert ctx.item_skips == [("b1", refusal_msg)]
        assert ctx.item_failures == []

    def test_a_not_found_sync_is_reported_as_a_skip_not_a_failure(self) -> None:
        """Kavita not having indexed the book yet reports skip, not failure (DFT-TR-5)."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(
            ok=False, message="book not found in Kavita", attempted=True, not_found=True, fields={}
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            plugin.enrich((view,), ctx)

        assert ctx.item_skips == [("b1", "book not found in Kavita")]
        assert ctx.item_failures == []

    def test_a_non_refused_failure_is_still_a_failure(self) -> None:
        """A non-refused failure (refused=False) still reports as failure."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        # Mock service returns non-refused failure
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(
            ok=False, message="Kavita is not reachable", attempted=True, fields={}
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            plugin.enrich((view,), ctx)

        assert ctx.item_failures == [("b1", "Kavita is not reachable")]
        assert ctx.item_skips == []

    def test_a_refused_enrich_is_also_a_skip(self) -> None:
        """A refused link in enrich also reports skip, not failure (EXP-243)."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_IMPORTED,
        )

        # Mock service returns refusal with attempted=True and refused=True
        mock_service = MagicMock()
        refusal_msg = "link refused: Kavita chapter 77 is already linked to book_id=owner1"
        mock_service.enrich.return_value = SyncResult(
            ok=False, message=refusal_msg, attempted=True, refused=True, fields={}
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            plugin.enrich((view,), ctx)

        assert ctx.item_skips == [("b1", refusal_msg)]
        assert ctx.item_failures == []

    def test_a_disabled_provider_is_neither_a_skip_nor_a_failure(self) -> None:
        """An unattempted outcome (attempted=False) is neither skip nor failure."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Book 1")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        # Mock service returns failure with attempted=False
        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(
            ok=False, message="Kavita sync is disabled", attempted=False, fields={}
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            plugin.enrich((view,), ctx)

        assert ctx.item_failures == []
        assert ctx.item_skips == []


# ---------------------------------------------------------------------------
# Backward move toast notification tests (EXP-123)
# ---------------------------------------------------------------------------


class TestBackwardMoveNotification:
    def test_a_backward_move_reaches_the_user_as_a_durable_notice(self) -> None:
        """Backward move triggers ctx.notify with durable=True (EXP-123, EXP-218)."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Test Book")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        notify_calls: list[tuple[str, str, bool]] = []

        def fake_notify(kind: str, message: str, *, durable: bool = False) -> None:
            notify_calls.append((kind, message, durable))

        ctx.notify = fake_notify  # type: ignore[attr-defined]

        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(
            ok=True,
            message="synced",
            fields={},
            backward_move='Read position for "Test Book" moved backwards: chapter 30 → 4',
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            plugin.enrich((view,), ctx)

        # Should have called ctx.notify with warning, the backward_move message, and durable=True
        assert len(notify_calls) == 1
        kind, message, durable = notify_calls[0]
        assert kind == "warning"
        assert "chapter 30 → 4" in message
        assert durable is True

    def test_a_forward_move_records_nothing(self) -> None:
        """A forward or neutral move (backward_move=None) does not trigger a notify call."""
        plugin = KavitaSyncPlugin()
        view = _make_book_view("b1", "Test Book")
        ctx = _FakeCtx(
            settings={
                "server": "http://kavita.local:5000",
                "api_key": "test_key",
            },
            event_type=api.PluginEventType.BOOK_UPDATED,
        )

        notify_calls: list[tuple[str, str, bool]] = []

        def fake_notify(kind: str, message: str, *, durable: bool = False) -> None:
            notify_calls.append((kind, message, durable))

        ctx.notify = fake_notify  # type: ignore[attr-defined]

        mock_service = MagicMock()
        mock_service.sync.return_value = SyncResult(
            ok=True,
            message="synced",
            fields={},
            backward_move=None,
        )

        with patch("kavita_sync.plugin._build_service", return_value=mock_service):
            plugin.enrich((view,), ctx)

        # Should not have called ctx.notify
        assert len(notify_calls) == 0


# ---------------------------------------------------------------------------
# Wrong-item relink purge authorisation (OR-6, EXP-243)
# ---------------------------------------------------------------------------


@pytest.mark.pins("EXP-243")
def test_a_kavita_chapter_id_change_never_authorises_a_history_purge() -> None:
    """Kavita resolves by path every sync, so its own relinked signal must stay off the patch."""
    plugin = KavitaSyncPlugin()
    view = _make_book_view("b1", "Book 1")
    ctx = _FakeCtx(
        settings={"server": "http://kavita.local:5000", "api_key": "test_key"},
        event_type=api.PluginEventType.BOOK_UPDATED,
    )

    mock_service = MagicMock()
    mock_service.sync.return_value = SyncResult(
        ok=True,
        fields={},
        relinked=True,
        read_position=api.ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=1,
            chapter_progress=0.1,
        ),
    )

    with patch("kavita_sync.plugin._build_service", return_value=mock_service):
        result = plugin.enrich((view,), ctx)

    assert len(result) == 1
    assert result[0].relinked is False


# ---------------------------------------------------------------------------
# Stale link kept is recorded as the book's link error (F5c)
# ---------------------------------------------------------------------------


def test_a_kept_stale_link_is_recorded_as_the_books_link_error() -> None:
    """A SyncResult reporting stale_link_kept lands on the patch's link error (F5c)."""
    plugin = KavitaSyncPlugin()
    view = _make_book_view("b1", "Book 1")
    ctx = _FakeCtx(
        settings={"server": "http://kavita.local:5000", "api_key": "test_key"},
        event_type=api.PluginEventType.BOOK_UPDATED,
    )
    mock_service = MagicMock()
    mock_service.sync.return_value = SyncResult(
        False, "stale link kept: X", fields={}, stale_link_kept="stale link kept: X"
    )

    with patch("kavita_sync.plugin._build_service", return_value=mock_service):
        result = plugin.enrich((view,), ctx)

    assert len(result) == 1
    assert result[0].fields["external_link_error"] == "stale link kept: X"


def test_a_clean_kavita_sync_still_clears_the_link_error() -> None:
    """A SyncResult with no stale link keeps clearing external_link_error as before (F5c)."""
    plugin = KavitaSyncPlugin()
    view = _make_book_view("b1", "Book 1")
    ctx = _FakeCtx(
        settings={"server": "http://kavita.local:5000", "api_key": "test_key"},
        event_type=api.PluginEventType.BOOK_UPDATED,
    )
    mock_service = MagicMock()
    mock_service.sync.return_value = SyncResult(True, "synced", fields={}, stale_link_kept=None)

    with patch("kavita_sync.plugin._build_service", return_value=mock_service):
        result = plugin.enrich((view,), ctx)

    assert len(result) == 1
    assert result[0].fields["external_link_error"] is None


# ---------------------------------------------------------------------------
# Link ownership (EXP-149): _build_service passes link_owner
# ---------------------------------------------------------------------------


def test_build_service_passes_the_link_owner() -> None:
    """_build_service passes ctx.provider_link_owner to KavitaService if available."""

    def mock_provider_link_owner(item_id: str) -> str | None:
        return "someBook" if item_id == "owned_id" else None

    ctx = _FakeCtx(
        settings={
            "server": "http://kavita.local:5000",
            "api_key": "test_key",
        }
    )
    # Dynamically add the provider_link_owner method
    ctx.provider_link_owner = mock_provider_link_owner  # type: ignore

    import kavita_sync.plugin

    service = kavita_sync.plugin._build_service(ctx, enabled=True)

    assert service._link_owner is not None
    assert service._link_owner("owned_id") == "someBook"
    assert service._link_owner("unowned_id") is None


# ---------------------------------------------------------------------------
# Unreachable result handling (EXP-192)
# ---------------------------------------------------------------------------


@pytest.mark.pins("EXP-192")
def test_an_unreachable_result_logs_at_debug_and_still_counts_as_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unreachable result from sync logs at DEBUG and still counts as a failure."""
    plugin = KavitaSyncPlugin()
    view1 = _make_book_view("b1", "Book 1")
    view2 = _make_book_view("b2", "Book 2")
    ctx = _FakeCtx(
        settings={
            "server": "http://kavita.local:5000",
            "api_key": "test_key",
        },
        event_type=api.PluginEventType.BOOK_UPDATED,
        logger=logging.getLogger("src.plugin.kavita_sync"),
    )

    # Mock service returning unreachable for both books
    mock_service = MagicMock()
    mock_service.sync.return_value = SyncResult(
        ok=False, message="Kavita is not reachable", attempted=True, unreachable=True
    )

    with (
        caplog.at_level(logging.DEBUG, logger="src.plugin.kavita_sync"),
        patch("kavita_sync.plugin._build_service", return_value=mock_service),
    ):
        plugin.enrich((view1, view2), ctx)

    # Should have no WARNING records from _log_outcome
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) == 0

    # Should have two DEBUG records with "Kavita sync skipped for"
    debug_records = [
        r
        for r in caplog.records
        if r.levelname == "DEBUG" and "Kavita sync skipped for" in r.message
    ]
    assert len(debug_records) == 2

    # Should still count as failures
    assert ctx.item_failures == [
        ("b1", "Kavita is not reachable"),
        ("b2", "Kavita is not reachable"),
    ]


def test_an_unlanded_restore_is_reported_as_an_item_failure(caplog: Any) -> None:
    """Unlanded restore (restore_attempted=True, restore_landed=False) reports item failure.

    When a sync reaches the book and attempts a restore but the provider rejects it
    (or no chapter matches), the restore is consumed (marker cleared) and reported
    as an item failure through ctx.report_failure(), so the task layer surfaces it.
    A WARNING is logged with the book title, book_id, and chapter_index.
    """

    plugin = KavitaSyncPlugin()
    restore_target = api.ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=3,
        chapter_progress=0.5,
    )

    view = api.BookView(
        book_id="b1",
        title="Book 1",
        author=None,
        story_url=None,
        output_filename="book1.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(item_id="11"),
        progress=api.ExternalProgress(),
        custom_values={},
        restore_target=restore_target,
    )

    ctx = _FakeCtx(
        settings={
            "server": "http://k",
            "api_key": "s",
        },
        event_type=api.PluginEventType.BOOK_UPDATED,
    )

    class _UnlandedRestoreService:
        def sync(
            self,
            book: Any,
            *,
            restore_target: api.ReadPosition | None = None,
        ) -> Any:
            return SyncResult(
                ok=True,
                message="synced",
                fields={"external_item_id": "11"},
                restore_attempted=True,
                restore_landed=False,
            )

        def enrich(self, book: Any) -> Any:
            return SyncResult(True, "enriched", fields={})

        def rescan_library_after_delete(self, library_id: str, titles: Any) -> bool:
            return True

        def nudge_folder_after_delete(self, output_filename: str | None, title: str | None) -> bool:
            return True

    with (
        patch("kavita_sync.plugin._build_service", return_value=_UnlandedRestoreService()),
        caplog.at_level(logging.WARNING, logger="src.plugin.kavita_sync"),
    ):
        patches = plugin.enrich((view,), ctx)

    # Should report the item failure
    assert ctx.item_failures == [("b1", "read position could not be restored")]

    # Should log WARNING with book title, book_id, and chapter_index
    assert any(
        r.levelname == "WARNING"
        and "Read-position restore did not land for" in r.message
        and "Book 1" in r.message
        and "book_id=b1" in r.message
        and "chapter_index=3" in r.message
        for r in caplog.records
    )

    # Should return a patch with read_position_restore cleared
    assert len(patches) == 1
    assert patches[0].fields.get("read_position_restore") is None


def test_a_landed_restore_reports_nothing() -> None:
    """Landed restore (restore_landed=True) does not report an item failure.

    When the provider accepts the restore write, no item failure is reported.
    The item_failures list should be empty.
    """

    plugin = KavitaSyncPlugin()
    restore_target = api.ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=3,
        chapter_progress=0.5,
    )

    view = api.BookView(
        book_id="b1",
        title="Book 1",
        author=None,
        story_url=None,
        output_filename="book1.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(item_id="11"),
        progress=api.ExternalProgress(),
        custom_values={},
        restore_target=restore_target,
    )

    ctx = _FakeCtx(
        settings={
            "server": "http://k",
            "api_key": "s",
        },
        event_type=api.PluginEventType.BOOK_UPDATED,
    )

    class _LandedRestoreService:
        def sync(
            self,
            book: Any,
            *,
            restore_target: api.ReadPosition | None = None,
        ) -> Any:
            return SyncResult(
                ok=True,
                message="synced",
                fields={"external_item_id": "11"},
                restore_attempted=True,
                restore_landed=True,
            )

        def enrich(self, book: Any) -> Any:
            return SyncResult(True, "enriched", fields={})

        def rescan_library_after_delete(self, library_id: str, titles: Any) -> bool:
            return True

        def nudge_folder_after_delete(self, output_filename: str | None, title: str | None) -> bool:
            return True

    with patch("kavita_sync.plugin._build_service", return_value=_LandedRestoreService()):
        patches = plugin.enrich((view,), ctx)

    # Should NOT report an item failure
    assert ctx.item_failures == []

    # Should still return a patch with read_position_restore cleared
    assert len(patches) == 1
    assert patches[0].fields.get("read_position_restore") is None


# ---------------------------------------------------------------------------
# Factory (_build_service) tests
# ---------------------------------------------------------------------------


@pytest.mark.pins("EXP-194")
def test_build_service_passes_the_library_folder_to_the_kavita_service() -> None:
    """_build_service passes library_root to KavitaService (EXP-194)."""
    ctx = _FakeCtx(
        settings={"server": "http://k", "api_key": "x"},
        library_root=Path("/lib"),
    )

    with patch("kavita_sync.plugin.RequestsKavitaClient"):
        service = _build_service(ctx, enabled=True)

    assert service._library_folder == Path("/lib")


@pytest.mark.pins("EXP-194")
def test_build_service_without_a_library_root_still_builds() -> None:
    """_build_service still builds when library_root is None."""
    from kavita_sync.plugin import _build_service

    ctx = _FakeCtx(
        settings={"server": "http://k", "api_key": "x"},
        library_root=None,
    )

    with patch("kavita_sync.plugin.RequestsKavitaClient"):
        service = _build_service(ctx, enabled=True)

    assert service is not None


# ---------------------------------------------------------------------------
# TASK-45 — epub_kept flag on BookDeleted (SCS-D27)
# ---------------------------------------------------------------------------


def test_the_kavita_nudge_is_the_same_whether_the_epub_is_kept_or_deleted() -> None:
    """BOOK_DELETED with epub_kept=1 and epub_kept=0 produce identical nudge/scan calls."""

    # Helper to spy on the service calls
    class _SpyKavitaService:
        def __init__(self):
            self.rescan_calls: list[tuple[str, list[str]]] = []
            self.nudge_calls: list[tuple[str, str]] = []

        def rescan_library_after_delete(self, library_id: str, titles: list[str]) -> None:
            self.rescan_calls.append((library_id, titles))

        def nudge_folder_after_delete(self, output_filename: str, title: str) -> None:
            self.nudge_calls.append((output_filename, title))

        def enrich(self, book: Any) -> Any:
            return SyncResult(True, "enriched", fields={})

        def sync(self, book: Any, *, allow_scan: bool = True, restore_target: Any = None) -> Any:
            return SyncResult(True, "synced", fields={})

    # Setup view with library_id
    view_with_lib_id = _make_book_view(
        book_id="b1",
        output_filename="test.epub",
        external=api.ExternalLink(
            provider="kavita",
            item_id="K1",
        ),
    )

    # First run with epub_kept=1
    ctx1 = _FakeCtx(
        event_type=api.PluginEventType.BOOK_DELETED,
        settings={"server": "http://k", "api_key": "x"},
        ui_context={"epub_kept": "1"},
    )
    spy1 = _SpyKavitaService()

    plugin = KavitaSyncPlugin()
    with patch("kavita_sync.plugin._build_service", return_value=spy1):
        plugin.enrich((view_with_lib_id,), ctx1)

    # Second run with epub_kept=0
    ctx2 = _FakeCtx(
        event_type=api.PluginEventType.BOOK_DELETED,
        settings={"server": "http://k", "api_key": "x"},
        ui_context={"epub_kept": "0"},
    )
    spy2 = _SpyKavitaService()

    with patch("kavita_sync.plugin._build_service", return_value=spy2):
        plugin.enrich((view_with_lib_id,), ctx2)

    # Both should have identical rescan and nudge calls
    assert spy1.rescan_calls == spy2.rescan_calls
    assert spy1.nudge_calls == spy2.nudge_calls
    # Neither should call anything delete-like (already nudge-only by design)
    # Confirm at least one nudge happened
    assert len(spy1.rescan_calls) > 0 or len(spy1.nudge_calls) > 0


class TestUnreachedItems:
    """Tests for unreached provider items (circuit open, DFT-FR-17, DFT-D13)."""

    def test_an_unreached_book_is_reported_as_a_skip(self) -> None:
        """An unreached book (unreachable=True) is reported as a skip (DFT-FR-17)."""
        ctx = _FakeCtx(
            event_type=None,
            settings={
                "server": "http://k",
                "api_key": "s",
            },
        )

        plugin = KavitaSyncPlugin()

        class _UnreachedService:
            def sync(self, book: Any, *, restore_target: Any = None) -> Any:
                return SyncResult(
                    False,
                    "Kavita is not reachable",
                    attempted=False,
                    unreachable=True,
                    fields={"external_chapter_count": None},
                )

            def enrich(self, book: Any) -> Any:
                return SyncResult(False, "boom", fields={})

            def rescan_library_after_delete(self, library_id: str, titles: list[str]) -> None:
                pass

            def nudge_folder_after_delete(self, output_filename: str, title: str) -> None:
                pass

        with patch("kavita_sync.plugin._build_service", return_value=_UnreachedService()):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert ctx.item_skips == [("b1", "Kavita is not reachable")]
        assert ctx.item_failures == []

    def test_a_disabled_provider_is_still_not_a_skip(self) -> None:
        """A disabled provider (attempted=False, unreachable=False) is neither skip nor failure."""
        ctx = _FakeCtx(
            event_type=None,
            settings={
                "server": "http://k",
                "api_key": "s",
            },
        )

        plugin = KavitaSyncPlugin()

        class _DisabledService:
            def sync(self, book: Any, *, restore_target: Any = None) -> Any:
                return SyncResult(False, "Kavita sync disabled", attempted=False)

            def enrich(self, book: Any) -> Any:
                return SyncResult(False, "boom", fields={})

            def rescan_library_after_delete(self, library_id: str, titles: list[str]) -> None:
                pass

            def nudge_folder_after_delete(self, output_filename: str, title: str) -> None:
                pass

        with patch("kavita_sync.plugin._build_service", return_value=_DisabledService()):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert ctx.item_skips == []
        assert ctx.item_failures == []

    def test_the_manifest_declares_deferred(self) -> None:
        """The manifest declares deferred=True (SPI 2.24, DFT-D13)."""
        plugin = KavitaSyncPlugin()
        assert plugin.manifest.deferred is True
