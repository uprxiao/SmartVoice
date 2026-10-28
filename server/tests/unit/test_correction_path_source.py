"""Path-only contract drafts; does not expand existing lexical policy."""
import pytest

from app.services.refiner_core import correction_guard


@pytest.mark.parametrize("source,marker,before,after,blocked", [
    ("路径 /tmp/a", "/tmp/", "a", "b", True),
    ("路径/tmp/a", "/tmp/", "a", "b", True),
    ("路径 /tmp/a，代号 a", "/tmp/", "a", "b", True),
    ("路径 /tmp/a，代号 a", "代号 ", "a", "b", False),
    ("文件 C:\\work\\alpha", "work\\", "alpha", "bravo", True),
    ("文件C:\\work\\alpha", "work\\", "alpha", "bravo", True),
    ("保存到 ~/memo/item", "memo/", "item", "entry", True),
    ("保存到 ./memo/item", "memo/", "item", "entry", True),
    ("保存到 ../memo/item", "memo/", "item", "entry", True),
    ("访问https://example.test/alpha", "example.test/", "alpha", "bravo", True),
    ("使用 CI/CB 流水线", "使用 ", "CI/CB", "CI/CD", False),
    ("模式为输入/输处", "输入/", "输处", "输出", False),
    ("设备品牌是小咪", "设备品牌是", "小咪", "小米", False),
    ("同事叫阿米", "同事叫阿", "米", "咪", False),
    ("同事叫吴非", "同事叫", "吴非", "吴菲", False),
    ("项目名为未央", "项目名为", "未央", "未秧", False),
])
def test_path_membership_comes_from_exact_position(source, marker, before, after, blocked):
    offset = source.index(marker) + len(marker)
    assert source[offset:offset + len(before)] == before
    assert (correction_guard(before, after, source=source, offset=offset) is not None) == blocked


def test_created_path_is_also_protected():
    assert correction_guard('cache', '/cache', source='位置 cache', offset=3) is not None


@pytest.mark.parametrize("offset", [None, -1, 1, True])
def test_inconsistent_owned_position_is_rejected(offset):
    assert correction_guard('a', 'b', source='a', offset=offset) is not None


@pytest.mark.parametrize("before,after", [("三", "两"), ("两", "三"), ("２", "３")])
def test_existing_number_rejections_are_regression_only(before, after):
    # These ALREADY reject in c43ba2f. Do not report them as newly fixed.
    assert correction_guard(before, after) is not None
