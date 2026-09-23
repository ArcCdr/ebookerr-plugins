"""Tests for EPUB normalizer unit-conversion functions."""

import pytest
from epub_normalize.normalize.units import (
    is_absolute_unit,
    to_em,
)


class TestPxBaseConversion:
    """Test conversions using px as the base unit."""

    def test_16px_equals_1em(self) -> None:
        """16 px should equal 1 em on a 16 px base."""
        assert to_em(16, "px") == "1em"

    def test_8px_equals_0_5em(self) -> None:
        """8 px should equal 0.5 em."""
        assert to_em(8, "px") == "0.5em"

    def test_24px_equals_1_5em(self) -> None:
        """24 px should equal 1.5 em."""
        assert to_em(24, "px") == "1.5em"


class TestPointConversion:
    """Test conversions from points."""

    def test_12pt_equals_1em(self) -> None:
        """12 pt × 4/3 = 16 px = 1 em."""
        assert to_em(12, "pt") == "1em"

    def test_18pt_equals_1_5em(self) -> None:
        """18 pt should equal 1.5 em."""
        assert to_em(18, "pt") == "1.5em"


class TestPhysicalUnits:
    """Test conversions from physical units (inches, centimeters, millimeters, picas)."""

    def test_1in_is_clamped_to_max(self) -> None:
        """1 inch = 96 px / 16 = 6 em → clamped to EM_MAX (4 em)."""
        assert to_em(1, "in") == "4em"

    def test_1cm_equals_2_362em(self) -> None:
        """1 cm = 96/2.54 = 37.795 px / 16 = 2.362 em."""
        assert to_em(1, "cm") == "2.362em"

    def test_10mm_equals_2_362em(self) -> None:
        """10 mm = 1 cm = 2.362 em."""
        assert to_em(10, "mm") == "2.362em"

    def test_1pc_equals_1em(self) -> None:
        """1 pica = 16 px = 1 em."""
        assert to_em(1, "pc") == "1em"


class TestQuarterMillimetre:
    """Test conversions from quarter millimeters."""

    def test_40q_equals_2_362em(self) -> None:
        """40 Q (quarter millimeters) = 10 mm = 1 cm = 2.362 em."""
        assert to_em(40, "q") == "2.362em"


class TestCaseInsensitiveUnits:
    """Test that unit names are case-insensitive."""

    def test_uppercase_px(self) -> None:
        """Uppercase 'PX' should work like lowercase 'px'."""
        assert to_em(16, "PX") == "1em"

    def test_mixed_case_pt(self) -> None:
        """Mixed case 'Pt' should work like lowercase 'pt'."""
        assert to_em(12, "Pt") == "1em"


class TestZeroHandling:
    """Test that zero values are handled specially."""

    def test_zero_px_returns_0(self) -> None:
        """Zero px should return '0' without a unit."""
        assert to_em(0, "px") == "0"

    def test_zero_float_pt_returns_0(self) -> None:
        """Zero pt (as float) should return '0' without a unit."""
        assert to_em(0.0, "pt") == "0"


class TestClamping:
    """Test that values outside [EM_MIN, EM_MAX] are clamped."""

    def test_values_below_min_are_clamped_up(self) -> None:
        """1 px = 0.0625 em → clamped up to EM_MIN (0.4 em)."""
        assert to_em(1, "px") == "0.4em"

    def test_values_above_max_are_clamped_down(self) -> None:
        """200 px = 12.5 em → clamped down to EM_MAX (4 em)."""
        assert to_em(200, "px") == "4em"


class TestNegativeValues:
    """Test that negative values keep their sign and clamp by magnitude."""

    def test_negative_24px_equals_minus_1_5em(self) -> None:
        """Negative values should keep their sign when clamping."""
        assert to_em(-24, "px") == "-1.5em"

    def test_negative_1px_clamped_to_minus_0_4em(self) -> None:
        """Negative magnitude below EM_MIN should clamp and keep sign."""
        assert to_em(-1, "px") == "-0.4em"

    def test_negative_200px_clamped_to_minus_4em(self) -> None:
        """Negative magnitude above EM_MAX should clamp and keep sign."""
        assert to_em(-200, "px") == "-4em"


class TestRelativeUnitsReturnNone:
    """Test that relative units (non-absolute) return None."""

    @pytest.mark.parametrize(
        "unit",
        ["em", "rem", "%", "ex", "ch", "vw", "vh", "", "fr"],
    )
    def test_relative_units_unsupported(self, unit: str) -> None:
        """Relative units should return None."""
        assert to_em(1, unit) is None


class TestIsAbsoluteUnit:
    """Test the is_absolute_unit predicate."""

    def test_px_is_absolute(self) -> None:
        """Pixels should be recognized as absolute."""
        assert is_absolute_unit("px") is True

    def test_pt_uppercase_is_absolute(self) -> None:
        """Point (uppercase) should be recognized as absolute."""
        assert is_absolute_unit("PT") is True

    def test_em_is_not_absolute(self) -> None:
        """Em should not be recognized as absolute."""
        assert is_absolute_unit("em") is False

    def test_empty_string_is_not_absolute(self) -> None:
        """Empty string should not be recognized as absolute."""
        assert is_absolute_unit("") is False


class TestTrailingZerosTrimmed:
    """Test that trailing zeros in the decimal are trimmed."""

    def test_1_25em_not_1_250em(self) -> None:
        """20 px = 1.25 em (never '1.250em')."""
        assert to_em(20, "px") == "1.25em"

    def test_2em_not_2_000em(self) -> None:
        """32 px = 2 em (never '2.000em')."""
        assert to_em(32, "px") == "2em"
