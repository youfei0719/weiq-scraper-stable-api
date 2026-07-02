import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest

from scraper_runtime import (
    AccountStatus,
    CrawlConfig,
    CrawlHooks,
    WEIQ_STARTUP_URLS,
    CRITICAL_METRIC_KEYS,
    ErrorCode,
    METRIC_KEYS,
    PROFILE_RESULT_KEYS,
    _count_effective_metrics,
    _resolve_verification_level_from_probe,
    get_browser_context_kwargs,
    get_playwright_launch_kwargs,
    has_usable_storage_state,
    is_browser_error_page,
    is_browser_session_closed_error,
    infer_blocked_response_issue,
    infer_post_extraction_issue,
    process_account_url,
    select_startup_state_file,
    verify_homepage_login_state,
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

    def test_browser_error_page_is_detected_as_navigation_error(self) -> None:
        class _Page:
            url = "https://www.weiq.com/"

            def is_closed(self) -> bool:
                return False

            def locator(self, selector: str):  # noqa: ARG002
                class _Locator:
                    def inner_text(self, timeout: int = 2000) -> str:  # noqa: ARG002
                        return "无法访问此网站 www.weiq.com 的响应时间过长。ERR_TIMED_OUT"

                    def count(self) -> int:
                        return 0

                return _Locator()

        self.assertTrue(is_browser_error_page(_Page()))
        verified, code = verify_homepage_login_state(_Page())
        self.assertFalse(verified)
        self.assertEqual(code, ErrorCode.NAVIGATION_ERROR)

    def test_startup_urls_use_non_www_first(self) -> None:
        self.assertEqual(WEIQ_STARTUP_URLS[0], "https://weiq.com/")

    def test_default_browser_launch_bypasses_broken_system_proxy(self) -> None:
        with patch.dict("os.environ", {"WEIQ_PROXY_SERVER": "", "WEIQ_USE_SYSTEM_PROXY": ""}, clear=False):
            kwargs = get_playwright_launch_kwargs(headless=False)

        self.assertFalse(kwargs["headless"])
        self.assertIn("--proxy-server=direct://", kwargs["args"])
        self.assertIn("--proxy-bypass-list=*", kwargs["args"])
        self.assertNotIn("proxy", kwargs)

    def test_explicit_browser_proxy_still_takes_precedence(self) -> None:
        with patch.dict("os.environ", {"WEIQ_PROXY_SERVER": "http://127.0.0.1:7897"}, clear=False):
            kwargs = get_playwright_launch_kwargs(headless=True)

        self.assertTrue(kwargs["headless"])
        self.assertEqual(kwargs["proxy"]["server"], "http://127.0.0.1:7897")
        self.assertNotIn("args", kwargs)

    def test_browser_context_uses_desktop_user_agent(self) -> None:
        with patch.dict("os.environ", {"WEIQ_BROWSER_USER_AGENT": ""}, clear=False):
            kwargs = get_browser_context_kwargs(None)

        self.assertIn("Mozilla/5.0", kwargs["user_agent"])
        self.assertIn("Chrome/", kwargs["user_agent"])
        self.assertNotIn("storage_state", kwargs)

    def test_auth_recovery_retries_same_account_even_when_retry_times_is_one(self) -> None:
        class _Response:
            status = 200

        class _Page:
            url = "https://weiq.com/client/product/weibo/detail?account_uid=1"

            def wait_for_load_state(self, state: str, timeout: int = 0) -> None:  # noqa: ARG002
                return None

        session = {"page": _Page(), "context": object(), "state_file": None}
        config = CrawlConfig(retry_times=1, retry_backoff_seconds=0, wait_min_seconds=0, wait_max_seconds=0)
        hooks = CrawlHooks()
        extracted = {key: "空_无标签" for key in METRIC_KEYS}
        extracted["粉丝数"] = "100万"
        extracted["直发CPM"] = "12.3"
        extracted["阅读中位数"] = "3万"
        extracted["发布博文数"] = "20"

        with patch("scraper_runtime.ensure_browser_session"), patch(
            "scraper_runtime.goto_with_recovery", side_effect=[_Response(), _Response()]
        ) as goto_mock, patch("scraper_runtime.perform_lazy_scroll"), patch(
            "scraper_runtime.detect_auth_or_challenge",
            side_effect=[(True, ErrorCode.AUTH_REQUIRED), (False, ErrorCode.NONE)],
        ), patch(
            "scraper_runtime.recover_account_session_after_auth",
            return_value=(True, ErrorCode.NONE),
        ) as recover_mock, patch(
            "scraper_runtime.extract_metrics", return_value=extracted
        ), patch(
            "scraper_runtime.extract_verification_level", return_value="橙V"
        ), patch(
            "scraper_runtime._safe_body_text", return_value="账号详情页"
        ), patch(
            "scraper_runtime._page_has_visible_login_form", return_value=False
        ), patch(
            "scraper_runtime.infer_post_extraction_issue", return_value=ErrorCode.NONE
        ):
            result = process_account_url(
                playwright_obj=object(),
                session=session,
                account_id="测试账号",
                url="https://weiq.com/client/product/weibo/detail?account_uid=1",
                current_idx=1,
                total_accounts=1,
                config=config,
                hooks=hooks,
            )

        self.assertEqual(result.account_status, AccountStatus.SUCCESS)
        self.assertEqual(result.error_code, ErrorCode.NONE)
        self.assertEqual(result.metrics["认证等级"], "橙V")
        self.assertEqual(goto_mock.call_count, 2)
        recover_mock.assert_called_once()

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
