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
# 0) 固定設定
# =========================
SEARCH_ENGINE_ID = "23e43fb5e029f4b50"  # 寫死 CX（非機密）

# =========================
# 1) Page Config
# =========================
st.set_page_config(
    page_title="Google SERP 戰略雷達 v3.1 (Strategy Only)",
    page_icon="🎯",
    layout="wide"
)

st.title("🎯 Google SERP 戰略雷達 v3.1")
st.markdown("""
### Private SEO Weapon: Battlefield Strategy Reader  
**只做一件事：判讀戰場 → 輸出可執行的 SEO 策略**
""")

# =========================
# 2) Sidebar
# =========================
with st.sidebar:
    st.header("🔑 API 設定")
    GOOGLE_API_KEY = st.text_input("Google API Key", type="password")
    GEMINI_API_KEY = st.text_input("Gemini API Key", type="password")

    st.divider()
    st.header("🧠 模型")
    MODEL_NAME = st.selectbox(
        "分析模型",
        ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-pro-preview"],
        index=0
    )

    st.divider()
    st.header("🌍 搜尋設定")
    TARGET_GL = st.text_input("地區 (gl)", value="tw")
    TARGET_HL = st.text_input("語言 (hl)", value="zh-TW")
    MAX_PAGES = st.slider("抓取頁數", 1, 3, 2)

# =========================
# 2.1) CSE 預覽（不耗 Quota）
# =========================
with st.expander("👀 Google CSE 搜尋預覽（不耗 API）"):
    components.html(
        f"""
        <script async src="https://cse.google.com/cse.js?cx={SEARCH_ENGINE_ID}"></script>
        <div class="gcse-search"></div>
        """,
        height=600,
        scrolling=True
    )

# =========================
# 3) Helper Functions
# =========================
def detect_page_type(item):
    link = (item.get("link") or "").lower()
    title = (item.get("title") or "").lower()

    if any(x in link for x in ["ptt.cc", "dcard", "reddit", "mobile01"]):
        return "UGC / Forum"
    if any(x in link for x in ["youtube.com", "instagram.com", "tiktok.com"]):
        return "Social / Video"
    if any(x in link for x in ["shopee", "momo", "pchome", "amazon", "/product/"]):
        return "E-commerce"
    if any(x in link for x in ["udn.com", "ltn.com", "ettoday", "/news/"]):
        return "Media"
    if "wiki" in link:
        return "Wiki"
    if any(x in title for x in ["價格", "優惠", "推薦"]):
        return "Commercial Content"
    return "General"

@st.cache_data(ttl=3600, show_spinner=False)
def get_serp(api_key, keyword, gl, hl, pages):
    service = build("customsearch", "v1", developerKey=api_key)
    results = []

    for page in range(pages):
        start = page * 10 + 1
        res = service.cse().list(
            q=keyword,
            cx=SEARCH_ENGINE_ID,
            num=10,
            start=start,
            gl=gl,
            hl=hl
        ).execute()

        for i, item in enumerate(res.get("items", [])):
            desc = item.get("snippet", "")
            if len(desc) > 200:
                desc = desc[:200] + "..."

            results.append({
                "Rank": start + i,
                "Type": detect_page_type(item),
                "Title": item.get("title"),
                "Description": desc,
                "DisplayLink": item.get("displayLink")
            })
        time.sleep(1.2)
    return results

def repair_json(api_key, broken_text, error):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")

    prompt = f"""
Fix the JSON below. Return ONLY valid JSON.

Error:
{error}

Broken JSON:
{broken_text}
"""
    try:
        res = model.generate_content(prompt)
        return json.loads(res.text.strip().strip("```json").strip("```"))
    except Exception:
        return None

def analyze_strategy(api_key, keyword, df, gl, model_name):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)

    data = df[["Rank", "Type", "Title", "Description", "DisplayLink"]].to_string(index=False)

    prompt = f"""
你是 SEO 策略顧問。
請分析關鍵字「{keyword}」在 Google（{gl}）的 SERP 戰場。

資料：
{data}

請只用 JSON 回傳：
{{
  "User_Intent": "...",
  "Battlefield_Status": "...",
  "Opportunity_Gap": "...",
  "Recommended_Page_Type": "...",
  "Winning_Angles": [
    {{ "angle": "...", "target": "..." }}
  ],
  "Killer_Titles": [
    {{ "title": "...", "reason": "..." }}
  ]
}}
"""

    try:
        res = model.generate_content(prompt)
        raw = res.text
        return json.loads(raw), raw
    except json.JSONDecodeError as e:
        fixed = repair_json(api_key, raw, e)
        return fixed if fixed else {"error": str(e)}, raw

# =========================
# 4) Main
# =========================
keywords_input = st.text_area(
    "輸入關鍵字（自動去重）",
    height=100,
    placeholder="空氣清淨機 推薦\nCRM 系統比較"
)

if st.button("🚀 啟動戰略分析", type="primary"):
    if not (GOOGLE_API_KEY and GEMINI_API_KEY):
        st.error("請輸入 Google API Key 與 Gemini API Key")
        st.stop()

    keywords = list(dict.fromkeys([k.strip() for k in keywords_input.split("\n") if k.strip()]))
    progress = st.progress(0)
    reports = []

    for i, kw in enumerate(keywords):
        st.subheader(f"🔍 {kw}")

        serp = get_serp(GOOGLE_API_KEY, kw, TARGET_GL, TARGET_HL, MAX_PAGES)
        df = pd.DataFrame(serp)

        with st.expander("📊 戰場分布", expanded=True):
            col1, col2 = st.columns([2, 1])
            with col1:
                st.dataframe(df, use_container_width=True, height=220)
            with col2:
                chart = alt.Chart(
                    df["Type"].value_counts().reset_index(name="Count").rename(columns={"index": "Type"})
                ).mark_arc(innerRadius=50).encode(
                    theta="Count",
                    color="Type",
                    tooltip=["Type", "Count"]
                )
                st.altair_chart(chart, use_container_width=True)

        result, raw = analyze_strategy(GEMINI_API_KEY, kw, df, TARGET_GL, MODEL_NAME)

        if "error" in result:
            st.error("策略解析失敗")
            st.text(raw)
        else:
            st.markdown("### 🧠 策略結論")
            st.info(result["User_Intent"])
            st.warning(result["Battlefield_Status"])
            st.success(result["Opportunity_Gap"])

            st.markdown("**建議頁型**")
            st.write(result["Recommended_Page_Type"])

            st.markdown("**致勝切角**")
            for a in result["Winning_Angles"]:
                st.markdown(f"- **{a['angle']}**（{a['target']}）")

            st.markdown("**必勝標題**")
            for t in result["Killer_Titles"]:
                st.markdown(f"- {t['title']}（{t['reason']}）")

            result["Keyword"] = kw
            reports.append(result)

        progress.progress((i + 1) / len(keywords))
        st.divider()

    st.success("✅ 全部策略分析完成")

    if reports:
        st.header("📥 下載")
        st.download_button(
            "下載 JSON",
            json.dumps(reports, ensure_ascii=False, indent=2),
            "seo_strategy.json",
            "application/json"
        )
