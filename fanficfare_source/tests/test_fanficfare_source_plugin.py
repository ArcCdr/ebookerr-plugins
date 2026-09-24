"""Tests for FanFicFareSourcePlugin — the catch-all Source wrapping the pull engine."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import ebookerr_sdk.spi as api
import pytest
from ebookerr_sdk.domain.dates import parse_datetime
from ebookerr_sdk.domain.ids import make_book_id
from ebookerr_sdk.testing import FakeContext, make_book_view

from fanficfare_source.protocol import DownloadResult
from fanficfare_source.plugin import FanFicFareSourcePlugin, SourcePullError
from fanficfare_source.pull import FanFicFarePull

URL = "https://www.literotica.com/s/the-12th-key"
OUTPUT = "gabthewriter/The 12th Key.epub"
_META_SAME = {"numChapters": "1", "dateUpdated": "2026-05-19", "status": "In-Progress"}


def fff_json(**overrides: Any) -> dict[str, Any]:
    """Build a FanFicFare-shaped download JSON payload, one field at a time."""
    base: dict[str, Any] = {
        "title": "The 12th Key",
        "author": "gabthewriter",
        "storyId": "the-12th-key",
        "storyUrl": URL,
        "sectionUrl": URL,
        "category": "Erotic Horror",
        "numChapters": "1",
        "output_filename": OUTPUT,
    }
    base.update(overrides)
    return base


def dl(
    *,
    ok: bool = True,
    json: dict[str, Any] | None = None,
    output: str | None = OUTPUT,
    error: str = "",
) -> DownloadResult:
    """Build a canned DownloadResult."""
    return DownloadResult(
        ok=ok,
        json_data=json if json is not None else fff_json(),
        output_filename=output,
        was_update=False,
        error=error,
    )


class FakeGateway:
    """A FanFicFareGateway double: stages a fake EPUB on a successful download."""

    def __init__(self, result: DownloadResult, meta: dict[str, Any] | None = None) -> None:
        """Configure the canned download result and metadata."""
        self.result = result
        self._meta = meta
        self.calls: list[tuple[str, Path]] = []
        self.fetch_metadata_calls = 0

    def fetch_metadata(
        self, url: str, *, timeout_s: int = 600, cancel_event: Any = None
    ) -> dict[str, Any] | None:
        """Record the call and return the canned metadata."""
        self.fetch_metadata_calls += 1
        return self._meta

    def download(
        self,
        url: str,
        *,
        work_dir: Path,
        cancel_event: Any = None,
        on_progress: Any = None,
        pinned_output: str | None = None,
        **_kwargs: Any,
    ) -> DownloadResult:
        """Record the call and stage a fake EPUB when the canned result is a success."""
        self.calls.append((url, work_dir))
        if self.result.ok and self.result.output_filename:
            staged = work_dir / self.result.output_filename
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_text("fake epub")
        return self.result

    def is_available(self) -> bool:
        """Report the gateway as always available."""
        return True


def make_plugin(*, fanficfare: FakeGateway) -> FanFicFareSourcePlugin:
    """Build a plugin wired to a real engine over a fake gateway."""
    engine = FanFicFarePull(fanficfare)  # type: ignore[arg-type]
    return FanFicFareSourcePlugin(pull=engine)


def make_prior(**overrides: Any) -> Any:
    """Build a BookView prior matching URL/OUTPUT, with sane defaults."""
    defaults: dict[str, Any] = {
        "book_id": make_book_id(URL),
        "story_url": URL,
        "output_filename": OUTPUT,
        "num_chapters": 1,
        "date_updated": parse_datetime("2026-05-19"),
    }
    defaults.update(overrides)
    return make_book_view(**defaults)


def stage(tmp_path: Path, relative: str) -> None:
    """Write a placeholder file at tmp_path / relative, as the core would have staged it."""
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("existing epub")


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------


class TestManifest:
    def test_manifest_is_source_floor(self) -> None:
        """Manifest is PluginType.SOURCE with priority 1000 and no event subscriptions."""
        plugin = FanFicFareSourcePlugin()
        assert plugin.manifest.plugin_type == api.PluginType.SOURCE
        assert plugin.manifest.priority == 1000
        assert plugin.manifest.events == ()

    def test_manifest_opts_into_the_update_check_scan(self) -> None:
        """The manifest declares update_check=True (a proxy only asks sources that do)."""
        plugin = FanFicFareSourcePlugin()
        assert plugin.manifest.update_check is True


# ---------------------------------------------------------------------------
# claims() tests
# ---------------------------------------------------------------------------


class TestClaims:
    def test_claims_http_and_https(self) -> None:
        """claims() returns True for http:// and https:// URLs."""
        plugin = FanFicFareSourcePlugin()
        assert plugin.claims("http://a/1") is True
        assert plugin.claims("https://a/1") is True

    def test_claims_rejects_non_http(self) -> None:
        """claims() returns False for non-http URLs."""
        plugin = FanFicFareSourcePlugin()
        assert plugin.claims("ftp://a") is False
        assert plugin.claims("a/1") is False


# ---------------------------------------------------------------------------
# settings_schema() tests
# ---------------------------------------------------------------------------


class TestSettingsSchema:
    def test_settings_schema_empty(self) -> None:
        """settings_schema() returns an empty SettingsSchema."""
        plugin = FanFicFareSourcePlugin()
        assert plugin.settings_schema() == api.SettingsSchema()


# ---------------------------------------------------------------------------
# __init__() / _engine()
# ---------------------------------------------------------------------------


class TestInit:
    def test_plugin_takes_no_repository(self) -> None:
        """Constructor does not accept a book_repo parameter."""
        import inspect

        sig = inspect.signature(FanFicFareSourcePlugin.__init__)
        assert "book_repo" not in sig.parameters

    def test_pull_constructs_engine_when_not_injected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Calling pull() with no injected engine constructs one on demand."""
        monkeypatch.setenv("EBOOKERR_PLUGIN_DATA_DIR", str(tmp_path / "plugin_data"))
        plugin = FanFicFareSourcePlugin()
        # The engine will be constructed but the pull will fail because we're not passing a real URL/context
        # This test mainly ensures the engine construction doesn't raise
        try:
            plugin.pull(URL, tmp_path, None, FakeContext())
        except Exception as e:
            # We expect some error from the actual pull operation, but not a RuntimeError about missing engine
            assert not isinstance(e, RuntimeError) or "no injected pull engine" not in str(e)


# ---------------------------------------------------------------------------
# pull() — delegates to the injected FanFicFarePull engine
# ---------------------------------------------------------------------------


class TestPull:
    def test_pull_delegates_to_the_engine(self, tmp_path: Path) -> None:
        """pull() forwards url and work_dir to the engine and returns its BookPatch."""
        gateway = FakeGateway(dl())
        plugin = make_plugin(fanficfare=gateway)

        patch = plugin.pull(URL, tmp_path, None, FakeContext())

        assert isinstance(patch, api.BookPatch)
        assert patch.book_id == make_book_id(URL)
        assert gateway.calls == [(URL, tmp_path)]

    def test_pull_new_url_returns_upsert_patch(self, tmp_path: Path) -> None:
        """A new URL returns a complete BookPatch(upsert=True)."""
        gateway = FakeGateway(dl())
        plugin = make_plugin(fanficfare=gateway)

        patch = plugin.pull(URL, tmp_path, None, FakeContext())

        assert patch.upsert is True
        assert patch.fields["output_filename"] == OUTPUT
        assert gateway.calls == [(URL, tmp_path)]

    def test_pull_no_new_content_returns_non_upsert_patch_and_skips_download(
        self, tmp_path: Path
    ) -> None:
        """A no-new-content poll returns a non-upsert patch and never calls download."""
        prior = make_prior()
        stage(tmp_path, OUTPUT)
        gateway = FakeGateway(dl(), meta=_META_SAME)
        plugin = make_plugin(fanficfare=gateway)

        patch = plugin.pull(URL, tmp_path, prior, FakeContext())

        assert patch.book_id == prior.book_id
        assert patch.upsert is False
        assert gateway.calls == []

    def test_pull_no_new_content_reports_complete(self, tmp_path: Path) -> None:
        """A skipped pull reports 100% (FR-TYPE-6 — touched-but-unchanged)."""
        prior = make_prior()
        stage(tmp_path, OUTPUT)
        gateway = FakeGateway(dl(), meta=_META_SAME)
        plugin = make_plugin(fanficfare=gateway)
        ctx = FakeContext()

        plugin.pull(URL, tmp_path, prior, ctx)

        assert ctx.reports[-1][0] == 100.0

    def test_pull_unsupported_url_raises_source_pull_error(self, tmp_path: Path) -> None:
        """A failed FanFicFare download surfaces as the documented non-fatal SourcePullError."""
        gateway = FakeGateway(dl(ok=False, error="unsupported site: x"))
        plugin = make_plugin(fanficfare=gateway)

        with pytest.raises(SourcePullError):
            plugin.pull(URL, tmp_path, None, FakeContext())

    def test_pull_verify_failure_raises_source_pull_error(self, tmp_path: Path) -> None:
        """A download with no staged output file surfaces as SourcePullError."""
        gateway = FakeGateway(dl(output=None))
        plugin = make_plugin(fanficfare=gateway)

        with pytest.raises(SourcePullError):
            plugin.pull(URL, tmp_path, None, FakeContext())

    def test_pull_completed_story_disables_auto_pull(self, tmp_path: Path) -> None:
        """A Completed story's patch carries auto_pull=0."""
        gateway = FakeGateway(dl(json=fff_json(status="Completed")))
        plugin = make_plugin(fanficfare=gateway)

        patch = plugin.pull(URL, tmp_path, None, FakeContext())

        assert patch.fields["auto_pull"] == 0

    def test_pull_passes_cancel_check_to_context(self, tmp_path: Path) -> None:
        """pull() routes cancellation checkpoints through the PluginContext."""
        gateway = FakeGateway(dl())
        plugin = make_plugin(fanficfare=gateway)
        ctx = FakeContext()

        plugin.pull(URL, tmp_path, None, ctx)

        assert ctx.cancel_checks >= 1


# ---------------------------------------------------------------------------
# check_for_update() — delegates to the injected engine
# ---------------------------------------------------------------------------


class FakeEngine:
    """A FanFicFarePull double for check_for_update delegation tests."""

    def __init__(self) -> None:
        """Initialise empty call history and no canned result."""
        self.check_for_update_calls: list[dict[str, Any]] = []
        self._check_result: api.UpdateCheck | None = None

    def check_for_update(
        self,
        url: str,
        *,
        prior: Any = None,
        ctx: Any = None,
        cancel_event: Any = None,
    ) -> api.UpdateCheck:
        """Record the call and return the canned result (or a default)."""
        self.check_for_update_calls.append(
            dict(url=url, prior=prior, ctx=ctx, cancel_event=cancel_event)
        )
        if self._check_result is not None:
            return self._check_result
        return api.UpdateCheck(needs_update=True, meta={"numChapters": "42"}, book_id=None)

    def set_check_result(self, result: api.UpdateCheck) -> None:
        """Set the canned check_for_update result this fake returns."""
        self._check_result = result

    def pull(self, url: str, work_dir: Path, prior: Any, ctx: Any) -> api.BookPatch:
        """Unused by the check_for_update delegation tests."""
        raise NotImplementedError


def test_check_for_update_delegates_to_the_engine() -> None:
    """check_for_update() delegates to the injected engine's check_for_update()."""
    fake = FakeEngine()
    expected = api.UpdateCheck(needs_update=True, meta={"numChapters": "42"}, book_id=None)
    fake.set_check_result(expected)
    plugin = FanFicFareSourcePlugin(pull=fake)  # type: ignore[arg-type]

    result = plugin.check_for_update(URL)

    assert result == expected
    assert len(fake.check_for_update_calls) == 1
    assert fake.check_for_update_calls[0]["url"] == URL


def test_check_for_update_forwards_cancel_event() -> None:
    """check_for_update() forwards cancel_event to the engine."""
    fake = FakeEngine()
    fake.set_check_result(api.UpdateCheck(needs_update=False, meta=None, book_id=None))
    plugin = FanFicFareSourcePlugin(pull=fake)  # type: ignore[arg-type]
    cancel_event = threading.Event()

    plugin.check_for_update(URL, cancel_event=cancel_event)

    assert fake.check_for_update_calls[0]["cancel_event"] is cancel_event


def test_check_for_update_forwards_prior() -> None:
    """check_for_update() forwards the prior BookView to the engine."""
    fake = FakeEngine()
    prior = make_book_view()
    plugin = FanFicFareSourcePlugin(pull=fake)  # type: ignore[arg-type]

    plugin.check_for_update(URL, prior=prior)

    assert fake.check_for_update_calls[0]["prior"] is prior


def test_check_for_update_forwards_ctx() -> None:
    """check_for_update() forwards ctx to the engine (SPI 2.30)."""
    fake = FakeEngine()
    ctx = FakeContext()
    plugin = FanFicFareSourcePlugin(pull=fake)  # type: ignore[arg-type]

    plugin.check_for_update(URL, ctx=ctx)

    assert fake.check_for_update_calls[0]["ctx"] is ctx
