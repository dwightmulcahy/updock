import pytest

from app.units import format_size, parse_size, parse_threshold


@pytest.mark.parametrize("text,n", [
    ("1048576", 1048576), ("40GB", 40_000_000_000), ("20GiB", 20 * 1024**3), ("512M", 512_000_000),
    ("1.5 TB", 1_500_000_000_000), ("10kib", 10240), ("7b", 7), ("1P", 10**15),
])
def test_parse_size(text, n):
    assert parse_size(text) == n


@pytest.mark.parametrize("text", ["", "lots", "-5GB", "5XB", "GB"])
def test_parse_size_invalid(text):
    with pytest.raises(ValueError, match="invalid size"):
        parse_size(text)


def test_parse_threshold():
    assert parse_threshold("80%", 1000) == 800
    assert parse_threshold("20GB") == 20_000_000_000
    with pytest.raises(ValueError, match="no maximum"):
        parse_threshold("80%")
    for bad in ("0%", "150%"):
        with pytest.raises(ValueError, match="invalid percentage"):
            parse_threshold(bad, 1000)


@pytest.mark.parametrize("n,out", [(0, "0 B"), (None, "0 B"), (999, "999 B"), (1500, "1.5 KB"),
                                   (4_595_826, "4.6 MB"), (40e9, "40.0 GB"), (3e15, "3000.0 TB")])
def test_format_size(n, out):
    assert format_size(n) == out
