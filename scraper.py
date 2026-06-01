import argparse
import random
import time
import os
import re
import sys
import pandas as pd
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ==========================================
# 配置与全局变量定义区
# ==========================================
INPUT_EXCEL = "accounts.xlsx"
OUTPUT_EXCEL = "weiq_results.xlsx"
STATE_JSON = "state.json"
DEFAULT_PROBE_UIDS = ["2115314532", "6557986019", "5099051423", "7331622139"]

global_request_count = 0

def init_env():
    if os.path.exists(OUTPUT_EXCEL):
        try:
            df_old = pd.read_excel(OUTPUT_EXCEL)
            required_cols = ["粉丝数", "最低阅读量", "认证等级"]
            if any(col not in df_old.columns for col in required_cols):
                backup_name = f"weiq_results_旧版备份_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
                os.rename(OUTPUT_EXCEL, backup_name)
                print(f"[系统保护] 检测到旧版本或结构不匹配的表格，已自动重命名为: {backup_name}")
                print(f"[系统保护] 本次运行将创建一张包含15项核心指标 + 认证等级的新结果表。\n")
        except Exception:
            os.remove(OUTPUT_EXCEL)

def init_browser(p):
    print("[初始化] 正在启动浏览器...")
    browser = p.chromium.launch(headless=False)
    
    if os.path.exists(STATE_JSON):
        print(f"[初始化] 检测到凭证文件 {STATE_JSON}，尝试以已登录状态恢复会话。")
        context = browser.new_context(storage_state=STATE_JSON)
    else:
        print(f"[警告] 未检测到凭证文件，将以未登录状态启动！")
        context = browser.new_context()
        
    page = context.new_page()
    return browser, context, page


def parse_args():
    parser = argparse.ArgumentParser(description="WEIQ 账号详情采集")
    parser.add_argument(
        "--probe-verify",
        action="store_true",
        help="运行认证等级探针（输出结构化证据映射，不修改采集流程）",
    )
    parser.add_argument(
        "--probe-uids",
        default=",".join(DEFAULT_PROBE_UIDS),
        help="探针 uid 列表，逗号分隔；默认使用 4 个验收样本",
    )
    return parser.parse_args()


def parse_probe_uids(raw):
    return [x.strip() for x in str(raw).split(",") if x.strip()]


def _normalize_signal_text(raw):
    text = str(raw or "").strip().lower()
    text = text.replace("：", ":")
    text = re.sub(r"\s+", "", text)
    return text


def _match_verify_level_from_signals(signals):
    normalized = " | ".join(_normalize_signal_text(x) for x in signals if x)
    if not normalized:
        return None

    gold_tokens = ["金v", "goldv", "gold_v", "gold-", "v-gold", "v_gold", "goldenv", "金红"]
    orange_tokens = ["橙v", "orangev", "orange_v", "orange-", "v-orange", "v_orange"]
    yellow_tokens = ["黄v", "yellowv", "yellow_v", "yellow-", "v-yellow", "v_yellow"]

    for token in gold_tokens:
        if token in normalized:
            return "金V"
    for token in orange_tokens:
        if token in normalized:
            return "橙V"
    for token in yellow_tokens:
        if token in normalized:
            return "黄V"

    return None


def _extract_api_verify_clues(payload):
    clues = []
    max_clues = 200
    verify_key_tokens = ("verify", "verified", "auth", "vip", "renzheng", "认证", "vtype", "v_type", "badge")

    def walk(node, path="root", depth=0):
        if depth > 8 or len(clues) >= max_clues:
            return

        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k)
                next_path = f"{path}.{key}"
                key_l = key.lower()
                if any(t in key_l for t in verify_key_tokens):
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        clues.append(f"{next_path}={v}")
                walk(v, next_path, depth + 1)
        elif isinstance(node, list):
            for i, item in enumerate(node[:60]):
                walk(item, f"{path}[{i}]", depth + 1)
        elif isinstance(node, str):
            node_l = node.lower()
            if any(t in node_l for t in verify_key_tokens):
                clues.append(f"{path}={node}")

    walk(payload)
    return clues


def _resolve_verify_level_from_api_payloads(api_payloads):
    all_clues = []
    for item in api_payloads:
        for clue in item.get("clues", []):
            all_clues.append(clue)
    return _match_verify_level_from_signals(all_clues), all_clues


def _probe_verify_dom(page):
    js = r"""
    () => {
      const out = {
        has_verify_icon: false,
        has_verify_text: false,
        signals: [],
        top_lines: []
      };

      const all = Array.from(document.querySelectorAll('*'));
      let uidEl = all.find(el => (el.innerText || '').includes('UID：') || (el.innerText || '').includes('UID:'));
      let card = null;

      if (uidEl) {
        let p = uidEl;
        for (let i = 0; i < 7 && p; i++) {
          const t = p.innerText || '';
          if (t.includes('粉丝数') && t.includes('博文总数')) {
            card = p;
            break;
          }
          p = p.parentElement;
        }
      }

      if (!card) {
        card = all.find(el => {
          const t = el.innerText || '';
          return t.includes('UID') && t.includes('粉丝数') && t.includes('博文总数');
        }) || null;
      }

      if (!card) {
        return out;
      }

      const topLines = (card.innerText || '').split(/\n+/).map(s => s.trim()).filter(Boolean).slice(0, 20);
      out.top_lines = topLines;
      out.has_verify_text = topLines.some(line => line.includes('认证信息'));

      const nodes = Array.from(card.querySelectorAll('img,svg,use,i,span,em,a,div'));
      const signalSet = new Set();
      const sizeHintNodes = [];

      for (const node of nodes) {
        const tag = node.tagName.toLowerCase();
        const attrs = [];
        for (const key of ['class', 'src', 'href', 'xlink:href', 'style', 'title', 'aria-label', 'alt', 'data-type', 'data-level', 'data-verify', 'data-vip']) {
          const v = node.getAttribute && node.getAttribute(key);
          if (v) attrs.push(`${key}=${v}`);
        }
        const raw = attrs.join(' ').toLowerCase();
        const hasKeyword = /(verify|verified|auth|badge|vip|renzheng|认证|gold|orange|yellow|huang|cheng|jin|金v|橙v|黄v|\\bv\\b)/.test(raw);
        if (hasKeyword && raw.length > 0) {
          signalSet.add(raw);
        }

        if (tag === 'img' || tag === 'svg' || tag === 'use') {
          const rect = node.getBoundingClientRect();
          if (rect.width > 0 && rect.height > 0 && rect.width <= 24 && rect.height <= 24) {
            sizeHintNodes.push(node);
          }
        }
      }

      out.has_verify_icon = sizeHintNodes.length > 0;
      out.signals = Array.from(signalSet).slice(0, 80);
      return out;
    }
    """
    return page.evaluate(js)


def extract_verify_level(page, api_verify_payloads):
    api_level, api_clues = _resolve_verify_level_from_api_payloads(api_verify_payloads)
    if api_level:
        return api_level, {
            "source": "api",
            "clues": api_clues[:20],
        }

    dom_probe = _probe_verify_dom(page)
    dom_level = _match_verify_level_from_signals(dom_probe.get("signals", []))
    if dom_level:
        return dom_level, {
            "source": "dom",
            "signals": dom_probe.get("signals", [])[:20],
            "top_lines": dom_probe.get("top_lines", [])[:12],
        }

    has_verify_text = bool(dom_probe.get("has_verify_text"))
    has_verify_icon = bool(dom_probe.get("has_verify_icon"))
    if has_verify_icon or has_verify_text:
        return "unknown", {
            "source": "dom_unknown",
            "signals": dom_probe.get("signals", [])[:20],
            "top_lines": dom_probe.get("top_lines", [])[:12],
        }
    return "无认证", {
        "source": "dom_none",
        "top_lines": dom_probe.get("top_lines", [])[:12],
    }


def _start_verify_response_capture(page):
    api_payloads = []

    def _on_response(response):
        try:
            ctype = (response.headers or {}).get("content-type", "").lower()
            if "json" not in ctype:
                return
            url_l = response.url.lower()
            if not any(t in url_l for t in ["weibo", "detail", "account", "user", "profile", "weiq"]):
                return
            payload = response.json()
            clues = _extract_api_verify_clues(payload)
            if clues:
                api_payloads.append({"url": response.url, "clues": clues[:120]})
        except Exception:
            pass

    page.on("response", _on_response)
    return api_payloads, _on_response


def _stop_verify_response_capture(page, handler):
    try:
        page.remove_listener("response", handler)
    except Exception:
        pass

def extract_metrics(page):
    # 【已修正】将“粉丝数量”更正为“粉丝数”
    target_keys = [
        "粉丝数", "直发CPM", "阅读中位数", "直发阅读中位数", "转发阅读中位数",
        "互动中位数", "直发互动中位数", "转发互动中位数", "发布博文数", 
        "转发中位数", "评论中位数", "点赞中位数", 
        "最低阅读量", "最高阅读量", "阅读量均值"
    ]
    
    results = {k: "空" for k in target_keys}
    
    js_extract_logic = r"""
    (keyword) => {
        const target = keyword.toUpperCase();
        let elements = Array.from(document.querySelectorAll('*'))
            .filter(el => el.childElementCount === 0 && el.textContent.trim().toUpperCase() === target);
            
        if (elements.length === 0) {
            elements = Array.from(document.querySelectorAll('*'))
                .filter(el => el.childElementCount === 0 && el.textContent.toUpperCase().includes(target));
        }
        
        if (elements.length === 0) return "空_无标签";
        
        const labelEl = elements[0];
        
        let parent = labelEl.parentElement;
        for (let i = 0; i < 4; i++) {
            if (parent) {
                let textContent = parent.innerText || '';
                let lines = textContent.split(/[\n\r]+/).map(s => s.trim()).filter(Boolean);
                let idx = lines.findIndex(s => s.toUpperCase() === target);
                
                if (idx !== -1 && idx + 1 < lines.length) {
                    let candidate = lines[idx + 1];
                    if (/^[\d,.]+[万wWkK]?$/.test(candidate) || candidate === '-' || candidate.includes('%')) {
                        return candidate;
                    }
                }
            }
            parent = parent ? parent.parentElement : null;
        }
        
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
        let currentNode = walker.nextNode();
        let found = false;
        while (currentNode) {
            if (currentNode.parentElement === labelEl || currentNode.nodeValue.toUpperCase().includes(target)) {
                found = true;
                break;
            }
            currentNode = walker.nextNode();
        }
        
        if (found) {
            currentNode = walker.nextNode();
            let attempt = 0;
            while(currentNode && attempt < 15) {
                let txt = currentNode.nodeValue.trim();
                if (txt && !['¥', '￥', ':', '：', '-', '/'].includes(txt) && txt.toUpperCase() !== target) {
                    return txt;
                }
                currentNode = walker.nextNode();
                attempt++;
            }
        }
        return "空_无数据";
    }
    """
    
    for key in target_keys:
        try:
            val = page.evaluate(js_extract_logic, key)
            if val:
                results[key] = val
        except Exception:
            pass
            
    return results

def check_anti_spider(page):
    needs_manual = False
    current_url = page.url.lower()
    
    if "login" in current_url or "passport" in current_url:
        print("\n\a[风控警告] 当前页面被重定向到了登录页！")
        needs_manual = True
        
    try:
        anti_keywords = ["滑动验证", "安全访问验证", "请输入验证码", "访问过于频繁"]
        page_text = page.locator("body").inner_text(timeout=2000)
        if any(kw in page_text for kw in anti_keywords):
            print(f"\n\a[风控警告] 页面命中风控拦截！当前 URL: {page.url}")
            needs_manual = True
    except Exception:
        pass

    if needs_manual:
        print(">>>>> 请立即在弹出的浏览器视窗中手动登录或滑块验证 <<<<<")
        input("请在手动处理完毕（并确保页面已加载出正常数据面板）后，按回车键继续...")
        print("[恢复] 继续执行爬虫流程。\n")
        time.sleep(3)

def append_to_excel(row_dict):
    df_new = pd.DataFrame([row_dict])
    
    if not os.path.exists(OUTPUT_EXCEL):
        df_new.to_excel(OUTPUT_EXCEL, index=False)
    else:
        with pd.ExcelWriter(OUTPUT_EXCEL, mode="a", engine="openpyxl", if_sheet_exists="overlay") as writer:
            start_row = writer.sheets["Sheet1"].max_row
            df_new.to_excel(writer, index=False, header=False, startrow=start_row)

def process_account_url(page, account_id, url, current_idx, total_accounts, metrics_enabled=True):
    global global_request_count
    progress = f"[{current_idx}/{total_accounts}]"
    
    print(f"\n{progress} ----------------------------------------------------")
    print(f"{progress} [ID: {account_id}] 正在访问页面...")
    
    global_request_count += 1
    if global_request_count > 1 and global_request_count % 50 == 0:
        print(f"\n{progress} [机制] 触发防封锁休眠保护！")
        for remaining in range(180, 0, -1):
            sys.stdout.write(f"\r{progress} [冷却倒计时] 还需休眠 {remaining:3d} 秒...")
            sys.stdout.flush()
            time.sleep(1)
        print(f"\n{progress} [机制] 冷却结束，恢复执行。\n")

    default_keys = [
        "粉丝数", "直发CPM", "阅读中位数", "直发阅读中位数", "转发阅读中位数",
        "互动中位数", "直发互动中位数", "转发互动中位数", "发布博文数", 
        "转发中位数", "评论中位数", "点赞中位数", 
        "最低阅读量", "最高阅读量", "阅读量均值"
    ]
    extracted_data = {k: "空" for k in default_keys}
    verify_level = "unknown"
    verify_debug = {}
    api_verify_payloads, verify_handler = _start_verify_response_capture(page)
    
    try:
        response = page.goto(url, timeout=45000, wait_until="domcontentloaded")
        
        if response is None or response.status >= 400:
            print(f"{progress} ❌ 页面拦截，状态码: {response.status if response else 'Null'}")
            return {k: "异常_阻断" for k in default_keys}, verify_level, verify_debug
            
        sys.stdout.write(f"\r{progress} 页面抵达，正在执行深度滚动触发懒加载...")
        sys.stdout.flush()
        page.evaluate("""
            () => {
                return new Promise((resolve) => {
                    let totalHeight = 0;
                    let distance = 500;
                    let timer = setInterval(() => {
                        let scrollHeight = document.body.scrollHeight;
                        window.scrollBy(0, distance);
                        totalHeight += distance;
                        if(totalHeight >= scrollHeight){
                            clearInterval(timer);
                            window.scrollTo(0, 0); 
                            resolve();
                        }
                    }, 250);
                });
            }
        """)
        print("")
            
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
            
        sleep_t2 = random.randint(2, 4)
        for remaining in range(sleep_t2, 0, -1):
            sys.stdout.write(f"\r{progress} 数据装配等待中: {remaining} 秒...")
            sys.stdout.flush()
            time.sleep(1)
        print("")
        
        check_anti_spider(page)
        verify_level, verify_debug = extract_verify_level(page, api_verify_payloads)
        if metrics_enabled:
            extracted_data = extract_metrics(page)
        
        if metrics_enabled:
            valid_count = sum(1 for v in extracted_data.values() if "空" not in str(v))
            
            # 增加无效页面诊断提示
            if valid_count == 0:
                print(f"{progress} ⚠️ 页面似乎为空白，博主可能已下架、换号或未被收录。")
                extracted_data = {k: "账号失效/未收录" for k in default_keys}
            else:
                print(f"{progress} ✅ 成功提取 {valid_count} 项核心指标。")
        else:
            print(f"{progress} ✅ 认证识别完成：{verify_level}")
        
    except PlaywrightTimeoutError:
        print(f"{progress} ❌ 页面响应超时。")
        extracted_data = {k: "超时" for k in default_keys}
        verify_level = "unknown"
    except Exception as e:
        print(f"{progress} ❌ 读取报错: {str(e)}")
        extracted_data = {k: "挂起" for k in default_keys}
        verify_level = "unknown"
    finally:
        _stop_verify_response_capture(page, verify_handler)
        
    return extracted_data, verify_level, verify_debug


def run_verify_probe(page, probe_uids):
    if not probe_uids:
        print("[探针] 未提供有效 uid，跳过认证探针。")
        return

    print("\n============================================")
    print("[探针] 开始执行认证等级结构化探针（4样本验收）")
    print("============================================")

    rows = []
    total = len(probe_uids)
    for i, uid in enumerate(probe_uids, 1):
        probe_id = f"PROBE_UID_{uid}"
        url = f"https://weiq.com/client/product/weibo/detail?account_uid={uid}"
        _, level, debug = process_account_url(
            page,
            probe_id,
            url,
            i,
            total,
            metrics_enabled=False,
        )
        source = debug.get("source", "unknown")
        clue_preview = ""
        if debug.get("clues"):
            clue_preview = str(debug["clues"][0])[:120]
        elif debug.get("signals"):
            clue_preview = str(debug["signals"][0])[:120]
        rows.append({
            "uid": uid,
            "认证等级": level,
            "证据来源": source,
            "线索预览": clue_preview or "-",
        })

    print("\n[探针结果] 认证等级映射预览")
    print("-" * 95)
    print(f"{'uid':<14} {'认证等级':<8} {'证据来源':<12} 线索预览")
    print("-" * 95)
    for row in rows:
        print(f"{row['uid']:<14} {row['认证等级']:<8} {row['证据来源']:<12} {row['线索预览']}")
    print("-" * 95)
    print("[探针说明] 若结果为 unknown，表示结构化信号不足或冲突，不会回退到颜色识别。")

def main():
    args = parse_args()
    init_env()
    
    if not os.path.exists(INPUT_EXCEL):
        print(f"[错误] 无法定位到输入表: {INPUT_EXCEL}")
        return

    try:
        df = pd.read_excel(INPUT_EXCEL)
    except Exception as e:
        print(f"[错误] Excel 读取失败: {str(e)}")
        return
        
    total_accounts = len(df)
    print(f"[系统] 准备执行 {total_accounts} 个任务，15 项核心指标 + 认证等级监控启动。")

    with sync_playwright() as p:
        browser, context, page = init_browser(p)
        
        if not os.path.exists(STATE_JSON):
            print("\n============================================")
            page.goto("https://www.weiq.com/", timeout=60000)
            print(">>>>> 请在派出的浏览器窗口中完成登录 <<<<<")
            input("====> 等你【确定登录成功】且进到操作大厅了，再点击终端并在键盘敲【回车键】发车：")
            context.storage_state(path=STATE_JSON)
            print("============================================\n")

        if args.probe_verify:
            probe_uids = parse_probe_uids(args.probe_uids)
            run_verify_probe(page, probe_uids)

        for index, row in df.iterrows():
            current_idx = index + 1
            aid = row.get("账号ID")
            uid = row.get("uid")
            
            if pd.isna(uid) or str(uid).strip() == "":
                print(f"\n[{current_idx}/{total_accounts}] ⚠️ [ID: {aid}] 丢失 uid 链接参数，路过。")
                continue
            
            aid = str(aid).strip()
            uid = str(uid).strip()
            url = f"https://weiq.com/client/product/weibo/detail?account_uid={uid}"
            
            metrics_dict, verify_level, _ = process_account_url(
                page, aid, url, current_idx, total_accounts, metrics_enabled=True
            )
            
            result_row = {"账号ID": aid, "uid": uid, "主页链接": url, "认证等级": verify_level}
            result_row.update(metrics_dict)
            
            try:
                append_to_excel(result_row)
            except Exception as e:
                print(f"[{current_idx}/{total_accounts}] ❌ 写入磁盘失败: {str(e)}")
                
        context.storage_state(path=STATE_JSON)
        print("\n============================================")
        print(f"[系统] 全部任务运行结束！请查看 {OUTPUT_EXCEL} 文件。")
        print("============================================")
        browser.close()

if __name__ == "__main__":
    main()
