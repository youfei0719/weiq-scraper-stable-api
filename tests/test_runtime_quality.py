import unittest

from scraper_runtime import (
    CRITICAL_METRIC_KEYS,
    ErrorCode,
    METRIC_KEYS,
    _count_effective_metrics,
    infer_post_extraction_issue,
)


class RuntimeQualityTest(unittest.TestCase):
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
