"""Tests for the DOCX download source's update check passing a site's browser check (FR-CHS-9).

``check_for_update`` routes the HEAD through the user's challenge solver: a Cloudflare browser
check is solved, or the check reads the solver sentence.
"""

from __future__ import annotations

import requests
import responses
from docx_download_source.plugin import DocxDownloadSourcePlugin
from ebookerr_sdk.domain.ids import make_book_id
from ebookerr_sdk.spi import BookView, CustomValueView
from ebookerr_sdk.testing import FakeContext


@responses.activate
def test_an_update_check_refused_by_a_check_reads_the_sentence() -> None:
    """An update check refused by a check reads the solver sentence."""
    url = "https://files.example.org/book.docx"
    responses.add(responses.HEAD, url, status=403, headers={"cf-mitigated": "challenge"})

    plugin = DocxDownloadSourcePlugin(session=requests.Session())

    prior = BookView(
        book_id=make_book_id(url),
        title="Test Book",
        author="Test Author",
        story_url=url,
        output_filename="test.docx",
        num_chapters=1,
        status=None,
        rating=None,
        cover_ref=None,
        external=None,
        progress=None,
        custom_values={
            "etag": CustomValueView(
                value="abc",
                value_type="str",
                updated_at=None,
            ),
        },
    )

    result = plugin.check_for_update(url, prior=prior, ctx=FakeContext())

    assert result.needs_update is False
    assert result.error == (
        "files.example.org asks for a browser check — set a challenge solver in Settings → General"
    )
