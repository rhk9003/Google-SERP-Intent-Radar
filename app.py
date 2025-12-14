import streamlit as st
import pandas as pd
from googleapiclient.discovery import build
import google.generativeai as genai
import time
import random
import json
import altair as alt

# --- 1. 頁面基礎設定 ---
st.set_page_config(
    page_title="Google SERP 戰略雷達 v3.0 (Enterprise)",
    page_icon="🎯",
    layout="wide"
)

st.title("🎯 Google SERP 戰略雷達 v3.0")
st.markdown("""
### Private SEO Weapon: Battlefield Reader & Content Architect
不僅是分析意圖，更直接生成「可落地的內容策略」與「寫作大綱」。具備自動修復 JSON 與成本監控功能。
""")

# --- 2. 側邊欄：設定與金鑰 ---
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
        help="建議：用 Flash 跑大量測試，用 3.0 Pro 產出最終高智商策略。"
    )

    st.divider()
    st.header("🌍 戰場設定")
    TARGET_GL = st.text_input("地區 (gl)", value="tw", help="例如: tw, us, jp")
    TARGET_HL = st.text_input("語言 (hl)", value="zh-TW", help="例如: zh-TW, en")
    MAX_PAGES = st.slider("抓取頁數", 1, 3, 2, help="1頁=Top10, 2頁=Top20 (注意配額消耗)")

# --- 3. 核心工具函式庫 ---

def detect_page_type(item):
    """
    [升級] 更細緻的頁面類型判斷邏輯
    區分：電商、媒體、論壇、官網、政府/維基、部落格
    """
    link = item.get('link', '').lower()
    snippet = item.get('snippet', '').lower()
    title = item.get('title', '').lower()
    
    # 強特徵判斷
    if any(x in link for x in ['forum', 'ptt.cc', 'dcard.tw', 'mobile01', 'reddit', 'baha']):
        return "🗣️ UGC/Forum (論壇)"
    if any(x in link for x in ['youtube.com', 'instagram.com', 'facebook.com', 'tiktok.com']):
        return "🎥 Social/Video (社群影音)"
    if any(x in link for x in ['/product/', 'shopee', 'momo', 'pchome', 'amazon', 'rakuten', 'buy123']):
        return "🛒 E-commerce (電商)"
    if any(x in link for x in ['/news/', 'news.', 'udn.com', 'ltn.com', 'chinatimes', 'ettoday']):
        return "📰 Media/News (新聞媒體)"
    if '.gov' in link:
        return "🏛️ Government (政府)"
    if 'wiki' in link or 'wikipedia' in link:
        return "📖 Wiki (百科)"
    
    # 弱特徵判斷 (依據標題或 snippet)
    if any(x in title for x in ['價格', '優惠', '買', '折扣', 'price', 'shop']):
        return "🛒 E-commerce (疑似電商)"
    if any(x in link for x in ['blog', 'article', 'post', 'topic']):
        return "📝 Blog/Article (內容頁)"
        
    return "📄 General (一般頁面)"

# [升級] 加上快取機制，避免重複扣 Quota
@st.cache_data(ttl=3600, show_spinner=False)
def get_google_serp_data_cached(api_key, cx, keyword, gl, hl, pages):
    """
    快取版的 SERP 抓取器。
    只要參數 (keyword, gl, hl, pages) 相同，一小時內不會重複 call Google API。
    """
    # 建立 service 物件 (無法 pickle，所以不快取 service 本身，只快取結果)
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
                
                items = res.get('items', [])
                if not items:
                    break 
                
                for i, item in enumerate(items):
                    # 嘗試抓取 og:description
                    pagemap = item.get('pagemap', {})
                    metatags = pagemap.get('metatags', [{}])[0]
                    description = metatags.get('og:description', item.get('snippet'))
                    
                    # [優化] 截斷過長的描述以節省 Token
                    if description and len(description) > 200:
                        description = description[:200] + "..."

                    all_results.append({
                        "Rank": start_index + i,
                        "Type": detect_page_type(item),
                        "Title": item.get('title'),
                        "Description": description,
                        "DisplayLink": item.get('displayLink'),
                        "Link": item.get('link')
                    })
                break
            except Exception as e:
                retries -= 1
                wait_time = (3 - retries) * 2 + random.uniform(0, 1)
                time.sleep(wait_time)
                if retries == 0:
                    return {"error": f"API Fetch Error (Page {page+1}): {str(e)}"}
        
        time.sleep(1.5) # 稍微休息
        
    return all_results

def repair_json_with_gemini(api_key, broken_text, error_msg):
    """
    [新增] JSON 外科手術修復師
    當主要模型吐出爛掉的 JSON 時，呼叫便宜的 Flash 模型來修復它。
    """
    genai.configure(api_key=api_key)
    # 使用 Flash 修復，速度快且便宜
    repair_model = genai.GenerativeModel('gemini-2.5-flash')
    
    prompt = f"""
    You are a JSON repair expert. The following text was intended to be a valid JSON but failed to parse.
    Error: {error_msg}
    
    Broken Text:
    {broken_text}
    
    Please fix the JSON structure, remove any markdown formatting (like ```json), and return ONLY the valid JSON string.
    Do not add any explanations.
    """
    try:
        response = repair_model.generate_content(prompt)
        cleaned = response.text.strip()
        if cleaned.startswith("
