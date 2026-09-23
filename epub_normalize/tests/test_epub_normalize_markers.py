"""Tests for EPUB normalize per-file idempotency markers."""

import pytest

from epub_normalize.normalize.markers import (
    read_css_marker,
    read_xhtml_marker,
    stamp_css,
    stamp_xhtml,
    strip_css_marker,
    strip_xhtml_marker,
)


class TestCssMarkers:
    """Tests for CSS marker operations."""

    def test_stamp_css_puts_the_marker_first(self) -> None:
        """stamp_css places the marker as the first line."""
        out = stamp_css("body { margin: 0 }\n", "abc123def456")
        assert out.startswith("/* ebookerr-normalized:abc123def456 */\n")
        assert out.endswith("body { margin: 0 }\n")

    def test_read_css_marker_roundtrip(self) -> None:
        """read_css_marker recovers the signature from stamped CSS."""
        result = read_css_marker(stamp_css("p{}", "0123456789ab"))
        assert result == "0123456789ab"

    def test_read_css_marker_absent(self) -> None:
        """read_css_marker returns None when no marker present."""
        assert read_css_marker("body { margin: 0 }") is None

    def test_read_css_marker_rejects_a_bad_signature(self) -> None:
        """read_css_marker rejects markers with non-hex signatures."""
        assert read_css_marker("/* ebookerr-normalized:NOTHEX */\np{}") is None

    def test_strip_css_marker_removes_it_and_its_newline(self) -> None:
        """strip_css_marker removes the marker and its trailing newline."""
        stamped = stamp_css("p{}\n", "0123456789ab")
        assert strip_css_marker(stamped) == "p{}\n"

    def test_stamp_css_is_idempotent(self) -> None:
        """stamp_css with the same signature is idempotent."""
        s = "0123456789ab"
        once = stamp_css("p{}\n", s)
        twice = stamp_css(once, s)
        assert twice == once

    def test_restamping_css_replaces_an_old_signature(self) -> None:
        """stamp_css with a new signature replaces the old one."""
        out = stamp_css(stamp_css("p{}\n", "aaaaaaaaaaaa"), "bbbbbbbbbbbb")
        assert read_css_marker(out) == "bbbbbbbbbbbb"
        assert "aaaaaaaaaaaa" not in out


class TestXhtmlMarkers:
    """Tests for XHTML marker operations."""

    def test_stamp_xhtml_inserts_before_head_close(self) -> None:
        """stamp_xhtml places the marker before the closing </head> tag."""
        doc = "<html><head><title>T</title></head><body><p>x</p></body></html>"
        result = stamp_xhtml(doc, "0123456789ab")
        assert "<!--ebookerr-normalized:0123456789ab--></head>" in result
        # Verify the marker is before </head> and after </title>
        assert "<title>T</title><!--ebookerr-normalized:0123456789ab--></head>" in result

    def test_stamp_xhtml_head_close_is_case_insensitive(self) -> None:
        """stamp_xhtml is case-insensitive when matching </head>."""
        doc = "<html><head><title>T</title></HEAD><body><p>x</p></body></html>"
        result = stamp_xhtml(doc, "0123456789ab")
        # The marker should be inserted before </HEAD>
        assert "<!--ebookerr-normalized:0123456789ab--></HEAD>" in result

    def test_stamp_xhtml_falls_back_to_html_close(self) -> None:
        """stamp_xhtml falls back to </html> when no </head> exists."""
        doc = "<html><body><p>x</p></body></html>"
        result = stamp_xhtml(doc, "0123456789ab")
        assert "<!--ebookerr-normalized:0123456789ab--></html>" in result

    def test_stamp_xhtml_falls_back_to_append(self) -> None:
        """stamp_xhtml appends the marker when no head or html close exists."""
        doc = "<p>fragment</p>"
        result = stamp_xhtml(doc, "0123456789ab")
        assert result == "<p>fragment</p><!--ebookerr-normalized:0123456789ab-->"

    def test_read_xhtml_marker_roundtrip(self) -> None:
        """read_xhtml_marker recovers the signature from stamped XHTML."""
        result = read_xhtml_marker(stamp_xhtml("<html></html>", "0123456789ab"))
        assert result == "0123456789ab"

    def test_read_xhtml_marker_absent(self) -> None:
        """read_xhtml_marker returns None when no marker present."""
        assert read_xhtml_marker("<html></html>") is None

    def test_strip_xhtml_marker_restores_the_original(self) -> None:
        """strip_xhtml_marker removes the marker and recovers the original."""
        doc = "<html><head><title>T</title></head><body><p>x</p></body></html>"
        stamped = stamp_xhtml(doc, "0123456789ab")
        assert strip_xhtml_marker(stamped) == doc

    @pytest.mark.parametrize(
        "doc",
        [
            "<html><head></head><body>x</body></html>",
            "<html><body>x</body></html>",
            "<p>fragment</p>",
        ],
    )
    def test_stamp_xhtml_is_idempotent(self, doc: str) -> None:
        """stamp_xhtml is idempotent across all insertion positions."""
        s = "0123456789ab"
        once = stamp_xhtml(doc, s)
        twice = stamp_xhtml(once, s)
        assert twice == once

    def test_restamping_xhtml_replaces_an_old_signature(self) -> None:
        """stamp_xhtml with a new signature replaces the old one."""
        doc = "<html></html>"
        out = stamp_xhtml(stamp_xhtml(doc, "aaaaaaaaaaaa"), "bbbbbbbbbbbb")
        assert read_xhtml_marker(out) == "bbbbbbbbbbbb"
        assert "aaaaaaaaaaaa" not in out


class TestMarkerPatterns:
    """Tests for marker pattern matching."""

    def test_markers_do_not_match_other_comments(self) -> None:
        """Markers reject malformed or different comment patterns."""
        # Missing colon
        assert read_xhtml_marker("<!-- ebookerr normalized -->") is None
        # Missing marker prefix
        assert read_css_marker("/* normalized:0123456789ab */") is None

    def test_only_the_last_html_close_is_used(self) -> None:
        """stamp_xhtml uses the final </html> when multiple exist."""
        doc = "<html><body>a</body></html>\n<!-- trailing --></html>"
        result = stamp_xhtml(doc, "0123456789ab")
        # The marker should be before the *last* </html>
        assert "<!--ebookerr-normalized:0123456789ab--></html>" in result
        # And it should appear after the trailing comment
        assert "<!-- trailing --><!--ebookerr-normalized:0123456789ab--></html>" in result
