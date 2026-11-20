import pytest

from reifs import paths


@pytest.mark.parametrize("raw,expected", [
    ("documents/1.txt", "/documents/1.txt"),
    ("/documents/1.txt/", "/documents/1.txt"),
    ("//a/./b", "/a/b"),
    ("", "/"),
    ("/", "/"),
])
def test_normalize(raw, expected):
    assert paths.normalize(raw) == expected


def test_normalize_rejects_parent():
    with pytest.raises(paths.VPathError):
        paths.normalize("/a/../b")


@pytest.mark.parametrize("vpath,issues", [
    ("/docs/ok.txt", set()),
    ("/docs/a:b.txt", {"chars"}),
    ("/what?/x", {"chars"}),
    ("/CON", {"reserved"}),
    ("/dir/aux.tar.gz", {"reserved"}),
    ("/dir/com1", {"reserved"}),
    ("/dir/console.txt", set()),
    ("/trailing./x", {"trailing"}),
    ("/x/space ", {"trailing"}),
    ("/" + "a" * 256, {"length"}),
])
def test_portability(vpath, issues):
    assert paths.portability_issues(vpath) == issues


def test_prefix_range():
    lo, hi = paths.prefix_range("/docs")
    assert lo <= "/docs/a" < hi
    assert lo <= "/docs/z/zz" < hi
    assert not (lo <= "/docs2/a" < hi)
    assert not (lo <= "/docs" < hi)


def test_parent_basename():
    assert paths.parent("/a/b/c") == "/a/b"
    assert paths.parent("/a") == "/"
    assert paths.basename("/a/b/c.txt") == "c.txt"
