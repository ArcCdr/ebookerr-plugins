"""Tests for KomgaSyncPlugin (."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from unittest import mock

import ebookerr_sdk.spi as api
import pytest
from ebookerr_sdk.providers.connection import ConnectionTestResult
from komga_sync import plugin as komga_sync
from komga_sync.plugin import KomgaSyncPlugin

# ---------------------------------------------------------------------------
# Stub PluginContext
# ---------------------------------------------------------------------------


@dataclass
class _FakeCtx:
    mode: api.InvocationMode = api.InvocationMode.HEADLESS
    event_type: api.PluginEventType | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    ui_context: Mapping[str, str] = field(default_factory=dict)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("test.plugin"))
    library_root: Any = None
    check_cancelled_calls: int = field(default=0, init=False)
    reported: list[float] = field(default_factory=list, init=False)
    item_failures: list[tuple[str, str]] = field(default_factory=list, init=False)
    item_skips: list[tuple[str, str]] = field(default_factory=list, init=False)

    def check_cancelled(self) -> None:
        self.check_cancelled_calls += 1

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
# Spy KomgaService
# ---------------------------------------------------------------------------


@dataclass
class _SpyKomgaService:
    """Records sync/enrich calls and their allow_scan kwargs."""

    sync_calls: list[tuple[api.BookView, bool, api.ReadPosition | None]] = field(
        default_factory=list, init=False
    )
    enrich_calls: list[api.BookView] = field(default_factory=list, init=False)
    _client: Any = field(default=None, init=False)

    def sync(
        self,
        book: api.BookView,
        *,
        allow_scan: bool = True,
        restore_target: api.ReadPosition | None = None,
    ) -> Any:
        self.sync_calls.append((book, allow_scan, restore_target))
        from komga_sync.service import SyncResult

        return SyncResult(True, "synced", fields={"external_item_id": "B7"})

    def enrich(self, book: api.BookView) -> Any:
        self.enrich_calls.append(book)
        from komga_sync.service import SyncResult

        return SyncResult(True, "enriched", fields={"external_item_id": "B7"})

    def delete_remote_book(self, external_item_id: str) -> None:
        self._client.delete_book_file(external_item_id)
        self._client.empty_trash()


# ---------------------------------------------------------------------------
# Spy KomgaClient
# ---------------------------------------------------------------------------


@dataclass
class _SpyKomgaClient:
    """Records delete_book_file and empty_trash calls."""

    delete_calls: list[str] = field(default_factory=list, init=False)
    trash_calls: int = field(default=0, init=False)

    def delete_book_file(self, item_id: str) -> bool:
        self.delete_calls.append(item_id)
        return True

    def empty_trash(self) -> bool:
        self.trash_calls += 1
        return True


# ---------------------------------------------------------------------------
# Fake AppSettingsGetter Protocol
# ---------------------------------------------------------------------------


class _FakeAppSettingsGetter(Protocol):
    """Mimics the AppSettingsGetter Protocol."""

    def get(self, key: str, default: str | None = None) -> str | None: ...


@dataclass
class _FakeAppSettings:
    """Fake app settings store."""

    values: dict[str, str] = field(default_factory=dict, init=False)

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_book_view(
    book_id: str = "b1",
    title: str | None = "Test Title",
    item_id: str | None = None,
    external: api.ExternalLink | None = None,
    file_size: int | None = None,
) -> api.BookView:
    if external is None:
        external = api.ExternalLink(item_id=item_id)
    return api.BookView(
        book_id=book_id,
        title=title,
        author=None,
        story_url=None,
        output_filename="test.epub",
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
        plugin = KomgaSyncPlugin()
        assert plugin.manifest.priority == 900

    def test_manifest_accepts_list(self) -> None:
        """Manifest declares accepts_list=True for batch processing."""
        plugin = KomgaSyncPlugin()
        assert plugin.manifest.accepts_list is True

    def test_manifest_is_book_provider(self) -> None:
        """Manifest has correct plugin_type and exclusive_group."""
        plugin = KomgaSyncPlugin()
        assert plugin.manifest.plugin_type == api.PluginType.BOOK
        assert plugin.manifest.exclusive_group == "library_server"
        assert plugin.manifest.id == "komga_sync"

    def test_manifest_events_and_schedule(self) -> None:
        """Manifest listens to the four book events and EpubModified."""
        plugin = KomgaSyncPlugin()
        assert plugin.manifest.events == (
            api.PluginEventType.BOOK_CREATED,
            api.PluginEventType.BOOK_UPDATED,
            api.PluginEventType.BOOK_IMPORTED,
            api.PluginEventType.BOOK_DELETED,
            api.PluginEventType.EPUB_MODIFIED,
        )

    def test_komga_settings_schema_fields(self) -> None:
        """The schema has the expected fields with correct types and properties."""
        plugin = KomgaSyncPlugin()
        schema = plugin.settings_schema()

        assert len(schema.fields) == 6
        fields_by_key = {f.key: f for f in schema.fields}

        assert "server" in fields_by_key
        assert fields_by_key["server"].type == "string"

        assert "api_key" in fields_by_key
        assert fields_by_key["api_key"].type == "string"
        assert fields_by_key["api_key"].secret is True

        assert "external_url" in fields_by_key
        assert fields_by_key["external_url"].type == "string"
        assert fields_by_key["external_url"].label == "External URL"

        assert "library_id" in fields_by_key
        assert fields_by_key["library_id"].type == "string"

        assert "scan_retry_max" in fields_by_key
        assert fields_by_key["scan_retry_max"].type == "int"
        assert fields_by_key["scan_retry_max"].default == "20"

        assert "scan_retry_delay" in fields_by_key
        assert fields_by_key["scan_retry_delay"].type == "int"
        assert fields_by_key["scan_retry_delay"].default == "2"

    def test_the_manifest_declares_its_provider_and_delete_mode(self) -> None:
        """Manifest declares provider=komga and delete_mode=purge."""
        plugin = KomgaSyncPlugin()
        assert plugin.manifest.provider == "komga"
        assert plugin.manifest.delete_mode == "purge"


# ---------------------------------------------------------------------------
# Service building tests
# ---------------------------------------------------------------------------


class TestServiceBuilding:
    def test_sync_builds_service_from_ctx_settings(self) -> None:
        """Service is built from ctx.settings with correct retry values."""

        ctx = _FakeCtx(
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
                "scan_retry_max": "5",
                "scan_retry_delay": "1",
            }
        )

        plugin = KomgaSyncPlugin()

        with mock.patch.object(komga_sync, "_build_service") as mock_build:
            mock_build.return_value = _SpyKomgaService()
            view = _make_book_view("b1")
            plugin.enrich((view,), ctx)

        mock_build.assert_called_once()
        call_ctx = mock_build.call_args[0][0]
        assert call_ctx is ctx

    def test_build_service_passes_external_url(self) -> None:
        """_build_service passes external_url to KomgaService when set."""
        ctx = _FakeCtx(
            settings={
                "server": "http://komga.local:25600",
                "api_key": "test_key",
                "external_url": "http://komga.example.test",
            }
        )

        from komga_sync.plugin import _build_service

        service = _build_service(ctx)

        # Verify that the service has the external_url set
        assert service._external_url == "http://komga.example.test"

    def test_sync_returns_patch_with_fields(self) -> None:
        """sync returns patch containing fields from SyncResult and link-attempt (EXP-200)."""

        ctx = _FakeCtx(
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            }
        )

        plugin = KomgaSyncPlugin()

        spy_service = _SpyKomgaService()
        with mock.patch.object(komga_sync, "_build_service", return_value=spy_service):
            view = _make_book_view("b1")
            patches = plugin.enrich((view,), ctx)

        assert len(patches) == 1
        assert patches[0].book_id == "b1"
        assert patches[0].fields["external_item_id"] == "B7"
        assert "external_link_error" in patches[0].fields
        assert patches[0].fields["external_link_error"] is None


# ---------------------------------------------------------------------------
# Event routing tests
# ---------------------------------------------------------------------------


class TestEventRouting:
    def test_imported_calls_enrich_not_sync(self) -> None:
        """BOOK_IMPORTED event calls service.enrich(), not sync()."""

        ctx = _FakeCtx(
            event_type=api.PluginEventType.BOOK_IMPORTED,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()
        spy_service = _SpyKomgaService()

        with mock.patch.object(komga_sync, "_build_service", return_value=spy_service):
            view = _make_book_view("b1")
            plugin.enrich((view,), ctx)

        assert len(spy_service.enrich_calls) == 1
        assert len(spy_service.sync_calls) == 0

    def test_deleted_deletes_file_and_empties_trash(self) -> None:
        """BOOK_DELETED calls client.delete_book_file() then empty_trash()."""

        ctx = _FakeCtx(
            event_type=api.PluginEventType.BOOK_DELETED,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()
        spy_client = _SpyKomgaClient()
        spy_service = _SpyKomgaService()

        def mock_build_service(ctx: Any) -> Any:
            spy_service._client = spy_client
            return spy_service

        with mock.patch.object(komga_sync, "_build_service", side_effect=mock_build_service):
            view = _make_book_view("b1", item_id="B7")
            patches = plugin.enrich((view,), ctx)

        assert spy_client.delete_calls == ["B7"]
        assert spy_client.trash_calls == 1
        assert patches == []

    def test_deleted_unlinked_is_noop(self) -> None:
        """BOOK_DELETED with no external.item_id is a no-op (no client calls)."""

        ctx = _FakeCtx(
            event_type=api.PluginEventType.BOOK_DELETED,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()
        spy_client = _SpyKomgaClient()
        spy_service = _SpyKomgaService()

        def mock_build_service(ctx: Any) -> Any:
            spy_service._client = spy_client
            return spy_service

        with mock.patch.object(komga_sync, "_build_service", side_effect=mock_build_service):
            view = _make_book_view("b1", item_id=None)
            patches = plugin.enrich((view,), ctx)

        assert spy_client.delete_calls == []
        assert spy_client.trash_calls == 0
        assert patches == []

    def test_allow_scan_rule(self) -> None:
        """allow_scan is True for HEADED or event-driven; False for HEADLESS without event."""

        plugin = KomgaSyncPlugin()
        spy_service = _SpyKomgaService()

        with mock.patch.object(komga_sync, "_build_service", return_value=spy_service):
            view = _make_book_view("b1")

            # HEADED + event None -> allow_scan True
            ctx_headed = _FakeCtx(
                mode=api.InvocationMode.HEADED,
                event_type=None,
                settings={"server": "http://k", "api_key": "s", "library_id": "L"},
            )
            spy_service.sync_calls.clear()
            plugin.enrich((view,), ctx_headed)
            assert spy_service.sync_calls[0][1] is True

            # HEADLESS + BOOK_UPDATED -> allow_scan True
            spy_service.sync_calls.clear()
            ctx_headless_event = _FakeCtx(
                mode=api.InvocationMode.HEADLESS,
                event_type=api.PluginEventType.BOOK_UPDATED,
                settings={"server": "http://k", "api_key": "s", "library_id": "L"},
            )
            plugin.enrich((view,), ctx_headless_event)
            assert spy_service.sync_calls[0][1] is True

            # HEADLESS + event None -> allow_scan False
            spy_service.sync_calls.clear()
            ctx_headless_no_event = _FakeCtx(
                mode=api.InvocationMode.HEADLESS,
                event_type=None,
                settings={"server": "http://k", "api_key": "s", "library_id": "L"},
            )
            plugin.enrich((view,), ctx_headless_no_event)
            assert spy_service.sync_calls[0][1] is False  # check allow_scan is at index 1

    def test_an_epub_modified_event_runs_a_sync(self) -> None:
        """EPUB_MODIFIED event calls service.sync(), not enrich(), with allow_scan=True."""

        ctx = _FakeCtx(
            event_type=api.PluginEventType.EPUB_MODIFIED,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()
        spy_service = _SpyKomgaService()

        with mock.patch.object(komga_sync, "_build_service", return_value=spy_service):
            view = _make_book_view("b1")
            plugin.enrich((view,), ctx)

        assert len(spy_service.sync_calls) == 1
        assert len(spy_service.enrich_calls) == 0
        assert spy_service.sync_calls[0][1] is True  # allow_scan at index 1


class TestSyncOutcomeLogging:
    def test_failed_sync_is_logged_at_warning(self, caplog: Any) -> None:
        """Failed sync (attempted=True) logs at WARNING with the error message."""

        ctx = _FakeCtx(
            event_type=None,  # BOOK_UPDATED, so calls sync()
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
            logger=logging.getLogger("komga_sync.plugin"),
        )

        plugin = KomgaSyncPlugin()

        class _FailingSyncService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "could not load the Komga book")

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with (
            caplog.at_level(logging.WARNING, logger="komga_sync.plugin"),
            mock.patch.object(komga_sync, "_build_service", return_value=_FailingSyncService()),
        ):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert "Komga sync did not complete" in caplog.text
        assert "could not load the Komga book" in caplog.text

    def test_disabled_komga_is_not_a_warning(self, caplog: Any) -> None:
        """Disabled Komga (attempted=False) logs at DEBUG, not WARNING."""

        ctx = _FakeCtx(
            event_type=None,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
            logger=logging.getLogger("komga_sync.plugin"),
        )

        plugin = KomgaSyncPlugin()

        class _DisabledKomgaService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "Komga sync is disabled", attempted=False)

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with (
            caplog.at_level(logging.WARNING, logger="komga_sync.plugin"),
            mock.patch.object(komga_sync, "_build_service", return_value=_DisabledKomgaService()),
        ):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert "did not complete" not in caplog.text

    def test_successful_sync_is_logged_at_debug(self, caplog: Any) -> None:
        """Successful sync logs at DEBUG with external_item_id."""

        ctx = _FakeCtx(
            event_type=None,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
            logger=logging.getLogger("komga_sync.plugin"),
        )

        plugin = KomgaSyncPlugin()

        class _SuccessSyncService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "synced", fields={"external_item_id": "KB1"})

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "enriched", fields={"external_item_id": "KB1"})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with (
            caplog.at_level(logging.DEBUG, logger="komga_sync.plugin"),
            mock.patch.object(komga_sync, "_build_service", return_value=_SuccessSyncService()),
        ):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert "Komga sync ok" in caplog.text
        assert "KB1" in caplog.text

    def test_failed_enrich_names_the_enrich_action(self, caplog: Any) -> None:
        """Failed enrich (BOOK_IMPORTED) logs 'enrich did not complete', not 'sync'."""

        ctx = _FakeCtx(
            event_type=api.PluginEventType.BOOK_IMPORTED,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
            logger=logging.getLogger("komga_sync.plugin"),
        )

        plugin = KomgaSyncPlugin()

        class _FailingEnrichService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom", fields={})

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "book not found in Komga")

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with (
            caplog.at_level(logging.WARNING, logger="komga_sync.plugin"),
            mock.patch.object(komga_sync, "_build_service", return_value=_FailingEnrichService()),
        ):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert "Komga enrich did not complete" in caplog.text


class TestRelinkedPatch:
    def test_the_patch_carries_a_wrong_item_relink(self) -> None:
        """A SyncResult reporting relinked=True is carried onto the patch (EXP-243)."""
        ctx = _FakeCtx(
            event_type=None,
            settings={"server": "http://k", "api_key": "s", "library_id": "L"},
        )
        plugin = KomgaSyncPlugin()

        class _RelinkedService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "synced", fields={"external_item_id": "B7"}, relinked=True)

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "enriched", fields={"external_item_id": "B7"})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_RelinkedService()):
            view = _make_book_view("b1", title="Test Book")
            patches = plugin.enrich((view,), ctx)

        assert len(patches) == 1
        assert patches[0].relinked is True

    def test_the_patch_does_not_claim_a_relink_by_default(self) -> None:
        """A SyncResult with relinked left at its default never claims one on the patch."""
        ctx = _FakeCtx(
            event_type=None,
            settings={"server": "http://k", "api_key": "s", "library_id": "L"},
        )
        plugin = KomgaSyncPlugin()

        class _UnrelinkedService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "synced", fields={"external_item_id": "B7"})

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "enriched", fields={"external_item_id": "B7"})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_UnrelinkedService()):
            view = _make_book_view("b1", title="Test Book")
            patches = plugin.enrich((view,), ctx)

        assert len(patches) == 1
        assert patches[0].relinked is False


class TestStaleLinkKeptPatch:
    def test_a_kept_stale_link_is_recorded_as_the_books_link_error(self) -> None:
        """A SyncResult reporting stale_link_kept lands on the patch's link error (F5c)."""
        ctx = _FakeCtx(
            event_type=None,
            settings={"server": "http://k", "api_key": "s", "library_id": "L"},
        )
        plugin = KomgaSyncPlugin()

        class _StaleKeptService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(
                    True,
                    "synced",
                    fields={"external_item_id": "KB_WRONG"},
                    stale_link_kept="stale link kept: X",
                )

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "enriched", fields={"external_item_id": "KB_WRONG"})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_StaleKeptService()):
            view = _make_book_view("b1", title="Test Book")
            patches = plugin.enrich((view,), ctx)

        assert len(patches) == 1
        assert patches[0].fields["external_link_error"] == "stale link kept: X"

    def test_a_clean_sync_still_clears_the_link_error(self) -> None:
        """A SyncResult with no stale link keeps clearing external_link_error as before (F5c)."""
        ctx = _FakeCtx(
            event_type=None,
            settings={"server": "http://k", "api_key": "s", "library_id": "L"},
        )
        plugin = KomgaSyncPlugin()

        class _CleanService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(
                    True,
                    "synced",
                    fields={"external_item_id": "KB_RIGHT"},
                    stale_link_kept=None,
                )

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "enriched", fields={"external_item_id": "KB_RIGHT"})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_CleanService()):
            view = _make_book_view("b1", title="Test Book")
            patches = plugin.enrich((view,), ctx)

        assert len(patches) == 1
        assert patches[0].fields["external_link_error"] is None


class TestTestConnection:
    def test_test_connection_uses_ctx_settings(self) -> None:
        """test_connection uses ctx.settings; returns (True, 'Connected') on success."""
        plugin = KomgaSyncPlugin()

        # Track which server/api_key were passed to the client
        called_with = {}

        def fake_client_factory(server: str, api_key: str, library_id: str) -> Any:
            called_with["server"] = server
            called_with["api_key"] = api_key
            called_with["library_id"] = library_id
            fake = mock.MagicMock()
            fake.test_connection.return_value = ConnectionTestResult("ok", "Connected.")
            return fake

        ctx = _FakeCtx(
            settings={
                "server": "http://komga.local:25600",
                "api_key": "test-key-123",
                "library_id": "lib-abc",
            }
        )

        with mock.patch(
            "komga_sync.plugin.RequestsKomgaClient",
            side_effect=fake_client_factory,
        ):
            ok, msg = plugin.test_connection(ctx)

        assert ok is True
        assert msg == "Connected"
        assert called_with["server"] == "http://komga.local:25600"
        assert called_with["api_key"] == "test-key-123"
        assert called_with["library_id"] == "lib-abc"

    def test_test_connection_unconfigured(self) -> None:
        """test_connection with missing server/api_key returns (False, 'Not configured')."""
        plugin = KomgaSyncPlugin()

        ctx = _FakeCtx(settings={})

        ok, msg = plugin.test_connection(ctx)

        assert ok is False
        assert msg == "Not configured"


def test_test_connection_distinguishes_unreachable_rejected_and_bad_library(caplog: Any) -> None:
    """Each Komga failure outcome maps to a distinct, actionable message + one WARNING."""
    plugin = KomgaSyncPlugin()
    ctx = _FakeCtx(
        settings={
            "server": "http://komga.local:25600",
            "api_key": "test-key-123",
            "library_id": "NOT-A-REAL-LIBRARY-ID",
        }
    )
    canned = {
        "unreachable": ConnectionTestResult(
            "unreachable",
            "Could not reach the Komga server at http://komga.local:25600. "
            "Check the URL and that Komga is running.",
        ),
        "rejected": ConnectionTestResult("rejected", "Komga refused the API key."),
        "misconfigured": ConnectionTestResult(
            "misconfigured",
            "No Komga library with id 'NOT-A-REAL-LIBRARY-ID'. Check the Library id field.",
        ),
        "ok": ConnectionTestResult("ok", "Connected."),
    }

    outcomes: dict[str, tuple[bool, str]] = {}
    for outcome, canned_result in canned.items():
        fake_client = mock.MagicMock()
        fake_client.test_connection.return_value = canned_result
        caplog.clear()
        with (
            caplog.at_level(logging.WARNING, logger="komga_sync.plugin"),
            mock.patch("komga_sync.plugin.RequestsKomgaClient", return_value=fake_client),
        ):
            outcomes[outcome] = plugin.test_connection(ctx)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        if outcome == "ok":
            assert outcomes[outcome] == (True, "Connected")
            assert warnings == []
        else:
            assert outcomes[outcome] == (False, canned_result.message)
            assert len(warnings) == 1
            assert f"outcome={outcome}" in warnings[0].message

    failure_messages = {outcomes[k][1] for k in ("unreachable", "rejected", "misconfigured")}
    assert len(failure_messages) == 3


# ---------------------------------------------------------------------------
# Batch enrich tests
# ---------------------------------------------------------------------------


class TestBatchEnrich:
    def test_enrich_batch_builds_service_once(self) -> None:
        """Batch enrich with accepts_list=True builds service once and syncs all books."""

        ctx = _FakeCtx(
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            }
        )

        plugin = KomgaSyncPlugin()
        spy_service = _SpyKomgaService()

        build_service_call_count = 0

        def counting_build_service(ctx: Any) -> Any:
            nonlocal build_service_call_count
            build_service_call_count += 1
            return spy_service

        with mock.patch.object(komga_sync, "_build_service", side_effect=counting_build_service):
            view1 = _make_book_view("b1")
            view2 = _make_book_view("b2")
            patches = plugin.enrich((view1, view2), ctx)

        assert build_service_call_count == 1
        assert len(spy_service.sync_calls) == 2
        assert len(patches) == 2


# ---------------------------------------------------------------------------
# BookDeleted unconfigured guard tests (
# ---------------------------------------------------------------------------


class TestBookDeletedUnconfigured:
    def test_book_deleted_unconfigured_is_noop(self, caplog: Any) -> None:
        """BookDeleted with unconfigured server/api_key returns [], no service built."""

        ctx = _FakeCtx(
            event_type=api.PluginEventType.BOOK_DELETED,
            settings={"server": None, "api_key": None},
            logger=logging.getLogger("komga_sync.plugin"),
        )

        plugin = KomgaSyncPlugin()

        # Ensure _build_service is not called by making it raise
        def failing_build_service(ctx: Any) -> Any:
            raise AssertionError("_build_service should not be called for unconfigured BookDeleted")

        with (
            caplog.at_level(logging.DEBUG, logger="komga_sync.plugin"),
            mock.patch.object(komga_sync, "_build_service", side_effect=failing_build_service),
        ):
            view = _make_book_view("b1", item_id="k1")
            patches = plugin.enrich((view,), ctx)

        assert patches == []
        assert "Komga not configured — BookDeleted ignored" in caplog.text

    def test_book_deleted_configured_deletes_and_logs(self, caplog: Any) -> None:
        """BookDeleted with configured server/api_key deletes and logs."""

        ctx = _FakeCtx(
            event_type=api.PluginEventType.BOOK_DELETED,
            settings={"server": "http://k", "api_key": "x"},
            logger=logging.getLogger("komga_sync.plugin"),
        )

        plugin = KomgaSyncPlugin()
        spy_client = _SpyKomgaClient()
        spy_service = _SpyKomgaService()

        def mock_build_service(ctx: Any) -> Any:
            spy_service._client = spy_client
            return spy_service

        with (
            caplog.at_level(logging.INFO, logger="komga_sync.plugin"),
            mock.patch.object(komga_sync, "_build_service", side_effect=mock_build_service),
        ):
            view = _make_book_view("b1", title="Test Book", item_id="k1")
            patches = plugin.enrich((view,), ctx)

        assert patches == []
        assert spy_client.delete_calls == ["k1"]
        assert 'Deleting "' in caplog.text
        assert "item_id=k1" in caplog.text

    def test_build_service_coerces_none_settings(self) -> None:
        """_build_service with all None settings does not crash; service created."""

        ctx = _FakeCtx(settings={"server": None, "api_key": None, "library_id": None})

        # Should not raise; service is created successfully with disabled state (_enabled=False)
        service = komga_sync._build_service(ctx)
        assert service is not None
        assert service._enabled is False


# ---------------------------------------------------------------------------
# Restore passthrough and marker consume tests
# ---------------------------------------------------------------------------


class TestRestorePassthroughAndMarkerConsume:
    def test_sync_passes_restore_target_to_service(self) -> None:
        """sync() passes view.restore_target to service.sync as restore_target kwarg."""

        restore_target = api.ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.75,
        )

        ctx = _FakeCtx(
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            }
        )

        plugin = KomgaSyncPlugin()
        spy_service = _SpyKomgaService()

        with mock.patch.object(komga_sync, "_build_service", return_value=spy_service):
            # Create a new BookView with restore_target
            view_with_restore = api.BookView(
                book_id="b1",
                title="Test Title",
                author=None,
                story_url=None,
                output_filename="test.epub",
                num_chapters=None,
                status=None,
                rating=None,
                cover_ref=None,
                external=api.ExternalLink(item_id=None),
                progress=api.ExternalProgress(),
                custom_values={},
                restore_target=restore_target,
            )
            plugin.enrich((view_with_restore,), ctx)

        assert len(spy_service.sync_calls) == 1
        _, _, passed_restore_target = spy_service.sync_calls[0]
        assert passed_restore_target == restore_target

    def test_marker_cleared_even_when_sync_fails(self) -> None:
        """When an attempted sync fails and view has restore_target, marker is cleared anyway.

        Post-EXP-155, "cleared regardless" is no longer literal: the marker is cleared only
        when the provider write was actually attempted (``restore_attempted=True``), which a
        failed-but-attempted sync still reports.
        """

        restore_target = api.ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.75,
        )

        ctx = _FakeCtx(
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            }
        )

        plugin = KomgaSyncPlugin()

        class _FailingSyncService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom", fields={}, restore_attempted=True)

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_FailingSyncService()):
            view_with_restore = api.BookView(
                book_id="b1",
                title="Test Title",
                author=None,
                story_url=None,
                output_filename="test.epub",
                num_chapters=None,
                status=None,
                rating=None,
                cover_ref=None,
                external=api.ExternalLink(item_id=None),
                progress=api.ExternalProgress(),
                custom_values={},
                restore_target=restore_target,
            )
            patches = plugin.enrich((view_with_restore,), ctx)

        assert len(patches) == 1
        assert patches[0].book_id == "b1"
        # Marker is cleared; link attempt also recorded (EXP-200)
        assert patches[0].fields["read_position_restore"] is None
        assert "external_link_error" in patches[0].fields
        assert patches[0].fields["external_link_error"] == "boom"

    def test_no_marker_field_without_restore_target(self) -> None:
        """Without restore_target, no read_position_restore field in patch."""

        ctx = _FakeCtx(
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            }
        )

        plugin = KomgaSyncPlugin()

        # Mock service to return specific fields
        class _CustomSyncService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "synced", fields={"external_item_id": "k1"})

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "enriched", fields={"external_item_id": "k1"})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_CustomSyncService()):
            view = api.BookView(
                book_id="b1",
                title="Test Title",
                author=None,
                story_url=None,
                output_filename="test.epub",
                num_chapters=None,
                status=None,
                rating=None,
                cover_ref=None,
                external=api.ExternalLink(item_id=None),
                progress=api.ExternalProgress(),
                custom_values={},
                restore_target=None,
            )
            patches = plugin.enrich((view,), ctx)

        assert len(patches) == 1
        assert patches[0].book_id == "b1"
        assert "read_position_restore" not in patches[0].fields

    def test_patch_carries_read_position(self) -> None:
        """Patch includes read_position from SyncResult even if fields is empty."""

        read_position = api.ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=1,
            chapter_progress=0.5,
        )

        ctx = _FakeCtx(
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            }
        )

        plugin = KomgaSyncPlugin()

        class _ReadPositionSyncService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(
                    True,
                    "synced",
                    fields={},
                    read_position=read_position,
                )

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "enriched", fields={}, read_position=read_position)

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        service = _ReadPositionSyncService()
        with mock.patch.object(komga_sync, "_build_service", return_value=service):
            view = api.BookView(
                book_id="b1",
                title="Test Title",
                author=None,
                story_url=None,
                output_filename="test.epub",
                num_chapters=None,
                status=None,
                rating=None,
                cover_ref=None,
                external=api.ExternalLink(item_id=None),
                progress=api.ExternalProgress(),
                custom_values={},
            )
            patches = plugin.enrich((view,), ctx)

        assert len(patches) == 1
        assert patches[0].book_id == "b1"
        assert patches[0].read_position is not None
        assert patches[0].read_position.chapter_index == 1
        assert patches[0].read_position.chapter_progress == 0.5

    def test_enrich_does_not_consume_the_restore_marker(self, caplog: Any) -> None:
        """A read-only BOOK_IMPORTED enrich never consumes a pending restore marker (EXP-155).

        A fresh re-import routes to ``enrich`` one second after the restore, before Komga has
        scanned the file back in — it cannot deliver the marker, so it must leave it pending
        for the next sync rather than clearing it on presence alone.
        """
        restore_target = api.ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.75,
        )

        ctx = _FakeCtx(
            event_type=api.PluginEventType.BOOK_IMPORTED,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
            logger=logging.getLogger("komga_sync.plugin"),
        )

        plugin = KomgaSyncPlugin()

        class _NotFoundEnrichService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom", fields={})

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(
                    False, "book not found in Komga", attempted=True, restore_attempted=False
                )

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with (
            caplog.at_level(logging.DEBUG, logger="komga_sync.plugin"),
            mock.patch.object(komga_sync, "_build_service", return_value=_NotFoundEnrichService()),
        ):
            view = api.BookView(
                book_id="b1",
                title="Test Title",
                author=None,
                story_url=None,
                output_filename="test.epub",
                num_chapters=None,
                status=None,
                rating=None,
                cover_ref=None,
                external=api.ExternalLink(item_id=None),
                progress=api.ExternalProgress(),
                custom_values={},
                restore_target=restore_target,
            )
            patches = plugin.enrich((view,), ctx)

        assert not patches or "read_position_restore" not in patches[0].fields
        pending_records = [r for r in caplog.records if "Restore marker left pending" in r.message]
        assert len(pending_records) == 1
        assert "enrich" in pending_records[0].message

    def test_sync_consumes_the_marker_after_a_real_attempt(self) -> None:
        """A sync that actually attempted the restore write consumes the pending marker."""
        restore_target = api.ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.75,
        )

        ctx = _FakeCtx(
            event_type=None,  # not BOOK_IMPORTED, so calls sync()
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()

        class _AttemptedRestoreSyncService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(
                    True, "synced", restore_attempted=True, fields={"external_item_id": "K1"}
                )

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "enriched", fields={"external_item_id": "K1"})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(
            komga_sync, "_build_service", return_value=_AttemptedRestoreSyncService()
        ):
            view = api.BookView(
                book_id="b1",
                title="Test Title",
                author=None,
                story_url=None,
                output_filename="test.epub",
                num_chapters=None,
                status=None,
                rating=None,
                cover_ref=None,
                external=api.ExternalLink(item_id=None),
                progress=api.ExternalProgress(),
                custom_values={},
                restore_target=restore_target,
            )
            patches = plugin.enrich((view,), ctx)

        assert len(patches) == 1
        assert patches[0].fields["read_position_restore"] is None

    def test_sync_leaves_the_marker_when_the_provider_was_unreachable(self) -> None:
        """A sync that was attempted but never reached the restore write leaves the marker."""
        restore_target = api.ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.75,
        )

        ctx = _FakeCtx(
            event_type=None,  # not BOOK_IMPORTED, so calls sync()
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()

        class _UnreachableSyncService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "unreachable", attempted=True, restore_attempted=False)

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(
            komga_sync, "_build_service", return_value=_UnreachableSyncService()
        ):
            view = api.BookView(
                book_id="b1",
                title="Test Title",
                author=None,
                story_url=None,
                output_filename="test.epub",
                num_chapters=None,
                status=None,
                rating=None,
                cover_ref=None,
                external=api.ExternalLink(item_id=None),
                progress=api.ExternalProgress(),
                custom_values={},
                restore_target=restore_target,
            )
            patches = plugin.enrich((view,), ctx)

        assert not patches or "read_position_restore" not in patches[0].fields

    def test_no_marker_key_when_there_was_no_marker(self) -> None:
        """No pending marker and no attempt means no marker key appears in the patch."""
        ctx = _FakeCtx(
            event_type=None,  # not BOOK_IMPORTED, so calls sync()
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()

        class _NoMarkerSyncService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(
                    True, "synced", fields={"external_item_id": "K1"}, restore_attempted=False
                )

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "enriched", fields={"external_item_id": "K1"})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_NoMarkerSyncService()):
            view = api.BookView(
                book_id="b1",
                title="Test Title",
                author=None,
                story_url=None,
                output_filename="test.epub",
                num_chapters=None,
                status=None,
                rating=None,
                cover_ref=None,
                external=api.ExternalLink(item_id=None),
                progress=api.ExternalProgress(),
                custom_values={},
                restore_target=None,
            )
            patches = plugin.enrich((view,), ctx)

        assert len(patches) == 1
        assert "read_position_restore" not in patches[0].fields


# ---------------------------------------------------------------------------
# Plugin repository/app_settings removal tests (EDIT-FR-14)
# ---------------------------------------------------------------------------


class TestPluginDependencyRemoval:
    def test_plugin_takes_no_repository(self) -> None:
        """KomgaSyncPlugin.__init__ no longer takes book_repo parameter."""
        sig = inspect.signature(KomgaSyncPlugin.__init__)
        assert "book_repo" not in sig.parameters
        assert "app_settings" not in sig.parameters

    def test_plugin_reads_the_external_url_from_the_context(self) -> None:
        """_build_service reads app_external_url from ctx.app_setting."""
        ctx = _FakeCtx(settings={"server": "http://k", "api_key": "s"})

        # Mock app_setting on context
        app_settings_values = {"app_external_url": "https://ext.example"}

        def fake_app_setting(key: str) -> str | None:
            return app_settings_values.get(key)

        ctx.app_setting = fake_app_setting  # type: ignore

        service = komga_sync._build_service(ctx)  # type: ignore

        assert service is not None
        # Verify service received the URL from context
        assert service._app_external_url == "https://ext.example"


# ---------------------------------------------------------------------------
# Per-book progress reporting tests
# ---------------------------------------------------------------------------


class TestPerBookProgressReporting:
    def test_enrich_reports_progress_per_book(self) -> None:
        """enrich reports progress after each book in normal (non-delete) mode."""
        ctx = _FakeCtx(
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            }
        )

        plugin = KomgaSyncPlugin()
        spy_service = _SpyKomgaService()

        with mock.patch.object(komga_sync, "_build_service", return_value=spy_service):
            view1 = _make_book_view("b1")
            view2 = _make_book_view("b2")
            view3 = _make_book_view("b3")
            view4 = _make_book_view("b4")
            plugin.enrich((view1, view2, view3, view4), ctx)

        assert ctx.reported == pytest.approx([25.0, 50.0, 75.0, 100.0])

    def test_enrich_reports_progress_on_the_delete_branch(self) -> None:
        """enrich reports progress on BookDeleted branch."""
        ctx = _FakeCtx(
            event_type=api.PluginEventType.BOOK_DELETED,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()
        spy_client = _SpyKomgaClient()
        spy_service = _SpyKomgaService()

        def mock_build_service(ctx: Any) -> Any:
            spy_service._client = spy_client
            return spy_service

        with mock.patch.object(komga_sync, "_build_service", side_effect=mock_build_service):
            view1 = _make_book_view("b1", item_id="k1")
            view2 = _make_book_view("b2", item_id="k2")
            patches = plugin.enrich((view1, view2), ctx)

        assert patches == []
        assert ctx.reported == pytest.approx([50.0, 100.0])

    def test_enrich_unconfigured_delete_reports_nothing(self) -> None:
        """enrich with BookDeleted but unconfigured server reports nothing."""
        ctx = _FakeCtx(
            event_type=api.PluginEventType.BOOK_DELETED,
            settings={"server": "", "api_key": ""},
        )

        plugin = KomgaSyncPlugin()

        view = _make_book_view("b1", item_id="k1")
        patches = plugin.enrich((view,), ctx)

        assert patches == []
        assert ctx.reported == []

    def test_enrich_empty_selection_reports_nothing(self) -> None:
        """enrich with empty selection reports nothing."""
        ctx = _FakeCtx(
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            }
        )

        plugin = KomgaSyncPlugin()
        spy_service = _SpyKomgaService()

        with mock.patch.object(komga_sync, "_build_service", return_value=spy_service):
            patches = plugin.enrich((), ctx)

        assert patches == []
        assert ctx.reported == []

    def test_enrich_logs_book_count_at_debug(self, caplog: Any) -> None:
        """enrich logs book count at DEBUG before processing."""
        ctx = _FakeCtx(
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
            logger=logging.getLogger("komga_sync.plugin"),
        )

        plugin = KomgaSyncPlugin()
        spy_service = _SpyKomgaService()

        with (
            caplog.at_level(logging.DEBUG, logger="komga_sync.plugin"),
            mock.patch.object(komga_sync, "_build_service", return_value=spy_service),
        ):
            view1 = _make_book_view("b1")
            view2 = _make_book_view("b2")
            view3 = _make_book_view("b3")
            plugin.enrich((view1, view2, view3), ctx)

        assert "Komga sync starting over 3 book(s)" in caplog.text


# ---------------------------------------------------------------------------
# Per-book failure reporting tests (EXP-001)
# ---------------------------------------------------------------------------


class TestPerBookFailureReporting:
    def test_failed_sync_reports_item_failure(self) -> None:
        """Failed sync (attempted=True) reports item failure via ctx.report_failure."""
        ctx = _FakeCtx(
            event_type=None,  # BOOK_UPDATED, so calls sync()
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()

        class _FailingSyncService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "Komga is not reachable")

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_FailingSyncService()):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert ctx.item_failures == [("b1", "Komga is not reachable")]

    def test_disabled_sync_reports_no_failure(self) -> None:
        """Disabled Komga (attempted=False) does not report failure."""
        ctx = _FakeCtx(
            event_type=None,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()

        class _DisabledKomgaService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "Komga sync is disabled", attempted=False)

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_DisabledKomgaService()):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert ctx.item_failures == []

    def test_ok_sync_reports_no_failure(self) -> None:
        """Successful sync does not report failure."""
        ctx = _FakeCtx(
            event_type=None,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()

        class _SuccessSyncService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "synced", fields={"external_item_id": "KB1"})

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "enriched", fields={"external_item_id": "KB1"})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_SuccessSyncService()):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert ctx.item_failures == []

    def test_a_refused_link_is_reported_as_a_skip_not_a_failure(self) -> None:
        """A refused link reports skip, not failure (EXP-243)."""
        ctx = _FakeCtx(
            event_type=None,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()

        class _RefusedSyncService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(
                    False,
                    "link refused: Komga book KB1 is already linked to book_id=owner1",
                    refused=True,
                )

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_RefusedSyncService()):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert ctx.item_skips == [
            ("b1", "link refused: Komga book KB1 is already linked to book_id=owner1")
        ]
        assert ctx.item_failures == []

    def test_a_not_found_sync_is_reported_as_a_skip_not_a_failure(self) -> None:
        """Komga not having indexed the book yet reports skip, not failure (DFT-TR-5)."""
        ctx = _FakeCtx(
            event_type=None,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()

        class _NotFoundSyncService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "book not found in Komga", not_found=True)

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_NotFoundSyncService()):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert ctx.item_skips == [("b1", "book not found in Komga")]
        assert ctx.item_failures == []

    def test_a_non_refused_failure_is_still_a_failure(self) -> None:
        """A non-refused failure still reports as failure."""
        ctx = _FakeCtx(
            event_type=None,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()

        class _NonRefusedFailureService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "Komga is not reachable")

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(
            komga_sync, "_build_service", return_value=_NonRefusedFailureService()
        ):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert ctx.item_failures == [("b1", "Komga is not reachable")]
        assert ctx.item_skips == []

    def test_a_refused_enrich_is_also_a_skip(self) -> None:
        """A refused link in enrich also reports skip, not failure (EXP-243)."""
        ctx = _FakeCtx(
            event_type=api.PluginEventType.BOOK_IMPORTED,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()

        class _RefusedEnrichService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom")

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(
                    False,
                    "link refused: Komga book KB1 is already linked to book_id=owner1",
                    attempted=True,
                    refused=True,
                )

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_RefusedEnrichService()):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert ctx.item_skips == [
            ("b1", "link refused: Komga book KB1 is already linked to book_id=owner1")
        ]
        assert ctx.item_failures == []

    def test_a_disabled_provider_is_neither_a_skip_nor_a_failure(self) -> None:
        """Unattempted outcome is neither skip nor failure."""
        ctx = _FakeCtx(
            event_type=None,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()

        class _DisabledProviderService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "Komga sync is disabled", attempted=False)

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(
            komga_sync, "_build_service", return_value=_DisabledProviderService()
        ):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert ctx.item_failures == []
        assert ctx.item_skips == []


# ---------------------------------------------------------------------------
# Backward move toast notification tests (EXP-123)
# ---------------------------------------------------------------------------


class TestBackwardMoveNotification:
    def test_a_backward_move_reaches_the_user_as_a_durable_notice(self) -> None:
        """Backward move triggers ctx.notify with durable=True (EXP-123, EXP-218)."""
        ctx = _FakeCtx(
            event_type=None,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        # Create a fake notify to record calls
        notify_calls: list[tuple[str, str, bool]] = []

        def fake_notify(kind: str, message: str, *, durable: bool = False) -> None:
            notify_calls.append((kind, message, durable))

        ctx.notify = fake_notify  # type: ignore

        plugin = KomgaSyncPlugin()

        class _BackwardMoveService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(
                    True,
                    "synced",
                    fields={"external_item_id": "KB1"},
                    backward_move='Read position for "Test Book" moved backwards: chapter 124 → 43',
                )

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "enriched", fields={"external_item_id": "KB1"})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_BackwardMoveService()):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        # Should have called ctx.notify with warning, the backward_move message, and durable=True
        assert len(notify_calls) == 1
        kind, message, durable = notify_calls[0]
        assert kind == "warning"
        assert "chapter 124 → 43" in message
        assert durable is True

    def test_a_forward_move_records_nothing(self) -> None:
        """A forward or neutral move (backward_move=None) does not trigger a notify call."""
        ctx = _FakeCtx(
            event_type=None,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        # Create a fake notify to record calls
        notify_calls: list[tuple[str, str, bool]] = []

        def fake_notify(kind: str, message: str, *, durable: bool = False) -> None:
            notify_calls.append((kind, message, durable))

        ctx.notify = fake_notify  # type: ignore

        plugin = KomgaSyncPlugin()

        class _NoBackwardMoveService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(
                    True,
                    "synced",
                    fields={"external_item_id": "KB1"},
                    backward_move=None,
                )

            def enrich(self, book: api.BookView) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(True, "enriched", fields={"external_item_id": "KB1"})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_NoBackwardMoveService()):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        # Should not have called ctx.notify
        assert len(notify_calls) == 0


class TestLinkAttempt:
    """Test link-attempt recording (EXP-200)."""

    def test_a_failed_link_is_recorded_on_the_book(self, caplog: pytest.LogCaptureFixture) -> None:
        """A failed provider link records the error, attempted_at, and size on the book."""
        from komga_sync.service import SyncResult

        plugin = KomgaSyncPlugin()
        view = _make_book_view("b1", "Book 1", file_size=100)
        ctx = _FakeCtx(
            event_type=api.PluginEventType.BOOK_UPDATED,
            settings={
                "server": "http://komga.local:8080",
                "api_key": "test_key",
            },
        )

        class _FailedLinkService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                return SyncResult(
                    False,
                    "book not found in Komga",
                    attempted=True,
                    fields={},
                )

            def enrich(self, book: api.BookView) -> Any:
                return SyncResult(True, "enriched", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with (
            caplog.at_level(logging.DEBUG),
            mock.patch.object(komga_sync, "_build_service", return_value=_FailedLinkService()),
        ):
            result = plugin.enrich((view,), ctx)

            # Should return one patch with link-attempt fields
            assert len(result) == 1
            patch_obj = result[0]
            assert patch_obj.book_id == "b1"
            assert "external_link_error" in patch_obj.fields
            assert patch_obj.fields["external_link_error"] == "book not found in Komga"
            assert "external_link_attempted_at" in patch_obj.fields
            assert "external_link_attempt_size" in patch_obj.fields
            assert patch_obj.fields["external_link_attempt_size"] == 100

            # Should log the failure at DEBUG
            assert (
                'Recorded link failure for "Book 1" (book_id=b1): book not found in Komga'
                in caplog.text
            )

    def test_a_successful_link_clears_the_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """A successful provider link clears a previous error and logs recovery."""
        from komga_sync.service import SyncResult

        plugin = KomgaSyncPlugin()
        view = _make_book_view(
            "b1",
            "Book 1",
            external=api.ExternalLink(link_error="book not found in Komga"),
            file_size=100,
        )
        ctx = _FakeCtx(
            event_type=api.PluginEventType.BOOK_UPDATED,
            settings={
                "server": "http://komga.local:8080",
                "api_key": "test_key",
            },
        )

        class _SuccessfulLinkService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                return SyncResult(
                    True,
                    "synced",
                    attempted=True,
                    fields={"external_item_id": "KB1"},
                )

            def enrich(self, book: api.BookView) -> Any:
                return SyncResult(True, "enriched", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with (
            caplog.at_level(logging.INFO),
            mock.patch.object(komga_sync, "_build_service", return_value=_SuccessfulLinkService()),
        ):
            result = plugin.enrich((view,), ctx)

            # Should return one patch with error cleared to None
            assert len(result) == 1
            patch_obj = result[0]
            assert patch_obj.book_id == "b1"
            assert "external_link_error" in patch_obj.fields
            assert patch_obj.fields["external_link_error"] is None
            assert patch_obj.fields["external_item_id"] == "KB1"

            # Should log the recovery at INFO
            assert (
                'Provider link recovered for "Book 1" (book_id=b1); previous failure: '
                "book not found in Komga" in caplog.text
            )

    def test_an_unreachable_or_disabled_provider_records_nothing(self) -> None:
        """An unreachable or disabled provider records no link attempt."""
        from komga_sync.service import SyncResult

        plugin = KomgaSyncPlugin()
        view = _make_book_view("b1", "Book 1", file_size=100)
        ctx = _FakeCtx(
            event_type=api.PluginEventType.BOOK_UPDATED,
            settings={
                "server": "http://komga.local:8080",
                "api_key": "test_key",
            },
        )

        # Case 1: unreachable provider
        class _UnreachableService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                return SyncResult(
                    False,
                    "Komga is not reachable",
                    attempted=True,
                    unreachable=True,
                    fields={},
                )

            def enrich(self, book: api.BookView) -> Any:
                return SyncResult(True, "enriched", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_UnreachableService()):
            result = plugin.enrich((view,), ctx)
            # No patch should be created (no fields and no read position)
            assert result == []

        # Case 2: disabled provider (attempted=False)
        class _DisabledService:
            def sync(
                self,
                book: api.BookView,
                *,
                allow_scan: bool = True,
                restore_target: api.ReadPosition | None = None,
            ) -> Any:
                return SyncResult(
                    False,
                    "Komga is disabled",
                    attempted=False,
                    fields={},
                )

            def enrich(self, book: api.BookView) -> Any:
                return SyncResult(True, "enriched", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_DisabledService()):
            result = plugin.enrich((view,), ctx)
            # No patch should be created (no fields and no read position)
            assert result == []


# ---------------------------------------------------------------------------
# Link ownership tests (EXP-149)
# ---------------------------------------------------------------------------


def test_build_service_passes_the_link_owner() -> None:
    """_build_service passes ctx.provider_link_owner to KomgaService if available."""

    def mock_provider_link_owner(item_id: str) -> str | None:
        return "someBook" if item_id == "owned_id" else None

    ctx = _FakeCtx(
        settings={
            "server": "http://k",
            "api_key": "key",
            "library_id": "lib",
        }
    )
    # Dynamically add the provider_link_owner method
    ctx.provider_link_owner = mock_provider_link_owner  # type: ignore

    service = komga_sync._build_service(ctx)

    assert service._link_owner is not None
    assert service._link_owner("owned_id") == "someBook"
    assert service._link_owner("unowned_id") is None


def test_an_unreachable_result_logs_at_debug_and_still_counts_as_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unreachable result from sync logs at DEBUG and still counts as a failure."""
    from komga_sync.service import SyncResult

    plugin = KomgaSyncPlugin()
    view1 = _make_book_view("b1", "Book 1")
    view2 = _make_book_view("b2", "Book 2")
    ctx = _FakeCtx(
        settings={
            "server": "http://komga.local:8080",
            "api_key": "test_key",
        },
        event_type=api.PluginEventType.BOOK_UPDATED,
        logger=logging.getLogger("komga_sync.plugin"),
    )

    mock_service = mock.MagicMock()
    mock_service.sync.return_value = SyncResult(
        ok=False, message="Komga is not reachable", attempted=True, unreachable=True
    )

    with (
        caplog.at_level(logging.DEBUG, logger="komga_sync.plugin"),
        mock.patch("komga_sync.plugin._build_service", return_value=mock_service),
    ):
        plugin.enrich((view1, view2), ctx)

    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) == 0

    debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert any("Komga" in r.message and "skipped" in r.message for r in debug_records)

    assert len(ctx.item_failures) == 2


def test_an_unlanded_restore_is_reported_as_an_item_failure(caplog: Any) -> None:
    """Unlanded restore (restore_attempted=True, restore_landed=False) reports item failure.

    When a sync reaches the book and attempts a restore but the provider rejects it
    (or no chapter matches), the restore is consumed (marker cleared) and reported
    as an item failure through ctx.report_failure(), so the task layer surfaces it.
    A WARNING is logged with the book title, book_id, and chapter_index.
    """
    from komga_sync.service import SyncResult

    plugin = KomgaSyncPlugin()
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
            "library_id": "L",
        },
        event_type=api.PluginEventType.BOOK_UPDATED,
    )

    class _UnlandedRestoreService:
        def sync(
            self,
            book: Any,
            *,
            allow_scan: bool = True,
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

        def delete_remote_book(self, external_item_id: str) -> None:
            pass

    with (
        mock.patch.object(komga_sync, "_build_service", return_value=_UnlandedRestoreService()),
        caplog.at_level(logging.WARNING, logger="komga_sync.plugin"),
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
    from komga_sync.service import SyncResult

    plugin = KomgaSyncPlugin()
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
            "library_id": "L",
        },
        event_type=api.PluginEventType.BOOK_UPDATED,
    )

    class _LandedRestoreService:
        def sync(
            self,
            book: Any,
            *,
            allow_scan: bool = True,
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

        def delete_remote_book(self, external_item_id: str) -> None:
            pass

    with mock.patch.object(komga_sync, "_build_service", return_value=_LandedRestoreService()):
        patches = plugin.enrich((view,), ctx)

    # Should NOT report an item failure
    assert ctx.item_failures == []

    # Should still return a patch with read_position_restore cleared
    assert len(patches) == 1
    assert patches[0].fields.get("read_position_restore") is None


# ---------------------------------------------------------------------------
# Factory (_build_service) tests
# ---------------------------------------------------------------------------


def test_build_service_passes_the_library_folder_to_the_komga_service() -> None:
    """_build_service passes library_root to KomgaService (EXP-194)."""
    from pathlib import Path as PathlibPath

    from komga_sync.plugin import _build_service

    ctx = _FakeCtx(
        settings={"server": "http://komga", "api_key": "key"},
        library_root=PathlibPath("/lib"),
    )

    with mock.patch("komga_sync.plugin.RequestsKomgaClient"):
        service = _build_service(ctx)

    assert service._library_folder == PathlibPath("/lib")


def test_build_service_binds_a_scan_ledger_to_the_run() -> None:
    """_build_service creates a ScanLedger bound to the plugin's context (DFT-D21, PMG-D32)."""
    from ebookerr_sdk.providers.scan_ledger import ScanLedger
    from komga_sync.plugin import _build_service

    ctx = _FakeCtx(
        settings={"server": "http://komga", "api_key": "key"},
    )

    with mock.patch("komga_sync.plugin.RequestsKomgaClient"):
        service = _build_service(ctx)

    assert isinstance(service._scan_requests, ScanLedger)
    assert service._scan_requests._ctx is ctx


# ---------------------------------------------------------------------------
# TASK-45 — epub_kept flag on BookDeleted (SCS-D27)
# ---------------------------------------------------------------------------


def test_a_kept_epub_is_not_purged_from_komga(caplog: Any) -> None:
    """BOOK_DELETED with epub_kept=1 skips the delete_remote_book call."""
    ctx = _FakeCtx(
        event_type=api.PluginEventType.BOOK_DELETED,
        settings={
            "server": "http://k",
            "api_key": "s",
            "library_id": "L",
        },
        ui_context={"epub_kept": "1"},
        logger=logging.getLogger("komga_sync.plugin"),
    )

    plugin = KomgaSyncPlugin()
    spy_client = _SpyKomgaClient()
    spy_service = _SpyKomgaService()

    def mock_build_service(ctx: Any) -> Any:
        spy_service._client = spy_client
        return spy_service

    with (
        caplog.at_level(logging.INFO, logger="komga_sync.plugin"),
        mock.patch.object(komga_sync, "_build_service", side_effect=mock_build_service),
    ):
        view = _make_book_view("b1", item_id="B7")
        patches = plugin.enrich((view,), ctx)

    assert spy_client.delete_calls == []
    assert spy_client.trash_calls == 0
    assert patches == []

    # Check that the log contains "Kept the EPUB" and "left in place"
    assert any(
        "Kept the EPUB" in record.message and "left in place" in record.message
        for record in caplog.records
        if record.levelname == "INFO"
    )


def test_a_deleted_epub_is_still_purged(caplog: Any) -> None:
    """BOOK_DELETED with epub_kept=0 calls delete_remote_book."""
    ctx = _FakeCtx(
        event_type=api.PluginEventType.BOOK_DELETED,
        settings={
            "server": "http://k",
            "api_key": "s",
            "library_id": "L",
        },
        ui_context={"epub_kept": "0"},
    )

    plugin = KomgaSyncPlugin()
    spy_client = _SpyKomgaClient()
    spy_service = _SpyKomgaService()

    def mock_build_service(ctx: Any) -> Any:
        spy_service._client = spy_client
        return spy_service

    with mock.patch.object(komga_sync, "_build_service", side_effect=mock_build_service):
        view = _make_book_view("b1", item_id="B7")
        patches = plugin.enrich((view,), ctx)

    assert spy_client.delete_calls == ["B7"]
    assert spy_client.trash_calls == 1
    assert patches == []


def test_an_absent_epub_kept_flag_still_purges(caplog: Any) -> None:
    """BOOK_DELETED with missing epub_kept flag defaults to purging (backward compat)."""
    ctx = _FakeCtx(
        event_type=api.PluginEventType.BOOK_DELETED,
        settings={
            "server": "http://k",
            "api_key": "s",
            "library_id": "L",
        },
        ui_context={},
    )

    plugin = KomgaSyncPlugin()
    spy_client = _SpyKomgaClient()
    spy_service = _SpyKomgaService()

    def mock_build_service(ctx: Any) -> Any:
        spy_service._client = spy_client
        return spy_service

    with mock.patch.object(komga_sync, "_build_service", side_effect=mock_build_service):
        view = _make_book_view("b1", item_id="B7")
        patches = plugin.enrich((view,), ctx)

    assert spy_client.delete_calls == ["B7"]
    assert patches == []


class TestUnreachedItems:
    """Tests for unreached provider items (circuit open, DFT-FR-17, DFT-D13)."""

    def test_an_unreached_book_is_reported_as_a_skip(self) -> None:
        """An unreached book (unreachable=True) is reported as a skip (DFT-FR-17)."""
        ctx = _FakeCtx(
            event_type=None,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()

        class _UnreachedService:
            def sync(
                self,
                book: Any,
                *,
                allow_scan: bool = True,
                restore_target: Any = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(
                    False,
                    "Komga is not reachable",
                    attempted=False,
                    unreachable=True,
                    fields={"external_chapter_count": None},
                )

            def enrich(self, book: Any) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_UnreachedService()):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert ctx.item_skips == [("b1", "Komga is not reachable")]
        assert ctx.item_failures == []

    def test_a_disabled_provider_is_still_not_a_skip(self) -> None:
        """A disabled provider (attempted=False, unreachable=False) is neither skip nor failure."""
        ctx = _FakeCtx(
            event_type=None,
            settings={
                "server": "http://k",
                "api_key": "s",
                "library_id": "L",
            },
        )

        plugin = KomgaSyncPlugin()

        class _DisabledService:
            def sync(
                self,
                book: Any,
                *,
                allow_scan: bool = True,
                restore_target: Any = None,
            ) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "Komga sync disabled", attempted=False)

            def enrich(self, book: Any) -> Any:
                from komga_sync.service import SyncResult

                return SyncResult(False, "boom", fields={})

            def delete_remote_book(self, external_item_id: str) -> None:
                pass

        with mock.patch.object(komga_sync, "_build_service", return_value=_DisabledService()):
            view = _make_book_view("b1", title="Test Book")
            plugin.enrich((view,), ctx)

        assert ctx.item_skips == []
        assert ctx.item_failures == []

    def test_the_manifest_declares_deferred(self) -> None:
        """The manifest declares deferred=True (SPI 2.24, DFT-D13)."""
        plugin = KomgaSyncPlugin()
        assert plugin.manifest.deferred is True
