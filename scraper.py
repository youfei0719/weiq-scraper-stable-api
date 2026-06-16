import argparse
import sys

from scraper_runtime import CrawlConfig, DEFAULT_PROBE_UIDS, parse_probe_uids, run_crawl


def parse_args():
    parser = argparse.ArgumentParser(description="WEIQ 账号详情采集")
    parser.add_argument("--input-excel", default="accounts.xlsx", help="输入账号 Excel 路径")
    parser.add_argument("--output-dir", default=".", help="结果输出目录")
    parser.add_argument("--output-excel", default=None, help="结果 Excel 完整路径")
    parser.add_argument("--state-storage", default="state.json", help="Playwright storage_state 路径")
    parser.add_argument("--headless", action="store_true", help="使用无头浏览器运行")
    parser.add_argument("--login-url", default="https://www.weiq.com/", help="缺少登录态时打开的登录页")
    parser.add_argument("--probe-verify", action="store_true", help="运行认证等级探针")
    parser.add_argument(
        "--probe-uids",
        default=",".join(DEFAULT_PROBE_UIDS),
        help="探针 uid 列表，逗号分隔",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_crawl(
        CrawlConfig(
            input_excel=args.input_excel,
            output_dir=args.output_dir,
            output_excel=args.output_excel,
            state_storage=args.state_storage,
            headless=args.headless,
            probe_verify=args.probe_verify,
            probe_uids=parse_probe_uids(args.probe_uids),
            require_login=True,
            prompt_for_login_if_missing=True,
            wait_on_anti_spider=True,
            save_storage_state=True,
            login_url=args.login_url,
        )
    )
    if result.status.value != "SUCCESS":
        print(f"[错误] {result.message}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
