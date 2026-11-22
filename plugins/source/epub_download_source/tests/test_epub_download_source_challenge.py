"""Tests for the EPUB download source passing a site's browser check (FR-CHS-9).

``check_for_update`` and ``pull`` send their request through the call's challenge solver: a
Cloudflare browser check is solved, or the check fails with the solver's sentence — never a
bare ``HTTP 403`` — and a refused check counts as no outage for the host breaker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import requests
import responses
from ebookerr_sdk.challenge.solver import Clearance, ClearanceCookie, Solution
from ebookerr_sdk.domain.ids import make_book_id
from ebookerr_sdk.spi import CustomValueView, SourcePullError
from ebookerr_sdk.testing import FakeContext, make_book_view
from epub_download_source.plugin import EpubDownloadSourcePlugin

_URL = "https://files.example.org/book.epub"


def _fake_solution() -> Solution:
    """A solver answer whose clearance passes *files.example.org*'s check."""
    return Solution(
        url="https://files.example.org/",
        status=200,
        body="<pre>OK</pre>",
        clearance=Clearance(
            user_agent="Mozilla/5.0 (FakeSolver) Chrome/152.0",
            cookies=(
                ClearanceCookie("clearance", "fake-clearance-value", domain="files.example.org"),
            ),
        ),
        expires_at=None,
    )


class FakeSolver:
    """A challenge solver double: its ``.url`` is set and ``solve`` returns a fixed clearance.

    While active it patches ``SolverClient`` so ``solve`` returns :func:`_fake_solution`,
    without any network call.
    """

    def __init__(self) -> None:
        """Patch ``SolverClient`` to return the fixed clearance."""
        self.url = "https://fake-solver.example"
        self._patch = mock.patch(
            "ebookerr_sdk.challenge.router.SolverClient",
            return_value=mock.Mock(solve=mock.Mock(return_value=_fake_solution())),
        )

    def __enter__(self) -> FakeSolver:
        """Start the ``SolverClient`` patch."""
        self._patch.start()
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop the ``SolverClient`` patch."""
        self._patch.stop()


def _prior_with_etag() -> Any:
    """A prior carrying the ``etag`` validator the update check compares against."""
    return make_book_view(
        book_id=make_book_id(_URL),
        title="Old Title",
        author="Old Author",
        story_url=_URL,
        output_filename="old.epub",
        num_chapters=1,
        custom_values={
            "etag": CustomValueView(value="abc", value_type="str", updated_at=None),
        },
    )


def _make_plugin() -> EpubDownloadSourcePlugin:
    """The plugin with a fresh session; the composition root gives it one per pull."""
    return EpubDownloadSourcePlugin(session=requests.Session())


@responses.activate
def test_an_update_check_refused_by_a_check_reads_the_sentence() -> None:
    """A challenged HEAD with a prior etag fails the update check with the solver's sentence."""
    responses.add(
        responses.HEAD,
        _URL,
        status=403,
        headers={"cf-mitigated": "challenge"},
    )

    result = _make_plugin().check_for_update(_URL, prior=_prior_with_etag(), ctx=FakeContext())

    assert result.error == (
        "files.example.org asks for a browser check — set a challenge solver in Settings → General"
    )


@responses.activate
def test_a_download_refused_by_a_check_fails_with_the_sentence(
    tmp_path: Path,
) -> None:
    """A challenged GET without a prior fails the pull with the solver's sentence, no outage."""
    responses.add(
        responses.GET,
        _URL,
        status=403,
        headers={"cf-mitigated": "challenge"},
    )

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    ctx = FakeContext()
    with pytest.raises(SourcePullError) as caught:
        _make_plugin().pull(_URL, work_dir, None, ctx)

    message = str(caught.value)
    assert "asks for a browser check" in message
    assert "HTTP 403" not in message
    assert ctx.circuit.failed == []


@responses.activate
def test_a_download_passes_a_check_through_the_solver(tmp_path: Path, build_epub: Any) -> None:
    """A challenged GET solved through the solver returns the EPUB with the clearance cookie."""
    epub_bytes = build_epub([("Chapter 1", "http://example.com/ch1")]).read_bytes()

    responses.add(responses.GET, _URL, status=403, headers={"cf-mitigated": "challenge"})
    responses.add(
        responses.GET,
        _URL,
        body=epub_bytes,
        status=200,
        headers={"Content-Type": "application/epub+zip"},
    )

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with FakeSolver() as fake:
        responses.add_passthru(fake.url)
        ctx = FakeContext(app_settings={"challenge_solver_url": fake.url})
        patch = _make_plugin().pull(_URL, work_dir, None, ctx)

    assert patch.upsert is True
    get_calls = [call for call in responses.calls if call.request.method == "GET"]
    assert len(get_calls) == 2
    assert "clearance=fake-clearance-value" in get_calls[1].request.headers["Cookie"]
