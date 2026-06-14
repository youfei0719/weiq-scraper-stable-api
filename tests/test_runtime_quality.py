import json
import tempfile
from pathlib import Path
import unittest

from scraper_runtime import (
    CRITICAL_METRIC_KEYS,
    ErrorCode,
    METRIC_KEYS,
    _count_effective_metrics,
    has_usable_storage_state,
    infer_blocked_response_issue,
    infer_post_extraction_issue,
)


class RuntimeQualityTest(unittest.TestCase):
    def test_empty_storage_state_is_treated_as_not_logged_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
            self.assertFalse(has_usable_storage_state(str(path)))

    def test_blocked_response_with_login_hints_is_treated_as_auth_required(self) -> None:
        class _LocatorItem:
            def is_visible(self) -> bool:
                return True

        class _Locator:
            def count(self) -> int:
                return 1

            def nth(self, index: int) -> _LocatorItem:  # noqa: ARG002
                return _LocatorItem()

            def inner_text(self, timeout: int = 2000) -> str:  # noqa: ARG002
                return "请先登录后查看完整数据，支持账号密码登录"

        class _Page:
            url = "https://www.weiq.com/login"

            def locator(self, selector: str) -> _Locator:  # noqa: ARG002
                return _Locator()

        issue = infer_blocked_response_issue(_Page())
        self.assertEqual(issue, ErrorCode.AUTH_REQUIRED)

    def test_effective_metric_count_supports_w_suffix(self) -> None:
        extracted = {key: "空_无标签" for key in METRIC_KEYS}
        extracted["粉丝数"] = "126w"
        extracted["直发CPM"] = "12.5"

        self.assertEqual(_count_effective_metrics(extracted, CRITICAL_METRIC_KEYS), 2)

    def test_soft_login_wall_is_treated_as_auth_required(self) -> None:
        extracted = {key: "空_无标签" for key in METRIC_KEYS}
        extracted["粉丝数"] = "126w"

        issue = infer_post_extraction_issue(
            page_url="https://weiq.com/client/product/weibo/detail?account_uid=5868150992",
            page_text="请先登录后查看完整数据，支持账号密码登录或手机验证码登录",
            has_login_form=True,
            extracted_data=extracted,
        )

        self.assertEqual(issue, ErrorCode.AUTH_REQUIRED)

    def test_sparse_core_metrics_without_login_hints_is_treated_as_empty_page(self) -> None:
        extracted = {key: "空_无标签" for key in METRIC_KEYS}
        extracted["粉丝数"] = "126w"
        extracted["互动中位数"] = "4"
        extracted["评论中位数"] = "1"
        extracted["点赞中位数"] = "3"

        issue = infer_post_extraction_issue(
            page_url="https://weiq.com/client/product/weibo/detail?account_uid=5868150992",
            page_text="账号详情页",
            has_login_form=False,
            extracted_data=extracted,
        )

        self.assertEqual(issue, ErrorCode.EMPTY_PAGE)


if __name__ == "__main__":
    unittest.main()
