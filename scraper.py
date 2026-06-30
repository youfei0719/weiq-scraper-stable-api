import sys

from scraper_runtime import StateStore, main as runtime_main


LEGACY_COMPAT_FLAGS = {"--probe-verify", "--probe-uids"}


def _normalize_legacy_argv(argv: list[str]) -> list[str]:
    normalized: list[str] = [argv[0]]
    skip_next = False
    ignored_flags: list[str] = []

    for index, arg in enumerate(argv[1:], start=1):
        if skip_next:
            skip_next = False
            continue
        if arg == "--probe-verify":
            ignored_flags.append(arg)
            continue
        if arg == "--probe-uids":
            ignored_flags.append(arg)
            if index + 1 < len(argv):
                skip_next = True
            continue
        normalized.append(arg)

    if ignored_flags:
        print(f"[兼容层] 已忽略旧版参数: {', '.join(ignored_flags)}")
        print("[兼容层] 当前入口已统一到 scraper_runtime，认证探针请改为直接访问样本页人工验真。")
    return normalized


def main() -> None:
    sys.argv = _normalize_legacy_argv(sys.argv)
    runtime_main()


if __name__ == "__main__":
    main()
