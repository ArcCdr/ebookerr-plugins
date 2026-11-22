"""The rerun rule of FanFicFare's browser-check route, from URL Story Extractor's copy (FR-CHS-10)."""

from __future__ import annotations

from unittest import mock

from ebookerr_sdk.challenge import ChallengeRouter, Clearance, ClearanceCookie
from ebookerr_sdk.testing import FakeContext, FakeSolver
from fanficfare import exceptions
from url_story_extractor.fff_support import challenge_outcome

URL = "https://example.org/story/1"


def test_a_403_on_the_rerun_marks_the_host_every_page() -> None:
    """A clearance FanFicFare's own fetch is refused: the host goes through the solver."""
    with FakeSolver() as fake:
        ctx = FakeContext(app_settings={"challenge_solver_url": fake.url})
        kept = Clearance("UA", (ClearanceCookie("c", "v", "example.org"),))
        ctx.state_set("challenge.clearance:example.org", kept.to_state())
        router = ChallengeRouter(ctx)
        with mock.patch("urllib.request.urlopen") as urlopen:
            outcome = challenge_outcome(
                exceptions.HTTPErrorFFF(URL, 403, "Forbidden"), URL, router, retried=True
            )
            urlopen.assert_not_called()
    assert outcome is True
    assert router.every_page("example.org") is True
    assert ctx.state_get("challenge.every_page:example.org") is True
    assert ctx.state_get("challenge.clearance:example.org") is None


def test_a_403_on_the_rerun_without_a_clearance_keeps_its_reason() -> None:
    """A rerun 403 on a host with no clearance is the site's own refusal, not a browser check."""
    with FakeSolver() as fake:
        ctx = FakeContext(app_settings={"challenge_solver_url": fake.url})
        router = ChallengeRouter(ctx)
        outcome = challenge_outcome(
            exceptions.HTTPErrorFFF(URL, 403, "Forbidden"), URL, router, retried=True
        )
    assert outcome is False
    assert router.every_page("example.org") is False
