import json
import tempfile
from pathlib import Path
import unittest

from scraper_runtime import (
    CRITICAL_METRIC_KEYS,
    ErrorCode,
    METRIC_KEYS,
    PROFILE_RESULT_KEYS,
    _count_effective_metrics,
    _resolve_verification_level_from_probe,
    has_usable_storage_state,
    is_browser_session_closed_error,
    infer_blocked_response_issue,
    infer_post_extraction_issue,
    select_startup_state_file,
)


class RuntimeQualityTest(unittest.TestCase):
    def test_empty_storage_state_is_treated_as_not_logged_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
            self.assertFalse(has_usable_storage_state(str(path)))

    def test_select_startup_state_file_prefers_task_state_then_long_term_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task_state = Path(temp_dir) / "storage_state.json"
            long_term_state = Path(temp_dir) / "state.json"
            long_term_state.write_text(json.dumps({"cookies": [{"name": "sid"}], "origins": []}), encoding="utf-8")
            self.assertEqual(select_startup_state_file(str(task_state), str(long_term_state)), str(long_term_state))

            task_state.write_text(json.dumps({"cookies": [{"name": "sid-task"}], "origins": []}), encoding="utf-8")
            self.assertEqual(select_startup_state_file(str(task_state), str(long_term_state)), str(task_state))

    def test_browser_closed_error_is_marked_recoverable(self) -> None:
        exc = RuntimeError("Page.goto: Target page, context or browser has been closed")
        self.assertTrue(is_browser_session_closed_error(exc))

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

    def test_metric_keys_include_extended_content_fields(self) -> None:
        for field in ["供稿直发", "供稿转发", "头条文章", "点评", "原创图文", "原创视频"]:
            self.assertIn(field, METRIC_KEYS)
        self.assertEqual(PROFILE_RESULT_KEYS, ["认证等级"])

    def test_resolve_verification_level_from_probe_supports_exact_svg_fill_map(self) -> None:
        level = _resolve_verification_level_from_probe(
            {
                "has_profile_card": True,
                "has_name_row": True,
                "has_verify_icon": True,
                "path_fills": ["#FFF", "#F6CA45", "#FFF"],
                "verify_text_value": "",
            }
        )
        self.assertEqual(level, "黄V")

    def test_resolve_verification_level_from_probe_supports_unverified_hint(self) -> None:
        level = _resolve_verification_level_from_probe(
            {
                "has_profile_card": True,
                "has_name_row": True,
                "has_verify_icon": False,
                "has_verify_text": False,
                "has_unverified_hint": True,
                "verify_text_value": "无",
            }
        )
        self.assertEqual(level, "无认证")


if __name__ == "__main__":
    unittest.main()
