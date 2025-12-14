import streamlit as st
import pandas as pd
from googleapiclient.discovery import build
import google.generativeai as genai
import time
import random
import json
import altair as alt
import streamlit.components.v1 as components

# =========================
# 1) Page Config
# =========================
st.set_page_config(
    page_title="Google SERP 戰略雷達 v3.0 (Enterprise)",
    page_icon="🎯",
    layout="wide"
)

st.title("🎯 Google SERP 戰略雷達 v3.0 (Final)")
st.markdown("""
### Private SEO Weapon: Battlefield Reader & Content Architect
不僅是分析意圖，更直接生成「可落地的內容策略」與「寫作大綱」。具備自動修復 JSON 與成本監控功能。
""")

# =========================
# 2) Sidebar Settings
# =========================
with st.sidebar:
    st.header("🔑 啟動金鑰")
    st.info("請確保已啟用 Google Custom Search API")
    GOOGLE_API_KEY = st.text_input("Google API Key", type="password")

    # [防呆] 自動移除 cx=
    raw_cx = st.text_input("Search Engine ID (CX)", type="password")
    SEARCH_ENGINE_ID = raw_cx.replace("cx=", "").strip() if raw_cx else ""

    GEMINI_API_KEY = st.text_input("Gemini API Key", type="password")

    st.divider()
    st.header("🧠 模型設定")
    MODEL_NAME = st.selectbox(
        "選擇主要分析模型",
        ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-pro-preview"],
        index=0,
        help="建議：Flash 跑大量測試；Pro/3.0 做最終策略。"
    )

    st.divider()
    st.header("🌍 戰場設定")
    TARGET_GL = st.text_input("地區 (gl)", value="tw", help="例如: tw, us, jp")
    TARGET_HL = st.text_input("語言 (hl)", value="zh-TW", help="例如: zh-TW, en")
    MAX_PAGES = st.slider("抓取頁數", 1, 3, 2, help="1頁=Top10, 2頁=Top20 (注意配額消耗)")

# =========================
# 2.1) CSE Preview (no quota)
# =========================
if SEARCH_ENGINE_ID:
    with st.expander("👀 手動搜尋驗證 (Google Programmable Search 預覽)"):
        st.caption("此區塊不消耗 API 配額，可直接預覽您的 Custom Search Engine 結果。")
        components.html(
            f"""
            <script async src="https://cse.google.com/cse.js?cx={SEARCH_ENGINE_ID}"></script>
            <div class="gcse-search"></div>
            """,
            height=600,
            scrolling=True
        )

# =========================
# 3) Helpers
# =========================
def detect_page_type(item):
    """
    更細緻的頁面類型判斷（規則法）。
    """
    link = (item.get('link') or "").lower()
    snippet = (item.get('snippet') or "").lower()
    title = (item.get('title') or "").lower()

    # 強特徵
    if any(x in link for x in ['forum', 'ptt.cc', 'dcard.tw', 'mobile01', 'reddit', 'baha']):
        return "🗣️ UGC/Forum (論壇)"
    if any(x in link for x in ['youtube.com', 'instagram.com', 'facebook.com', 'tiktok.com']):
        return "🎥 Social/Video (社群影音)"
    if any(x in link for x in ['/product/', 'shopee', 'momo', 'pchome', 'amazon', 'rakuten', 'buy123']):
        return "🛒 E-commerce (電商)"
    if any(x in link for x in ['/news/', 'news.', 'udn.com', 'ltn.com', 'chinatimes', 'ettoday']):
        return "📰 Media/News (新聞媒體)"
    if ".gov" in link:
        return "🏛️ Government (政府)"
    if "wiki" in link or "wikipedia" in link:
        return "📖 Wiki (百科)"

    # 弱特徵（標題/摘要）
    if any(x in title for x in ['價格', '優惠', '買', '折扣', 'price', 'shop']) or any(x in snippet for x in ['價格', '優惠', '折扣', '購買', '下單']):
        return "🛒 E-commerce (疑似電商)"
    if any(x in link for x in ['blog', 'article', 'post', 'topic']):
        return "📝 Blog/Article (內容頁)"

    return "📄 General (一般頁面)"


@st.cache_data(ttl=3600, show_spinner=False)
def get_google_serp_data_cached(api_key, cx, keyword, gl, hl, pages):
    """
    快取版 SERP 抓取器（回傳 list[dict] 或 {"error": "..."}）
    """
    try:
        service = build("customsearch", "v1", developerKey=api_key)
    except Exception as e:
        return {"error": f"Service Build Error: {e}"}

    all_results = []

    for page in range(pages):
        start_index = (page * 10) + 1
        retries = 3

        while retries > 0:
            try:
                res = service.cse().list(
                    q=keyword,
                    cx=cx,
                    num=10,
                    start=start_index,
                    gl=gl,
                    hl=hl
                ).execute()

                items = res.get("items", [])
                if not items:
                    break

                for i, item in enumerate(items):
                    pagemap = item.get("pagemap", {}) or {}
                    metatags_list = pagemap.get("metatags", [{}]) or [{}]
                    metatags = metatags_list[0] if isinstance(metatags_list, list) and metatags_list else {}
                    description = metatags.get("og:description") or item.get("snippet") or ""

                    # 截斷以控 token
                    if description and len(description) > 200:
                        description = description[:200] + "..."

                    all_results.append({
                        "Rank": start_index + i,
                        "Type": detect_page_type(item),
                        "Title": item.get("title"),
                        "Description": description,
                        "DisplayLink": item.get("displayLink"),
                        "Link": item.get("link")
                    })
                break

            except Exception as e:
                retries -= 1
                wait_time = (3 - retries) * 2 + random.uniform(0, 1)
                time.sleep(wait_time)
                if retries == 0:
                    return {"error": f"API Fetch Error (Page {page+1}): {str(e)}"}

        time.sleep(1.2)

    return all_results


def _strip_code_fences(text: str) -> str:
    if not text:
        return ""
    t = text.strip()

    # 常見情況：```json ... ```
    if t.startswith("```"):
        # 移除第一段 fence 行
        first_newline = t.find("\n")
        if first_newline != -1:
            t = t[first_newline + 1:]
        # 移除結尾 fence
        if t.strip().endswith("```"):
            t = t.strip()[:-3]
    return t.strip()


def repair_json_with_gemini(api_key, broken_text, error_msg):
    """
    JSON 修復：使用便宜 Flash 模型把壞掉的 JSON 修成可 parse 的 JSON 字串
    回傳 dict 或 None
    """
    genai.configure(api_key=api_key)
    repair_model = genai.GenerativeModel("gemini-2.5-flash")

    prompt = f"""
You are a JSON repair expert.
The following text was intended to be valid JSON but failed to parse.

Parse error:
{error_msg}

Broken text:
{broken_text}

Task:
- Return ONLY a valid JSON object string.
- Remove any markdown fences like ```json.
- Do not include any explanations.
"""

    try:
        response = repair_model.generate_content(prompt)
        cleaned = _strip_code_fences(getattr(response, "text", "") or "")
        if not cleaned:
            return None
        return json.loads(cleaned)
    except Exception:
        return None


def analyze_strategy_with_gemini(api_key, keyword, df, gl, model_name):
    """
    主策略分析：回傳 (result_dict, raw_text)
    - 先用主模型產 JSON
    - parse 失敗 => 呼叫 repair_json_with_gemini 修復
    """
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)

    # 輸入壓縮：只保留必要欄位（描述已在前面截斷）
    compact_df = df[["Rank", "Type", "Title", "Description", "DisplayLink"]].copy()

    # 再做一次保險截斷（避免有人改前面邏輯）
    compact_df["Title"] = compact_df["Title"].fillna("").astype(str).str.slice(0, 120)
    compact_df["Description"] = compact_df["Description"].fillna("").astype(str).str.slice(0, 220)
    compact_df["DisplayLink"] = compact_df["DisplayLink"].fillna("").astype(str).str.slice(0, 80)

    data_str = compact_df.to_string(index=False)

    prompt = f"""
你是專精於 SEO 的策略顧問與內容架構師。
我們正在分析關鍵字「{keyword}」在 Google 搜尋結果（gl={gl}）的前 {len(df)} 名分佈。

以下是 SERP 資料（Type 為規則判斷的頁面類型）：
{data_str}

請輸出「可落地」的策略，並嚴格以 JSON 物件格式回傳（不要 Markdown fence，不要解釋文字）：

{{
  "User_Intent": "一句話說明使用者最核心想完成的任務（可含 1-2 個次要意圖）",
  "Battlefield_Status": "戰場概況：誰在霸榜、頁型分佈、是否壟斷、UGC/媒體/電商的權重",
  "Opportunity_Gap": "目前前段結果的不足與可切入的缺口（要具體，不要空話）",
  "Recommended_Page_Type": "建議我們要做的頁型（例如：比較文/選購指南/FAQ/產品頁/落地頁/評測）",
  "Winning_Angles": [
    {{ "angle": "差異化切角1", "target_audience": "適用對象/情境" }},
    {{ "angle": "差異化切角2", "target_audience": "適用對象/情境" }},
    {{ "angle": "差異化切角3", "target_audience": "適用對象/情境" }}
  ],
  "Killer_Titles": [
    {{ "title": "必勝標題1", "reason": "為何能贏（對齊意圖/缺口/可點擊）" }},
    {{ "title": "必勝標題2", "reason": "為何能贏（對齊意圖/缺口/可點擊）" }},
    {{ "title": "必勝標題3", "reason": "為何能贏（對齊意圖/缺口/可點擊）" }}
  ],
  "Content_Outline": [
    "H1: ...",
    "H2: ...",
    "H2: ...",
    "H3: ...",
    "H2: ...",
    "FAQ: ..."
  ]
}}
"""

    response = None
    raw_text = ""
    try:
        response = model.generate_content(prompt)
        raw_text = getattr(response, "text", "") or ""
        cleaned = _strip_code_fences(raw_text)
        parsed = json.loads(cleaned)
        return parsed, raw_text

    except json.JSONDecodeError as e:
        # 觸發修復
        repaired = repair_json_with_gemini(api_key, raw_text, str(e))
        if repaired is not None:
            return repaired, raw_text
        return {"error": f"JSON 解析失敗且修復無效: {e}"}, raw_text

    except Exception as e:
        return {"error": f"API 錯誤: {e}"}, raw_text


def generate_markdown_report(report_data_list):
    """生成人類可讀 Markdown 報告（顧問交付用）"""
    md = f"# SEO 戰略分析報告\n\n生成時間: {time.strftime('%Y-%m-%d %H:%M')}\n\n"

    for item in report_data_list:
        kw = item.get("Keyword", "")
        md += f"## 關鍵字：{kw}\n\n"

        md += "### 1. 意圖與戰場\n"
        md += f"- **核心意圖**: {item.get('User_Intent', '')}\n"
        md += f"- **戰場現況**: {item.get('Battlefield_Status', '')}\n"
        md += f"- **機會缺口**: {item.get('Opportunity_Gap', '')}\n\n"

        md += "### 2. 內容策略\n"
        md += f"- **建議頁型**: {item.get('Recommended_Page_Type', '')}\n"
        md += "- **致勝切角**:\n"
        for angle in item.get("Winning_Angles", []):
            if isinstance(angle, dict):
                md += f"  - **{angle.get('angle', '')}**：{angle.get('target_audience', '')}\n"
            else:
                md += f"  - {str(angle)}\n"

        md += "\n### 3. 必勝標題\n"
        for t in item.get("Killer_Titles", []):
            if isinstance(t, dict):
                md += f"- {t.get('title', '')} (*{t.get('reason', '')}*)\n"
            else:
                md += f"- {str(t)}\n"

        md += "\n### 4. 內容大綱 (Outline)\n"
        outline = item.get("Content_Outline", [])
        if isinstance(outline, list):
            for line in outline:
                md += f"- {line}\n"
        else:
            md += f"- {str(outline)}\n"

        md += "\n---\n\n"

    return md

# =========================
# 4) Main UI
# =========================
keywords_input = st.text_area(
    "輸入關鍵字 (自動去重)",
    height=100,
    placeholder="空氣清淨機 推薦\nCRM 系統比較"
)

col_act1, col_act2 = st.columns([1, 3])
with col_act1:
    start_btn = st.button("🚀 啟動戰略雷達", type="primary")

if start_btn:
    # Key 檢查
    if not (GOOGLE_API_KEY and SEARCH_ENGINE_ID and GEMINI_API_KEY):
        st.error("⚠️ 請先在左側欄位輸入所有 API Key")
        st.stop()

    if not keywords_input.strip():
        st.warning("⚠️ 請輸入至少一個關鍵字")
        st.stop()

    # 去重且保留順序
    raw_keywords = [k.strip() for k in keywords_input.split("\n") if k.strip()]
    keywords = list(dict.fromkeys(raw_keywords))

    main_progress = st.progress(0)

    # 成本/配額估算（粗略：SERP calls）
    est_serp_calls = len(keywords) * MAX_PAGES
    st.caption(f"📊 預計執行：{len(keywords)} 個關鍵字 | SERP 查詢消耗：約 {est_serp_calls} 次 (Quota)")

    report_data_list = []

    for idx, kw in enumerate(keywords):
        st.subheader(f"🔍 目標：{kw}")

        # 1) SERP (cached)
        with st.spinner(f"正在掃描戰場 (Top {MAX_PAGES*10})..."):
            raw_data = get_google_serp_data_cached(
                GOOGLE_API_KEY, SEARCH_ENGINE_ID, kw, TARGET_GL, TARGET_HL, MAX_PAGES
            )

        if isinstance(raw_data, dict) and "error" in raw_data:
            st.error(f"❌ {raw_data['error']}")
        elif raw_data:
            df = pd.DataFrame(raw_data)

            # 1.1) Battlefield Viz
            with st.expander("📊 戰場分佈視覺化 (點擊展開)", expanded=True):
                col_viz1, col_viz2 = st.columns([2, 1])

                with col_viz1:
                    st.dataframe(
                        df[["Rank", "Type", "Title", "DisplayLink"]],
                        use_container_width=True,
                        height=220
                    )

                with col_viz2:
                    type_counts = df["Type"].value_counts().reset_index()
                    type_counts.columns = ["Type", "Count"]
                    chart = alt.Chart(type_counts).mark_arc(innerRadius=50).encode(
                        theta=alt.Theta(field="Count", type="quantitative"),
                        color=alt.Color(field="Type", type="nominal"),
                        tooltip=["Type", "Count"]
                    ).properties(title=f"Top {MAX_PAGES*10} 類型佔比")
                    st.altair_chart(chart, use_container_width=True)

            # 2) AI Strategy
            with st.spinner(f"🧠 {MODEL_NAME} 正在建構策略..."):
                analysis_result, raw_text = analyze_strategy_with_gemini(
                    GEMINI_API_KEY, kw, df, TARGET_GL, MODEL_NAME
                )

            if "error" in analysis_result:
                st.error(f"❌ 分析失敗: {analysis_result['error']}")
                with st.expander("查看原始模型回應 (Debug)"):
                    st.text(raw_text)
            else:
                st.markdown("#### 📝 戰略分析報告")

                c1, c2, c3 = st.columns(3)
                with c1:
                    st.info(f"**核心意圖**\n\n{analysis_result.get('User_Intent', 'N/A')}")
                with c2:
                    st.warning(f"**建議頁型**\n\n{analysis_result.get('Recommended_Page_Type', 'N/A')}")
                with c3:
                    st.success(f"**機會缺口**\n\n{analysis_result.get('Opportunity_Gap', 'N/A')}")

                t1, t2 = st.tabs(["💡 切角與標題", "🧱 內容大綱 (Outline)"])

                with t1:
                    st.markdown("**致勝切角：**")
                    for a in analysis_result.get("Winning_Angles", []):
                        if isinstance(a, dict):
                            st.markdown(f"- **{a.get('angle', '')}**：{a.get('target_audience', '')}")
                        else:
                            st.markdown(f"- {str(a)}")

                    st.markdown("---")
                    st.markdown("**必勝標題：**")
                    for t in analysis_result.get("Killer_Titles", []):
                        if isinstance(t, dict):
                            st.markdown(f"- {t.get('title', '')} (*{t.get('reason', '')}*)")
                        else:
                            st.markdown(f"- {str(t)}")

                with t2:
                    st.markdown("##### 建議文章結構")
                    outline = analysis_result.get("Content_Outline", [])
                    if isinstance(outline, list):
                        st.text("\n".join([str(x) for x in outline]))
                    else:
                        st.text(str(outline))

                analysis_result["Keyword"] = kw
                report_data_list.append(analysis_result)

        else:
            st.error(f"❌ 無法抓取 {kw} 的資料 (Unknown Error)")

        st.divider()
        main_progress.progress((idx + 1) / len(keywords))

    st.success("✅ 全部分析完成！")

    # 3) Downloads
    if report_data_list:
        st.header("📥 下載戰略報告")

        md_report = generate_markdown_report(report_data_list)
        json_report = json.dumps(report_data_list, ensure_ascii=False, indent=2)

        # 扁平化 CSV
        csv_data_list = []
        for item in report_data_list:
            flat_item = dict(item)
            flat_item["Winning_Angles"] = json.dumps(item.get("Winning_Angles", []), ensure_ascii=False)
            flat_item["Killer_Titles"] = json.dumps(item.get("Killer_Titles", []), ensure_ascii=False)
            outline = item.get("Content_Outline", [])
            flat_item["Content_Outline"] = "\n".join(outline) if isinstance(outline, list) else str(outline)
            csv_data_list.append(flat_item)
        df_csv = pd.DataFrame(csv_data_list)
        csv_report = df_csv.to_csv(index=False).encode("utf-8-sig")

        d1, d2, d3 = st.columns(3)
        with d1:
            st.download_button("📄 下載 Markdown 報告", md_report, f"seo_report_{int(time.time())}.md", "text/markdown")
        with d2:
            st.download_button("📊 下載 Excel 友善 CSV", csv_report, f"seo_data_{int(time.time())}.csv", "text/csv")
        with d3:
            st.download_button("📋 下載 JSON", json_report, f"seo_raw_{int(time.time())}.json", "application/json")
