import argparse
import random
import time
import os
import re
import sys
import json
import io
import pandas as pd
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import colorsys
from scraper_runtime import StateStore

# ==========================================
# 配置与全局变量定义区
# ==========================================
INPUT_EXCEL = "accounts.xlsx"
OUTPUT_EXCEL = "weiq_results.xlsx"
STATE_JSON = "state.json"
DEFAULT_PROBE_UIDS = ["2115314532", "6557986019", "5099051423", "7331622139"]
VERIFY_FILL_MAP = {
    ("#FFFFFF", "#F6CA45", "#FFFFFF"): "黄V",
    ("#FFFFFF", "#FF6C00", "#FFFFFF"): "橙V",
    ("#FEFF78", "#CD3620", "#FEFF78"): "金V",
}

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


def _normalize_hex_color(raw):
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    if re.fullmatch(r"#[0-9A-F]{3}", text):
        return "#" + "".join(ch * 2 for ch in text[1:])
    if re.fullmatch(r"#[0-9A-F]{6}", text):
        return text
    m = re.search(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", text.lower())
    if m:
        return "#{:02X}{:02X}{:02X}".format(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return text


def _to_number_color_triplet(raw):
    if not raw:
        return None
    m = re.search(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", str(raw).lower())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _color_distance(c1, c2):
    return ((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2) ** 0.5


def _rgb_to_hsv_255(r, g, b):
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    return h * 360.0, s * 255.0, v * 255.0


def _classify_hue_bucket(h):
    # 经验区间：金红(偏红) < 橙 < 黄
    if h < 12:
        return "金V"
    if h < 42:
        return "橙V"
    if h < 70:
        return "黄V"
    return None


def _match_verify_level_from_rgb_text_signals(signals):
    votes = {"金V": 0.0, "橙V": 0.0, "黄V": 0.0}
    for s in signals:
        text = str(s or "").lower()
        for m in re.finditer(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", text):
            r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
            h, sat, val = _rgb_to_hsv_255(r, g, b)
            if sat < 35 or val < 60:
                continue
            level = _classify_hue_bucket(h)
            if not level:
                continue
            votes[level] += (sat / 255.0) * (val / 255.0)
    total = sum(votes.values())
    if total <= 0:
        return None
    ordered = sorted(votes.items(), key=lambda x: x[1], reverse=True)
    top_level, top_score = ordered[0]
    second_score = ordered[1][1]
    ratio = top_score / total
    margin = (top_score - second_score) / total
    if top_level == "金V":
        if ratio >= 0.72 and margin >= 0.26:
            return "金V"
        if votes["橙V"] >= top_score * 0.55:
            return "橙V"
        return None
    if ratio >= 0.52 and margin >= 0.10:
        return top_level
    return None


def _match_verify_level_by_semantic_color(signals):
    # 仅在“昵称邻域已确认有认证图标但缺少语义 token”时作为样式语义兜底，不做像素取色。
    colors = []
    for s in signals:
        t = str(s).lower()
        if "style:color=" in t or "style:fill=" in t or "style:stroke=" in t:
            for part in re.split(r"[;|]", t):
                if "rgb(" in part:
                    c = _to_number_color_triplet(part)
                    if c:
                        colors.append(c)
    if not colors:
        return None

    # 排除低饱和灰阶，减少误把正文黑灰色当成认证色
    colors = [c for c in colors if (max(c) - min(c)) >= 18]
    if not colors:
        return None

    score = {"金V": 0.0, "橙V": 0.0, "黄V": 0.0}
    total = 0.0
    for r, g, b in colors:
        h, s, v = _rgb_to_hsv_255(r, g, b)
        if s < 35 or v < 70:
            continue
        level = _classify_hue_bucket(h)
        if not level:
            continue
        w = (s / 255.0) * (v / 255.0)
        score[level] += w
        total += w

    if total <= 0:
        return None
    ordered = sorted(score.items(), key=lambda x: x[1], reverse=True)
    top_level, top_score = ordered[0]
    second_score = ordered[1][1]
    ratio = top_score / total
    margin = (top_score - second_score) / total
    # 金V门槛更严，防止橙V被暗红阴影误吸到金V
    if top_level == "金V":
        if ratio >= 0.72 and margin >= 0.26:
            return top_level
        if score.get("橙V", 0.0) >= top_score * 0.55:
            return "橙V"
        return None
    if ratio >= 0.55 and margin >= 0.15:
        return top_level
    return None


def _classify_verify_by_region_screenshot(page, dom_probe):
    try:
        from PIL import Image

        rect = dom_probe.get("name_rect") or {}
        card_rect = dom_probe.get("card_rect") or {}
        nl = float(rect.get("left", 0))
        nt = float(rect.get("top", 0))
        nw = float(rect.get("width", 0))
        nh = float(rect.get("height", 0))
        cr = float(card_rect.get("right", 0))
        cb = float(card_rect.get("bottom", 0))
        if nw <= 0 or nh <= 0:
            return None, {}

        x = max(nl + nw - 2, 0)
        y = max(nt - 6, 0)
        max_w = max(cr - x - 2, 0)
        w = min(140, max_w)
        h = min(max(nh + 12, 24), max(cb - y - 2, 0))
        if w < 12 or h < 12:
            return None, {}

        png = page.screenshot(clip={"x": x, "y": y, "width": w, "height": h})
        img = Image.open(io.BytesIO(png)).convert("RGB")
        pixels = img.get_flattened_data()
        if not pixels:
            return None, {}

        hit_counts = {"金V": 0.0, "橙V": 0.0, "黄V": 0.0}
        colorful_count = 0
        valid_count = 0
        for r, g, b in pixels:
            if max(r, g, b) - min(r, g, b) < 20:
                continue
            colorful_count += 1
            h_deg, s, v = _rgb_to_hsv_255(r, g, b)
            if s < 45 or v < 85:
                continue
            level = _classify_hue_bucket(h_deg)
            if not level:
                continue
            valid_count += 1
            hit_counts[level] += (s / 255.0) * (v / 255.0)

        # 区域内有效彩色像素过少，视为证据不足
        if valid_count < 5:
            return None, {"colorful_pixels": colorful_count, "valid_pixels": valid_count, "hit_scores": hit_counts}

        level, score = max(hit_counts.items(), key=lambda x: x[1])
        total_score = sum(hit_counts.values()) or 1.0
        ordered = sorted(hit_counts.items(), key=lambda x: x[1], reverse=True)
        ratio = score / total_score
        margin = (ordered[0][1] - ordered[1][1]) / total_score
        if level == "金V":
            if ratio >= 0.72 and margin >= 0.26:
                return level, {
                    "clip": {"x": round(x, 2), "y": round(y, 2), "width": round(w, 2), "height": round(h, 2)},
                    "colorful_pixels": colorful_count,
                    "valid_pixels": valid_count,
                    "hit_scores": hit_counts,
                    "ratio": round(ratio, 4),
                    "margin": round(margin, 4),
                }
            if hit_counts.get("橙V", 0.0) >= score * 0.55:
                return "橙V", {
                    "clip": {"x": round(x, 2), "y": round(y, 2), "width": round(w, 2), "height": round(h, 2)},
                    "colorful_pixels": colorful_count,
                    "valid_pixels": valid_count,
                    "hit_scores": hit_counts,
                    "ratio": round(ratio, 4),
                    "margin": round(margin, 4),
                    "demote_from_gold": True,
                }
            return None, {
                "colorful_pixels": colorful_count,
                "valid_pixels": valid_count,
                "hit_scores": hit_counts,
                "ratio": round(ratio, 4),
                "margin": round(margin, 4),
                "gold_conf_low": True,
            }

        if ratio >= 0.58 and margin >= 0.18:
            return level, {
                "clip": {"x": round(x, 2), "y": round(y, 2), "width": round(w, 2), "height": round(h, 2)},
                "colorful_pixels": colorful_count,
                "valid_pixels": valid_count,
                "hit_scores": hit_counts,
                "ratio": round(ratio, 4),
                "margin": round(margin, 4),
            }
        return None, {
            "colorful_pixels": colorful_count,
            "valid_pixels": valid_count,
            "hit_scores": hit_counts,
            "ratio": round(ratio, 4),
            "margin": round(margin, 4),
        }
    except Exception as e:
        return None, {"error": str(e)}


def _has_verify_semantic_hint(signals):
    normalized = " | ".join(_normalize_signal_text(x) for x in signals if x)
    if not normalized:
        return False
    return bool(
        re.search(
            r"(verify|verified|auth|badge|vip|renzheng|认证|gold|orange|yellow|huang|cheng|jin|hong|red|weibo[-_]?v|icon[-_]?v|vip[-_]?icon|金v|橙v|黄v)",
            normalized,
        )
    )


def _match_verify_level_from_signals(signals):
    normalized = " | ".join(_normalize_signal_text(x) for x in signals if x)
    if not normalized:
        return None

    gold_tokens = [
        "金v", "goldv", "gold_v", "gold-", "v-gold", "v_gold", "goldenv", "金红",
        "redv", "red_v", "v-red", "jinv", "jin_v", "v-jin", "hongv", "hong_v",
    ]
    orange_tokens = [
        "橙v", "orangev", "orange_v", "orange-", "v-orange", "v_orange",
        "chengv", "cheng_v", "v-cheng",
    ]
    yellow_tokens = [
        "黄v", "yellowv", "yellow_v", "yellow-", "v-yellow", "v_yellow",
        "huangv", "huang_v", "v-huang",
    ]

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


def _extract_json_from_text_payload(text):
    # 兼容 text/plain 包裹 JSON 的接口
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None
    if raw.startswith("{") or raw.startswith("["):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def _resolve_verify_level_from_api_payloads(api_payloads):
    all_clues = []
    for item in api_payloads:
        for clue in item.get("clues", []):
            all_clues.append(clue)
    return _match_verify_level_from_signals(all_clues), all_clues


def _extract_verify_level_from_exact_svg(page):
    js = r"""
    () => {
      const out = {
        has_profile_card: false,
        has_name_row: false,
        has_verify_icon: false,
        has_verify_text: false,
        has_unverified_hint: false,
        verify_text_value: '',
        svg_found: false,
        svg_class: '',
        svg_html: '',
        path_fills: [],
        top_lines: []
      };

      const all = Array.from(document.querySelectorAll('*'));
      const card = all.find(el => {
        const t = (el.innerText || '').trim();
        return t.includes('UID') && t.includes('粉丝数') && t.includes('博文总数');
      });
      if (!card) return out;

      out.has_profile_card = true;
      const topLines = (card.innerText || '').split(/\n+/).map(s => s.trim()).filter(Boolean).slice(0, 20);
      out.top_lines = topLines;

      const normalize = (s) => String(s || '').replace(/\s+/g, '');
      const isNegative = (s) => {
        const v = normalize(s).toLowerCase();
        if (!v) return false;
        if (/^[-—–~～_=·*xX\/]+$/.test(v)) return true;
        if (['无', '暂无', '未认证', 'none', 'null', 'na', 'n/a'].includes(v)) return true;
        return false;
      };

      let verifyTextValue = '';
      for (const line of topLines) {
        if (!line.includes('认证信息')) continue;
        const m = line.match(/认证信息\s*[：:]?\s*(.*)$/);
        const tail = m ? (m[1] || '') : line.split('认证信息').slice(1).join('');
        const cleaned = String(tail || '').trim();
        if (cleaned && !verifyTextValue) verifyTextValue = cleaned;
      }
      out.verify_text_value = verifyTextValue;
      out.has_verify_text = Boolean(verifyTextValue && !isNegative(verifyTextValue));
      out.has_unverified_hint = Boolean(verifyTextValue) && isNegative(verifyTextValue);

      const nameRow = card.querySelector('.user-name-text.pointer');
      if (!nameRow) return out;

      out.has_name_row = true;
      const svg = nameRow.querySelector('svg.gl-icon-default.icon.v.ml4');
      if (!svg) return out;

      out.has_verify_icon = true;
      out.svg_found = true;
      out.svg_class = svg.getAttribute('class') || '';
      out.svg_html = (svg.outerHTML || '').slice(0, 1600);
      out.path_fills = Array.from(svg.querySelectorAll('path'))
        .map(p => p.getAttribute('fill') || '')
        .filter(Boolean);
      return out;
    }
    """
    return page.evaluate(js)


def _probe_verify_dom(page):
    js = r"""
    () => {
      const out = {
        has_verify_icon: false,
        has_profile_card: false,
        has_verify_text: false,
        has_unverified_hint: false,
        has_name_row: false,
        verify_text_value: '',
        name_rect: null,
        card_rect: null,
        signals: [],
        name_row_signals: [],
        icon_style_signals: [],
        icon_nodes: [],
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
      out.has_profile_card = true;
      const _cr = card.getBoundingClientRect();
      out.card_rect = {left: _cr.left, top: _cr.top, right: _cr.right, bottom: _cr.bottom, width: _cr.width, height: _cr.height};

      const topLines = (card.innerText || '').split(/\n+/).map(s => s.trim()).filter(Boolean).slice(0, 20);
      out.top_lines = topLines;
      const normalize = (s) => String(s || '').replace(/\s+/g, '');
      const isNegative = (s) => {
        const v = normalize(s).toLowerCase();
        if (!v) return false;
        if (/^[-—–~～_=·*xX\/]+$/.test(v)) return true;
        if (['无', '暂无', '未认证', 'none', 'null', 'na', 'n/a'].includes(v)) return true;
        return false;
      };
      let verifyTextValue = '';
      for (const line of topLines) {
        if (!line.includes('认证信息')) continue;
        const m = line.match(/认证信息\s*[：:]?\s*(.*)$/);
        const tail = m ? (m[1] || '') : line.split('认证信息').slice(1).join('');
        const cleaned = String(tail || '').trim();
        if (cleaned && !verifyTextValue) verifyTextValue = cleaned;
      }
      out.verify_text_value = verifyTextValue;
      out.has_verify_text = Boolean(verifyTextValue && !isNegative(verifyTextValue));
      out.has_unverified_hint = Boolean(verifyTextValue) && isNegative(verifyTextValue);

      const uidTextNode = Array.from(card.querySelectorAll('*')).find(el => {
        const t = (el.innerText || '').trim();
        return t.startsWith('UID：') || t.startsWith('UID:') || /^UID[：:]\s*\d+/.test(t);
      });

      const cardRect = card.getBoundingClientRect();
      let nameRow = null;
      let nicknameEl = null;
      if (uidTextNode && uidTextNode.parentElement) {
        const siblings = Array.from(uidTextNode.parentElement.children);
        const uidIdx = siblings.indexOf(uidTextNode);
        if (uidIdx > 0) {
          const prev = siblings[uidIdx - 1];
          const prevText = (prev.innerText || '').trim();
          if (prevText && !prevText.includes('UID') && prevText.length <= 40) {
            nameRow = prev;
            nicknameEl = prev;
          }
        }
      }

      const uidRect = uidTextNode ? uidTextNode.getBoundingClientRect() : null;
      const maybeNames = Array.from(card.querySelectorAll('*')).filter(el => {
          if (!el || !el.getBoundingClientRect) return false;
          const rect = el.getBoundingClientRect();
          if (rect.width < 16 || rect.height < 12) return false;
          if (rect.top < cardRect.top || rect.bottom > cardRect.bottom + 1) return false;
          const t = (el.innerText || '').trim();
          if (!t) return false;
          if (t.includes('UID') || t.includes('粉丝数') || t.includes('博文总数') || t.includes('转评赞总数')) return false;
          if (t.length > 30) return false;
          if (uidRect) {
            if (rect.top > uidRect.top) return false;
            if ((uidRect.top - rect.top) > 80) return false;
          }
          return true;
        });

      if (!nicknameEl && maybeNames.length > 0) {
        maybeNames.sort((a, b) => {
          const ar = a.getBoundingClientRect();
          const br = b.getBoundingClientRect();
          const ay = uidRect ? Math.abs(uidRect.top - ar.top) : ar.top;
          const by = uidRect ? Math.abs(uidRect.top - br.top) : br.top;
          if (ay !== by) return ay - by;
          return ar.left - br.left;
        });
        nicknameEl = maybeNames[0];
      }

      if (!nameRow && nicknameEl) {
        nameRow = nicknameEl.parentElement || nicknameEl;
      }

      // 二次兜底：按 UID 上方近邻文本推断昵称行（解决“昵称与 UID 非同父结构”的页面）
      if (!nicknameEl && uidRect) {
        const textCandidates = Array.from(card.querySelectorAll('*')).filter(el => {
          if (!el || !el.getBoundingClientRect) return false;
          const t = (el.innerText || '').trim();
          if (!t) return false;
          if (t.includes('UID') || t.includes('粉丝数') || t.includes('博文总数') || t.includes('转评赞总数')) return false;
          if (t.length > 30) return false;
          const r = el.getBoundingClientRect();
          if (r.width < 12 || r.height < 12) return false;
          if (r.bottom > uidRect.top + 8) return false;
          if (r.top < uidRect.top - 110) return false;
          if (r.left < cardRect.left - 2 || r.right > cardRect.right + 2) return false;
          return true;
        });
        if (textCandidates.length > 0) {
          textCandidates.sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            const dy = Math.abs(uidRect.top - ar.bottom) - Math.abs(uidRect.top - br.bottom);
            if (dy !== 0) return dy;
            return (br.width - ar.width);
          });
          nicknameEl = textCandidates[0];
          nameRow = nicknameEl.parentElement || nicknameEl;
        }
      }

      if (!nameRow && maybeNames.length > 0) {
        for (const el of maybeNames) {
          if (el.querySelector && el.querySelector('img,svg,use,i,span,em')) {
            nameRow = el;
            nicknameEl = el;
            break;
          }
        }
      }

      if (!nameRow) {
        return out;
      }
      out.has_name_row = true;

      const nameRect = nicknameEl && nicknameEl.getBoundingClientRect ? nicknameEl.getBoundingClientRect() : nameRow.getBoundingClientRect();
      const rowRect = nameRow.getBoundingClientRect();
      out.name_rect = {left: nameRect.left, top: nameRect.top, right: nameRect.right, bottom: nameRect.bottom, width: nameRect.width, height: nameRect.height};

      const nodes = Array.from(nameRow.querySelectorAll('*'));
      const signalSet = new Set();
      const nameRowSignalSet = new Set();
      const iconStyleSet = new Set();
      const iconNodes = [];
      let siblingIconHit = false;
      let pseudoIconHit = false;

      const addComputedStyleSignals = (node, bucket, iconBucket) => {
        const style = window.getComputedStyle(node);
        if (!style) return;
        const keys = ['color', 'fill', 'stroke', 'backgroundImage', 'backgroundColor', 'filter'];
        for (const key of keys) {
          const v = style[key];
          if (!v) continue;
          const val = String(v).trim();
          if (!val || val === 'none') continue;
          const signal = `style:${key}=${val}`;
          bucket.add(signal);
          if (iconBucket) iconBucket.add(signal);
        }
      };

      const collectAttrs = (node) => {
        const attrs = [];
        for (const key of ['class', 'src', 'href', 'xlink:href', 'style', 'title', 'aria-label', 'alt', 'data-type', 'data-level', 'data-verify', 'data-vip', 'name']) {
          const v = node.getAttribute && node.getAttribute(key);
          if (v) attrs.push(`${key}=${v}`);
        }
        const text = ((node.textContent || '') + '').trim();
        if (text && text.length <= 20) attrs.push(`text=${text}`);
        return attrs;
      };

      const collectPseudoSignals = (node, label) => {
        if (!node || !window.getComputedStyle) return;
        for (const pseudo of ['::before', '::after']) {
          let st = null;
          try {
            st = window.getComputedStyle(node, pseudo);
          } catch (e) {
            st = null;
          }
          if (!st) continue;

          const content = String(st.content || '').trim();
          const width = String(st.width || '').trim();
          const height = String(st.height || '').trim();
          const color = String(st.color || '').trim();
          const fill = String(st.fill || '').trim();
          const stroke = String(st.stroke || '').trim();
          const bg = String(st.backgroundImage || '').trim();
          const bgc = String(st.backgroundColor || '').trim();
          const mask = String(st.maskImage || st.webkitMaskImage || '').trim();

          const styleLine = `pseudo:${label}:${pseudo}|content=${content}|w=${width}|h=${height}|color=${color}|fill=${fill}|stroke=${stroke}|bg=${bg}|bgc=${bgc}|mask=${mask}`.toLowerCase();
          nameRowSignalSet.add(styleLine);
          signalSet.add(styleLine);
          iconStyleSet.add(`style:color=${color}`);
          iconStyleSet.add(`style:fill=${fill}`);
          iconStyleSet.add(`style:stroke=${stroke}`);
          iconStyleSet.add(`style:backgroundColor=${bgc}`);

          const hasVisual = (
            (content && content !== 'none' && content !== 'normal' && content !== '""' && content !== "''") ||
            (bg && bg !== 'none') ||
            (mask && mask !== 'none')
          );
          if (hasVisual) pseudoIconHit = true;
        }
      };

      const iconSelector = 'img,svg,use,i,span,em';
      const iconCandidates = Array.from(card.querySelectorAll(iconSelector));
      for (const node of nodes) {
        const attrs = collectAttrs(node);
        const raw = attrs.join(' | ').toLowerCase();
        const hasKeyword = /(verify|verified|auth|badge|vip|renzheng|认证|gold|orange|yellow|huang|cheng|jin|hong|red|金v|橙v|黄v|\\bv\\b)/.test(raw);
        if (hasKeyword && raw.length > 0) {
          signalSet.add(raw);
          nameRowSignalSet.add(raw);
        }
      }

      const nicknameRight = nameRect.right;
      const rowTop = rowRect.top;
      const rowBottom = rowRect.bottom;
      const nameTop = nameRect.top;
      const nameBottom = nameRect.bottom;
      const nearIconCandidates = [];
      for (const node of iconCandidates) {
        if (!node || !node.getBoundingClientRect) continue;
        const rect = node.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        if (rect.width > 30 || rect.height > 30) continue;

        const centerY = rect.top + rect.height / 2;
        const alignedRow = centerY >= (rowTop - 7) && centerY <= (rowBottom + 7);
        const alignedName = centerY >= (nameTop - 8) && centerY <= (nameBottom + 8);
        const rightOfName = rect.left >= (nicknameRight - 8);
        const nearName = rect.left <= (nicknameRight + 140);
        if (!(alignedRow || alignedName)) continue;
        if (!(rightOfName && nearName)) continue;
        if (rect.top < cardRect.top || rect.bottom > cardRect.bottom + 1) continue;

        nearIconCandidates.push(node);
      }

      for (const node of nearIconCandidates) {
        const tag = node.tagName.toLowerCase();
        const rect = node.getBoundingClientRect();
        const attrs = collectAttrs(node);
        const raw = attrs.join(' | ').toLowerCase();
        const st = window.getComputedStyle(node);
        const summary = `${tag}|w=${Math.round(rect.width)}|h=${Math.round(rect.height)}|${raw}|style_color=${st ? st.color : ''}|style_fill=${st ? st.fill : ''}|style_stroke=${st ? st.stroke : ''}`.trim();
        iconNodes.push(summary);
        signalSet.add(summary);
        nameRowSignalSet.add(summary);

        addComputedStyleSignals(node, nameRowSignalSet, iconStyleSet);
        if (node.parentElement) addComputedStyleSignals(node.parentElement, nameRowSignalSet, iconStyleSet);
        if (node.parentElement && node.parentElement.parentElement) {
          addComputedStyleSignals(node.parentElement.parentElement, nameRowSignalSet, iconStyleSet);
        }

        if (/(v|vip|verify|badge|认证|gold|orange|yellow|huang|cheng|jin|hong|red)/i.test(summary)) {
          signalSet.add(summary.toLowerCase());
          nameRowSignalSet.add(summary.toLowerCase());
        }
      }

      // 兜底：昵称文本节点的右侧兄弟节点里，很多站点会把认证图标挂在这里
      if (nicknameEl && nicknameEl.parentElement) {
        const siblings = Array.from(nicknameEl.parentElement.children || []);
        for (const sib of siblings) {
          if (!sib || sib === nicknameEl || !sib.getBoundingClientRect) continue;
          const r = sib.getBoundingClientRect();
          if (r.width <= 0 || r.height <= 0) continue;
          if (r.left < (nicknameRight - 6) || r.left > (nicknameRight + 160)) continue;
          const centerY = r.top + r.height / 2;
          if (centerY < (nameTop - 10) || centerY > (nameBottom + 10)) continue;
          const attrs = collectAttrs(sib);
          const summary = `${sib.tagName.toLowerCase()}|w=${Math.round(r.width)}|h=${Math.round(r.height)}|${attrs.join(' | ').toLowerCase()}`;
          iconNodes.push(summary);
          signalSet.add(summary);
          nameRowSignalSet.add(summary);
          addComputedStyleSignals(sib, nameRowSignalSet, iconStyleSet);
          siblingIconHit = true;
        }
      }

      // 关键兜底：很多站点把认证图标做成昵称元素的伪元素，而非真实节点
      collectPseudoSignals(nicknameEl, 'nickname');
      collectPseudoSignals(nameRow, 'name_row');
      if (nicknameEl && nicknameEl.parentElement) {
        collectPseudoSignals(nicknameEl.parentElement, 'name_parent');
      }

      out.has_verify_icon = nearIconCandidates.length > 0 || siblingIconHit || pseudoIconHit;
      out.signals = Array.from(signalSet).slice(0, 80);
      out.name_row_signals = Array.from(nameRowSignalSet).slice(0, 120);
      out.icon_style_signals = Array.from(iconStyleSet).slice(0, 80);
      out.icon_nodes = iconNodes.slice(0, 30);
      return out;
    }
    """
    return page.evaluate(js)


def _extract_inline_verify_clues(page):
    js = r"""
    () => {
      const out = [];
      const max = 300;
      const verifyRe = /(verify|verified|auth|vip|badge|renzheng|认证|v[_-]?type|gold|orange|yellow|jin|cheng|huang|hong|red|金v|橙v|黄v)/i;

      const push = (x) => {
        if (!x) return;
        if (out.length >= max) return;
        out.push(String(x).slice(0, 300));
      };

      const walk = (node, path, depth) => {
        if (depth > 7 || out.length >= max) return;
        if (Array.isArray(node)) {
          for (let i = 0; i < Math.min(node.length, 50); i++) walk(node[i], `${path}[${i}]`, depth + 1);
          return;
        }
        if (node && typeof node === 'object') {
          for (const k of Object.keys(node).slice(0, 80)) {
            const v = node[k];
            const kp = `${path}.${k}`;
            if (verifyRe.test(k)) push(`${kp}=${typeof v === 'object' ? '[obj]' : String(v)}`);
            walk(v, kp, depth + 1);
          }
          return;
        }
        if (typeof node === 'string' && verifyRe.test(node)) push(`${path}=${node}`);
      };

      const globals = ['__INITIAL_STATE__', '__NUXT__', '__NEXT_DATA__', '__APOLLO_STATE__', '__PINIA__'];
      for (const g of globals) {
        try {
          if (window[g]) walk(window[g], `window.${g}`, 0);
        } catch (e) {}
      }

      const scripts = Array.from(document.querySelectorAll('script[type="application/json"],script'));
      for (const s of scripts.slice(0, 60)) {
        const txt = (s.textContent || '').trim();
        if (!txt) continue;
        if (!verifyRe.test(txt)) continue;
        push(`script:${txt.slice(0, 240)}`);
      }
      return out;
    }
    """
    try:
        return page.evaluate(js)
    except Exception:
        return []


def extract_verify_level(page, api_verify_payloads):
    svg_probe = _extract_verify_level_from_exact_svg(page)
    fills = tuple(_normalize_hex_color(x) for x in svg_probe.get("path_fills", []))
    exact_level = VERIFY_FILL_MAP.get(fills)
    if exact_level:
        return exact_level, {
            "source": "dom_svg_exact",
            "fills": list(fills),
            "svg_class": svg_probe.get("svg_class", ""),
            "svg_html": svg_probe.get("svg_html", ""),
            "top_lines": svg_probe.get("top_lines", [])[:12],
        }

    if svg_probe.get("has_profile_card") and svg_probe.get("has_name_row") and not svg_probe.get("has_verify_icon"):
        if svg_probe.get("has_unverified_hint") or not svg_probe.get("has_verify_text"):
            return "无认证", {
                "source": "dom_svg_absent",
                "verify_text_value": svg_probe.get("verify_text_value", ""),
                "top_lines": svg_probe.get("top_lines", [])[:12],
            }

    if svg_probe.get("has_verify_icon"):
        return "unknown", {
            "source": "dom_svg_unknown",
            "fills": list(fills),
            "svg_class": svg_probe.get("svg_class", ""),
            "svg_html": svg_probe.get("svg_html", ""),
            "verify_text_value": svg_probe.get("verify_text_value", ""),
            "top_lines": svg_probe.get("top_lines", [])[:12],
        }

    api_level, api_clues = _resolve_verify_level_from_api_payloads(api_verify_payloads)
    if api_level:
        return api_level, {
            "source": "api",
            "clues": api_clues[:20],
        }

    return "unknown", {
        "source": "no_exact_signal",
        "verify_text_value": svg_probe.get("verify_text_value", ""),
        "top_lines": svg_probe.get("top_lines", [])[:12],
    }


def _start_verify_response_capture(page):
    api_payloads = []

    def _on_response(response):
        try:
            ctype = (response.headers or {}).get("content-type", "").lower()
            url_l = response.url.lower()
            if not any(t in url_l for t in ["weibo", "detail", "account", "user", "profile", "weiq", "api"]):
                return

            payload = None
            if "json" in ctype:
                try:
                    payload = response.json()
                except Exception:
                    payload = None
            if payload is None:
                try:
                    text_payload = response.text()
                    payload = _extract_json_from_text_payload(text_payload)
                except Exception:
                    payload = None
            if payload is None:
                return

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
        if verify_level in ("unknown", "无认证"):
            source = verify_debug.get("source", "unknown")
            preview = ""
            if verify_debug.get("icons"):
                preview = str(verify_debug["icons"][0])[:120]
            elif verify_debug.get("signals"):
                preview = str(verify_debug["signals"][0])[:120]
            elif verify_debug.get("svg_html"):
                preview = str(verify_debug["svg_html"])[:120]
            extra = ""
            if verify_debug.get("verify_text_value"):
                extra += f" verify_text={str(verify_debug.get('verify_text_value'))[:40]}"
            if verify_debug.get("fills"):
                extra += f" fills={verify_debug.get('fills')}"
            if verify_debug.get("region_debug"):
                extra += f" region={str(verify_debug.get('region_debug'))[:120]}"
            print(f"{progress} ⚠️ 认证等级={verify_level}（source={source}）{(' 线索=' + preview) if preview else ''}{extra}")
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
