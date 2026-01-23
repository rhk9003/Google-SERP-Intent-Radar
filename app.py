import streamlit as st
import pandas as pd
from googleapiclient.discovery import build
import google.generativeai as genai
import time
import random
import json
import altair as alt
import streamlit.components.v1 as components
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from collections import OrderedDict
import requests
from bs4 import BeautifulSoup
import html2text

# =================================================
# 0. 固定設定
# =================================================
SEARCH_ENGINE_ID = "23e43fb5e029f4b50"  # CX 寫死（非機密）

# =================================================
# 1. Page Config
# =================================================
st.set_page_config(
    page_title="Google SERP 戰略雷達 v4.0",
    page_icon="🎯",
    layout="wide"
)

st.title("🎯 Google SERP 戰略雷達 v4.0")
st.markdown("""
### Private SEO Weapon: Two-Phase Strategic Analysis  
**第一階段：關鍵字探索** → **第二階段：SERP 戰略分析 + 內容方向指引**
""")

# =================================================
# 2. Sidebar
# =================================================
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

    st.divider()
    st.header("⚡ 效能設定")
    MAX_CONCURRENT_SERP = st.slider(
        "SERP 同時請求數", 
        min_value=1, 
        max_value=5, 
        value=3,
        help="Google CSE API 的並發上限"
    )
    MAX_CONCURRENT_GEMINI = st.slider(
        "Gemini 同時請求數", 
        min_value=1, 
        max_value=3, 
        value=2,
        help="建議保守設定，避免撞 RPM 限制"
    )
    GEMINI_MIN_INTERVAL = st.slider(
        "Gemini 請求間隔（秒）",
        min_value=0.5,
        max_value=3.0,
        value=1.0,
        step=0.5,
        help="每次 Gemini 呼叫的最小間隔"
    )

# =================================================
# 3. Rate Limited Executor（核心平行控制）
# =================================================
class RateLimitedExecutor:
    """帶 rate limit 的平行執行器，防止 API 過載"""
    
    def __init__(self, max_concurrent_serp=3, max_concurrent_gemini=2, gemini_min_interval=1.0):
        self.serp_semaphore = threading.Semaphore(max_concurrent_serp)
        self.gemini_semaphore = threading.Semaphore(max_concurrent_gemini)
        self.gemini_last_call = 0
        self.gemini_min_interval = gemini_min_interval
        self.lock = threading.Lock()
        
        # 統計用
        self.stats = {
            "serp_calls": 0,
            "gemini_calls": 0,
            "gemini_retries": 0,
            "errors": []
        }
    
    def call_serp(self, func, *args, **kwargs):
        """執行 SERP API 呼叫，帶並發控制"""
        with self.serp_semaphore:
            try:
                result = func(*args, **kwargs)
                with self.lock:
                    self.stats["serp_calls"] += 1
                time.sleep(0.5)  # 基本間隔避免過快
                return result
            except Exception as e:
                with self.lock:
                    self.stats["errors"].append(f"SERP: {str(e)}")
                raise
    
    def call_gemini(self, func, *args, **kwargs):
        """執行 Gemini API 呼叫，帶並發控制 + 速率限制 + 重試"""
        with self.gemini_semaphore:
            # 確保最小間隔
            with self.lock:
                elapsed = time.time() - self.gemini_last_call
                if elapsed < self.gemini_min_interval:
                    time.sleep(self.gemini_min_interval - elapsed)
                self.gemini_last_call = time.time()
            
            # Exponential backoff retry
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    result = func(*args, **kwargs)
                    with self.lock:
                        self.stats["gemini_calls"] += 1
                    return result
                except Exception as e:
                    error_str = str(e).lower()
                    is_rate_limit = any(x in error_str for x in ["429", "quota", "rate", "limit"])
                    
                    if is_rate_limit and attempt < max_retries - 1:
                        wait_time = (2 ** attempt) + random.uniform(0.5, 1.5)
                        with self.lock:
                            self.stats["gemini_retries"] += 1
                        time.sleep(wait_time)
                    else:
                        with self.lock:
                            self.stats["errors"].append(f"Gemini: {str(e)}")
                        raise
            
            # 最後一次嘗試
            return func(*args, **kwargs)


# =================================================
# 4. Phase 1: 關鍵字探索 Helper Functions
# =================================================
def fetch_webpage_content(url):
    """抓取網頁內容並轉換為純文字"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or 'utf-8'
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 移除不需要的元素
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe', 'noscript']):
            tag.decompose()
        
        # 取得主要內容
        main_content = soup.find('main') or soup.find('article') or soup.find('body')
        
        if main_content:
            # 轉換為純文字
            h = html2text.HTML2Text()
            h.ignore_links = True
            h.ignore_images = True
            h.ignore_emphasis = False
            text = h.handle(str(main_content))
            
            # 清理多餘空白
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            cleaned_text = '\n'.join(lines)
            
            # 限制長度避免 token 過多
            if len(cleaned_text) > 15000:
                cleaned_text = cleaned_text[:15000] + "..."
            
            return cleaned_text, None
        else:
            return None, "無法找到主要內容區塊"
            
    except requests.exceptions.RequestException as e:
        return None, f"網頁抓取失敗：{str(e)}"
    except Exception as e:
        return None, f"內容解析錯誤：{str(e)}"


def extract_keywords_from_content(api_key, content, product_name, model_name):
    """AI 分析頁面內容，萃取 30 組關鍵字"""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)
    
    prompt = f"""
你是一位專業的 SEO 關鍵字研究專家。

請分析以下網頁內容，針對產品/服務「{product_name}」萃取 30 組具有 SEO 價值的關鍵字。

網頁內容：
---
{content}
---

請將關鍵字分為三類：
1. **痛點字（Pain Point Keywords）**：使用者可能遇到的問題、困擾、需求（例如：「失眠怎麼辦」「肩頸痠痛」）
2. **產品字（Product Keywords）**：與產品/服務直接相關的搜尋詞（例如：「保健食品推薦」「按摩椅功能」）
3. **品牌字（Brand Keywords）**：品牌名稱、競品名稱、商品型號（例如：「XXX品牌評價」「YYY vs ZZZ」）

每類至少 8 組，總共 30 組關鍵字。

請只用 JSON 回傳，不要任何 markdown 格式、不要 ```json```、不要任何前後說明文字：
{{
  "pain_point_keywords": [
    {{"keyword": "關鍵字1", "search_intent": "搜尋意圖說明"}},
    {{"keyword": "關鍵字2", "search_intent": "搜尋意圖說明"}}
  ],
  "product_keywords": [
    {{"keyword": "關鍵字1", "search_intent": "搜尋意圖說明"}},
    {{"keyword": "關鍵字2", "search_intent": "搜尋意圖說明"}}
  ],
  "brand_keywords": [
    {{"keyword": "關鍵字1", "search_intent": "搜尋意圖說明"}},
    {{"keyword": "關鍵字2", "search_intent": "搜尋意圖說明"}}
  ]
}}
"""
    
    try:
        res = model.generate_content(prompt)
        raw = res.text.strip()
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned), None
    except json.JSONDecodeError as e:
        # 嘗試修復
        fixed = repair_json(api_key, raw, str(e))
        if fixed:
            return fixed, None
        return None, f"JSON 解析失敗：{str(e)}"
    except Exception as e:
        return None, f"AI 分析失敗：{str(e)}"


def get_related_keywords_from_serp(api_key, keyword, gl, hl):
    """使用 Google CSE 查詢關鍵字，從結果中提取相關詞彙"""
    try:
        service = build("customsearch", "v1", developerKey=api_key)
        res = service.cse().list(
            q=keyword,
            cx=SEARCH_ENGINE_ID,
            num=10,
            gl=gl,
            hl=hl
        ).execute()
        
        related = []
        
        # 從標題和描述中提取相關詞
        items = res.get("items", [])
        for item in items[:5]:  # 只取前 5 個結果
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            # 簡單合併作為參考
            related.append(f"{title[:50]}")
        
        # 檢查是否有 spelling 建議
        if "spelling" in res:
            related.insert(0, res["spelling"].get("correctedQuery", ""))
        
        return related[:5], None
        
    except Exception as e:
        return [], str(e)


# =================================================
# 5. Phase 2: SERP 分析 Helper Functions
# =================================================
def detect_page_type(item):
    """判斷 SERP 結果的頁面類型"""
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


def get_serp_raw(api_key, keyword, gl, hl, pages):
    """抓取 SERP 資料"""
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
            desc = item.get("snippet", "") or ""
            if len(desc) > 200:
                desc = desc[:200] + "..."

            results.append({
                "Rank": start + i,
                "Type": detect_page_type(item),
                "Title": item.get("title"),
                "Description": desc,
                "DisplayLink": item.get("displayLink"),
                "URL": item.get("link")
            })

        if page < pages - 1:
            time.sleep(0.8)

    return results


def repair_json(api_key, broken_text, error):
    """嘗試修復 Gemini 回傳的壞 JSON"""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    prompt = f"""
Fix the JSON below and return ONLY valid JSON. No markdown, no explanation.

Error: {error}

Broken JSON:
{broken_text}
"""
    try:
        res = model.generate_content(prompt)
        text = res.text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception:
        return None


def analyze_strategy_raw(api_key, keyword, df, gl, model_name):
    """執行 Gemini 策略分析"""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)

    data = df[["Rank", "Type", "Title", "Description", "DisplayLink"]].to_string(index=False)

    prompt = f"""
你是 SEO 策略顧問。
請分析關鍵字「{keyword}」在 Google（{gl}）的 SERP 戰場。

資料：
{data}

請只用 JSON 回傳，不要任何 markdown 格式、不要 ```json```、不要任何前後說明文字：
{{
  "User_Intent": "描述使用者搜尋此關鍵字的意圖",
  "Battlefield_Status": "目前 SERP 戰場的競爭狀態分析",
  "Opportunity_Gap": "發現的機會缺口",
  "Recommended_Page_Type": "建議製作的頁面類型",
  "Winning_Angles": [
    {{ "angle": "切角1", "target": "目標受眾" }},
    {{ "angle": "切角2", "target": "目標受眾" }}
  ],
  "Killer_Titles": [
    {{ "title": "標題1", "reason": "為何有效" }},
    {{ "title": "標題2", "reason": "為何有效" }}
  ]
}}
"""

    try:
        res = model.generate_content(prompt)
        raw = res.text.strip()
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned), raw
    except json.JSONDecodeError as e:
        fixed = repair_json(api_key, raw, str(e))
        if fixed:
            return fixed, raw
        return {"error": str(e), "raw_response": raw}, raw
    except Exception as e:
        return {"error": str(e)}, str(e)


def generate_content_direction(api_key, all_strategies, selected_keywords, model_name):
    """根據所有關鍵字的 SERP 分析，產生內容寫作綜合指引"""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)
    
    # 整理所有策略資訊
    strategy_summary = []
    for s in all_strategies:
        if "error" not in s:
            strategy_summary.append({
                "keyword": s.get("Keyword", ""),
                "intent": s.get("User_Intent", ""),
                "opportunity": s.get("Opportunity_Gap", ""),
                "page_type": s.get("Recommended_Page_Type", "")
            })
    
    prompt = f"""
你是一位資深內容策略顧問。

根據以下關鍵字的 SERP 戰場分析結果，請產生一份「內容寫作方向綜合指引」。

分析的關鍵字：
{json.dumps(selected_keywords, ensure_ascii=False)}

各關鍵字的 SERP 分析摘要：
{json.dumps(strategy_summary, ensure_ascii=False, indent=2)}

請提供具體、可執行的內容策略建議。

請只用 JSON 回傳，不要任何 markdown 格式、不要 ```json```、不要任何前後說明文字：
{{
  "content_theme": "核心主題方向（一句話描述這篇內容的核心定位）",
  "target_audience": "目標受眾描述（他們是誰？在什麼情境下會搜尋？）",
  "content_structure": [
    {{"section": "建議段落標題1", "focus": "這段要涵蓋的重點內容", "keywords_to_use": ["建議使用的關鍵字"]}},
    {{"section": "建議段落標題2", "focus": "這段要涵蓋的重點內容", "keywords_to_use": ["建議使用的關鍵字"]}},
    {{"section": "建議段落標題3", "focus": "這段要涵蓋的重點內容", "keywords_to_use": ["建議使用的關鍵字"]}}
  ],
  "must_cover_topics": ["必須涵蓋的主題1", "必須涵蓋的主題2", "必須涵蓋的主題3"],
  "differentiation_angle": "差異化切角（如何讓這篇內容與現有 SERP 結果不同）",
  "content_format_suggestion": "建議的內容格式（例如：比較表、步驟教學、案例分析等）",
  "avoid_pitfalls": ["需避免的寫作陷阱1", "需避免的寫作陷阱2"]
}}
"""
    
    try:
        res = model.generate_content(prompt)
        raw = res.text.strip()
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned), None
    except json.JSONDecodeError as e:
        fixed = repair_json(api_key, raw, str(e))
        if fixed:
            return fixed, None
        return None, f"JSON 解析失敗：{str(e)}"
    except Exception as e:
        return None, f"內容指引產生失敗：{str(e)}"


def process_single_keyword(kw, executor, google_key, gemini_key, gl, hl, pages, model_name):
    """處理單一關鍵字的完整流程（SERP + 分析）"""
    result = {
        "keyword": kw,
        "serp_df": None,
        "serp_raw": None,
        "strategy": None,
        "raw_response": None,
        "error": None,
        "timing": {}
    }
    
    try:
        # Step 1: SERP 抓取
        start_serp = time.time()
        serp_data = executor.call_serp(
            get_serp_raw, google_key, kw, gl, hl, pages
        )
        result["timing"]["serp"] = time.time() - start_serp
        result["serp_raw"] = serp_data
        result["serp_df"] = pd.DataFrame(serp_data)
        
        # Step 2: Gemini 分析
        start_gemini = time.time()
        strategy, raw = executor.call_gemini(
            analyze_strategy_raw, gemini_key, kw, result["serp_df"], gl, model_name
        )
        result["timing"]["gemini"] = time.time() - start_gemini
        result["strategy"] = strategy
        result["raw_response"] = raw
        
    except Exception as e:
        result["error"] = str(e)
    
    return result


# =================================================
# 6. Session State 初始化
# =================================================
if "phase1_keywords" not in st.session_state:
    st.session_state.phase1_keywords = None
if "selected_keywords" not in st.session_state:
    st.session_state.selected_keywords = []
if "phase1_completed" not in st.session_state:
    st.session_state.phase1_completed = False


# =================================================
# 7. Main App - 兩階段分頁
# =================================================
tab1, tab2 = st.tabs(["🔍 第一階段：關鍵字探索", "📊 第二階段：SERP 戰略分析"])

# =================================================
# 7.1 第一階段：關鍵字探索
# =================================================
with tab1:
    st.markdown("""
    ### 🔍 關鍵字探索
    輸入產品頁面網址，AI 將自動萃取高價值關鍵字並擴展相關詞彙。
    """)
    
    col1, col2 = st.columns([2, 1])
    with col1:
        input_url = st.text_input(
            "網頁網址",
            placeholder="https://example.com/product-page",
            help="輸入產品或服務頁面的網址"
        )
    with col2:
        product_name = st.text_input(
            "產品/服務名稱",
            placeholder="例：益生菌保健食品",
            help="用於引導 AI 萃取更精準的關鍵字"
        )
    
    # 備用方案：直接貼上內容
    with st.expander("📝 備用方案：直接貼上網頁內容"):
        manual_content = st.text_area(
            "貼上網頁內容（若網址無法抓取時使用）",
            height=200,
            placeholder="將網頁的主要文字內容貼在這裡..."
        )
    
    if st.button("🚀 開始關鍵字探索", type="primary", key="phase1_btn"):
        if not (GOOGLE_API_KEY and GEMINI_API_KEY):
            st.error("請先在側邊欄輸入 Google API Key 與 Gemini API Key")
            st.stop()
        
        if not product_name:
            st.warning("請輸入產品/服務名稱")
            st.stop()
        
        content = None
        
        # 優先使用網址抓取
        if input_url:
            with st.spinner("🌐 正在抓取網頁內容..."):
                content, error = fetch_webpage_content(input_url)
                if error:
                    st.warning(f"⚠️ {error}")
                    if manual_content:
                        st.info("使用您貼上的內容繼續分析...")
                        content = manual_content
        elif manual_content:
            content = manual_content
        
        if not content:
            st.error("請輸入網址或貼上網頁內容")
            st.stop()
        
        # 顯示抓取到的內容預覽
        with st.expander("📄 抓取到的內容預覽", expanded=False):
            st.text(content[:2000] + "..." if len(content) > 2000 else content)
        
        # AI 萃取關鍵字
        with st.spinner("🤖 AI 正在分析並萃取關鍵字..."):
            keywords_data, error = extract_keywords_from_content(
                GEMINI_API_KEY, content, product_name, MODEL_NAME
            )
        
        if error:
            st.error(f"❌ {error}")
            st.stop()
        
        # 為每個關鍵字取得相關詞
        st.info("🔄 正在擴展關聯字彙...")
        progress_bar = st.progress(0)
        
        all_keywords = []
        categories = ["pain_point_keywords", "product_keywords", "brand_keywords"]
        category_names = {"pain_point_keywords": "痛點字", "product_keywords": "產品字", "brand_keywords": "品牌字"}
        
        total_kw = sum(len(keywords_data.get(cat, [])) for cat in categories)
        processed = 0
        
        for category in categories:
            kw_list = keywords_data.get(category, [])
            for kw_item in kw_list:
                keyword = kw_item.get("keyword", "")
                if keyword:
                    related, _ = get_related_keywords_from_serp(
                        GOOGLE_API_KEY, keyword, TARGET_GL, TARGET_HL
                    )
                    all_keywords.append({
                        "category": category,
                        "category_name": category_names[category],
                        "keyword": keyword,
                        "search_intent": kw_item.get("search_intent", ""),
                        "related": related
                    })
                    processed += 1
                    progress_bar.progress(processed / total_kw)
                    time.sleep(0.3)  # 避免 API 過載
        
        progress_bar.empty()
        st.session_state.phase1_keywords = all_keywords
        st.success(f"✅ 成功萃取 {len(all_keywords)} 組關鍵字！")
    
    # 顯示關鍵字結果
    if st.session_state.phase1_keywords:
        st.divider()
        st.subheader("📋 萃取結果：請勾選要用於第二階段的關鍵字")
        
        keywords = st.session_state.phase1_keywords
        
        # 按類別分組顯示
        for category, cat_name, color in [
            ("pain_point_keywords", "🔴 痛點字", "#FFE4E1"),
            ("product_keywords", "🟢 產品字", "#E8F5E9"),
            ("brand_keywords", "🔵 品牌字", "#E3F2FD")
        ]:
            cat_keywords = [k for k in keywords if k["category"] == category]
            if cat_keywords:
                st.markdown(f"### {cat_name}")
                
                for kw in cat_keywords:
                    with st.container():
                        cols = st.columns([0.5, 3, 4])
                        with cols[0]:
                            checked = st.checkbox(
                                "選",
                                key=f"kw_{kw['keyword']}",
                                label_visibility="collapsed"
                            )
                        with cols[1]:
                            st.markdown(f"**{kw['keyword']}**")
                            st.caption(kw.get("search_intent", ""))
                        with cols[2]:
                            if kw.get("related"):
                                st.caption("關聯字：" + " • ".join(kw["related"][:4]))
        
        st.divider()
        
        # 收集勾選的關鍵字
        selected = []
        for kw in keywords:
            if st.session_state.get(f"kw_{kw['keyword']}", False):
                selected.append(kw["keyword"])
        
        col1, col2, col3 = st.columns([2, 1, 1])
        with col1:
            st.info(f"已選擇 {len(selected)} 組關鍵字")
        with col2:
            if st.button("✅ 全選"):
                for kw in keywords:
                    st.session_state[f"kw_{kw['keyword']}"] = True
                st.rerun()
        with col3:
            if st.button("❌ 全不選"):
                for kw in keywords:
                    st.session_state[f"kw_{kw['keyword']}"] = False
                st.rerun()
        
        if st.button("🎯 進入第二階段分析", type="primary", disabled=len(selected) == 0):
            st.session_state.selected_keywords = selected
            st.session_state.phase1_completed = True
            st.success("✅ 已將關鍵字傳遞至第二階段，請切換分頁！")


# =================================================
# 7.2 第二階段：SERP 戰略分析
# =================================================
with tab2:
    st.markdown("""
    ### 📊 SERP 戰略分析
    針對關鍵字進行搜尋結果分析，產出策略建議與內容寫作方向指引。
    """)
    
    # Google CSE 預覽
    with st.expander("👀 Google 搜尋預覽（不耗 API）"):
        components.html(
            f"""
            <script async src="https://cse.google.com/cse.js?cx={SEARCH_ENGINE_ID}"></script>
            <div class="gcse-search"></div>
            """,
            height=600,
            scrolling=True
        )
    
    # 關鍵字輸入
    if st.session_state.phase1_completed and st.session_state.selected_keywords:
        default_keywords = "\n".join(st.session_state.selected_keywords)
        st.success(f"✅ 已從第一階段接收 {len(st.session_state.selected_keywords)} 組關鍵字")
    else:
        default_keywords = ""
    
    keywords_input = st.text_area(
        "輸入關鍵字（每行一個，自動去重）",
        value=default_keywords,
        height=150,
        placeholder="空氣清淨機 推薦\nCRM 系統比較\n辦公椅 ptt"
    )
    
    # 顯示預估資訊
    if keywords_input.strip():
        keywords_preview = list(dict.fromkeys([k.strip() for k in keywords_input.split("\n") if k.strip()]))
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("關鍵字數", len(keywords_preview))
        with col2:
            st.metric("預估 SERP 呼叫", len(keywords_preview) * MAX_PAGES)
        with col3:
            st.metric("預估 Gemini 呼叫", len(keywords_preview) + 1)  # +1 for content direction
    
    if st.button("🚀 啟動戰略分析", type="primary", key="phase2_btn"):
        if not (GOOGLE_API_KEY and GEMINI_API_KEY):
            st.error("請輸入 Google API Key 與 Gemini API Key")
            st.stop()

        keywords = list(dict.fromkeys([k.strip() for k in keywords_input.split("\n") if k.strip()]))
        
        if not keywords:
            st.warning("請輸入至少一個關鍵字")
            st.stop()

        # 初始化執行器
        executor = RateLimitedExecutor(
            max_concurrent_serp=MAX_CONCURRENT_SERP,
            max_concurrent_gemini=MAX_CONCURRENT_GEMINI,
            gemini_min_interval=GEMINI_MIN_INTERVAL
        )
        
        # UI 元素
        st.divider()
        status_header = st.empty()
        status_header.info(f"⚡ 平行處理中... SERP×{MAX_CONCURRENT_SERP} / Gemini×{MAX_CONCURRENT_GEMINI}")
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # 收集結果
        all_results = OrderedDict()
        completed_count = 0
        total_start_time = time.time()
        
        # 平行執行
        max_workers = max(MAX_CONCURRENT_SERP, MAX_CONCURRENT_GEMINI) + 1
        
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_kw = {
                pool.submit(
                    process_single_keyword,
                    kw, executor, GOOGLE_API_KEY, GEMINI_API_KEY,
                    TARGET_GL, TARGET_HL, MAX_PAGES, MODEL_NAME
                ): kw for kw in keywords
            }
            
            for future in as_completed(future_to_kw):
                kw = future_to_kw[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {
                        "keyword": kw,
                        "error": str(e),
                        "serp_df": None,
                        "strategy": None
                    }
                
                all_results[kw] = result
                completed_count += 1
                
                progress_bar.progress(completed_count / len(keywords))
                status_text.text(f"✅ 完成：{kw} ({completed_count}/{len(keywords)})")
        
        total_time = time.time() - total_start_time
        
        status_header.success(f"✅ SERP 分析完成！總耗時 {total_time:.1f} 秒")
        status_text.empty()
        
        # 執行統計
        with st.expander("📊 執行統計", expanded=False):
            stat_cols = st.columns(4)
            with stat_cols[0]:
                st.metric("SERP 呼叫次數", executor.stats["serp_calls"])
            with stat_cols[1]:
                st.metric("Gemini 呼叫次數", executor.stats["gemini_calls"])
            with stat_cols[2]:
                st.metric("Gemini 重試次數", executor.stats["gemini_retries"])
            with stat_cols[3]:
                st.metric("總耗時", f"{total_time:.1f}s")
            
            if executor.stats["errors"]:
                st.warning(f"發生 {len(executor.stats['errors'])} 個錯誤")
                for err in executor.stats["errors"]:
                    st.text(err)
        
        st.divider()
        
        # 顯示結果
        reports = []
        serp_all_rows = []
        
        for kw in keywords:
            r = all_results.get(kw)
            if not r:
                continue
            
            st.subheader(f"🔍 {kw}")
            
            if r.get("timing"):
                timing = r["timing"]
                st.caption(f"⏱️ SERP: {timing.get('serp', 0):.1f}s ｜ Gemini: {timing.get('gemini', 0):.1f}s")
            
            if r.get("error"):
                st.error(f"❌ 處理失敗：{r['error']}")
                st.divider()
                continue
            
            df = r.get("serp_df")
            strategy = r.get("strategy")
            
            # 收集 SERP 原始資料
            if df is not None and not df.empty:
                serp_copy = df.copy()
                serp_copy.insert(0, "Keyword", kw)
                serp_all_rows.append(serp_copy)
            
            # 戰場分布
            with st.expander("📊 戰場分布", expanded=True):
                col1, col2 = st.columns([2, 1])
                with col1:
                    if df is not None:
                        st.dataframe(
                            df[["Rank", "Type", "Title", "DisplayLink"]], 
                            use_container_width=True, 
                            height=220
                        )
                with col2:
                    if df is not None and not df.empty:
                        type_counts = df["Type"].value_counts().reset_index()
                        type_counts.columns = ["Type", "Count"]
                        chart = alt.Chart(type_counts).mark_arc(innerRadius=50).encode(
                            theta="Count",
                            color="Type",
                            tooltip=["Type", "Count"]
                        )
                        st.altair_chart(chart, use_container_width=True)
            
            # 策略結論
            if strategy and "error" not in strategy:
                st.markdown("### 🧠 策略結論")
                
                col_a, col_b = st.columns(2)
                with col_a:
                    st.info(f"**使用者意圖**\n{strategy.get('User_Intent', 'N/A')}")
                    st.success(f"**機會缺口**\n{strategy.get('Opportunity_Gap', 'N/A')}")
                with col_b:
                    st.warning(f"**戰場狀態**\n{strategy.get('Battlefield_Status', 'N/A')}")
                    st.info(f"**建議頁型**\n{strategy.get('Recommended_Page_Type', 'N/A')}")

                st.markdown("**致勝切角**")
                for a in strategy.get("Winning_Angles", []):
                    st.markdown(f"- **{a.get('angle', '')}**（{a.get('target', '')}）")

                st.markdown("**必勝標題**")
                for t in strategy.get("Killer_Titles", []):
                    st.markdown(f"- {t.get('title', '')}｜{t.get('reason', '')}")

                strategy["Keyword"] = kw
                reports.append(strategy)
            
            elif strategy and "error" in strategy:
                st.error("❌ 策略解析失敗")
                with st.expander("查看原始回應"):
                    st.code(r.get("raw_response", "N/A"))
            
            st.divider()
        
        # =================================================
        # 內容寫作方向綜合指引（新功能）
        # =================================================
        if reports:
            st.header("📝 內容寫作方向綜合指引")
            
            with st.spinner("🤖 AI 正在產生內容策略建議..."):
                content_direction, error = generate_content_direction(
                    GEMINI_API_KEY, reports, keywords, MODEL_NAME
                )
            
            if error:
                st.error(f"❌ {error}")
            elif content_direction:
                # 核心主題
                st.markdown("### 🎯 核心主題方向")
                st.info(content_direction.get("content_theme", "N/A"))
                
                # 目標受眾
                st.markdown("### 👥 目標受眾")
                st.success(content_direction.get("target_audience", "N/A"))
                
                # 建議文章架構
                st.markdown("### 📐 建議文章架構")
                for section in content_direction.get("content_structure", []):
                    with st.container():
                        st.markdown(f"**{section.get('section', '')}**")
                        st.write(section.get('focus', ''))
                        if section.get('keywords_to_use'):
                            st.caption(f"建議使用關鍵字：{', '.join(section['keywords_to_use'])}")
                        st.markdown("---")
                
                # 必須涵蓋的主題
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown("### ✅ 必須涵蓋的主題")
                    for topic in content_direction.get("must_cover_topics", []):
                        st.markdown(f"- {topic}")
                
                with col2:
                    st.markdown("### ⚠️ 需避免的陷阱")
                    for pitfall in content_direction.get("avoid_pitfalls", []):
                        st.markdown(f"- {pitfall}")
                
                # 差異化切角
                st.markdown("### 💡 差異化切角")
                st.warning(content_direction.get("differentiation_angle", "N/A"))
                
                # 內容格式建議
                st.markdown("### 📄 建議內容格式")
                st.info(content_direction.get("content_format_suggestion", "N/A"))
            
            st.divider()
        
        # =================================================
        # Excel 輸出
        # =================================================
        if reports:
            st.subheader("📥 下載報告")
            
            # 策略工作表
            strategy_rows = []
            for r in reports:
                strategy_rows.append({
                    "Keyword": r.get("Keyword", ""),
                    "User_Intent": r.get("User_Intent", ""),
                    "Battlefield_Status": r.get("Battlefield_Status", ""),
                    "Opportunity_Gap": r.get("Opportunity_Gap", ""),
                    "Recommended_Page_Type": r.get("Recommended_Page_Type", ""),
                    "Winning_Angles": "\n".join(
                        [f"- {a.get('angle', '')}（{a.get('target', '')}）"
                         for a in r.get("Winning_Angles", [])]
                    ),
                    "Killer_Titles": "\n".join(
                        [f"- {t.get('title', '')}｜{t.get('reason', '')}"
                         for t in r.get("Killer_Titles", [])]
                    ),
                    "Raw_JSON": json.dumps(r, ensure_ascii=False)
                })

            df_strategy = pd.DataFrame(strategy_rows)
            
            # SERP 原始資料工作表
            df_serp_all = pd.concat(serp_all_rows, ignore_index=True) if serp_all_rows else pd.DataFrame()
            
            # 內容指引工作表
            df_content_direction = pd.DataFrame()
            if content_direction:
                df_content_direction = pd.DataFrame([{
                    "Content_Theme": content_direction.get("content_theme", ""),
                    "Target_Audience": content_direction.get("target_audience", ""),
                    "Differentiation_Angle": content_direction.get("differentiation_angle", ""),
                    "Content_Format": content_direction.get("content_format_suggestion", ""),
                    "Must_Cover_Topics": "\n".join(content_direction.get("must_cover_topics", [])),
                    "Avoid_Pitfalls": "\n".join(content_direction.get("avoid_pitfalls", [])),
                    "Content_Structure": json.dumps(content_direction.get("content_structure", []), ensure_ascii=False)
                }])

            # 寫入 Excel
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
                df_strategy.to_excel(writer, sheet_name="Strategy", index=False)
                
                if not df_serp_all.empty:
                    df_serp_all.to_excel(writer, sheet_name="SERP_Raw", index=False)
                
                if not df_content_direction.empty:
                    df_content_direction.to_excel(writer, sheet_name="Content_Direction", index=False)
                
                # 調整欄寬
                workbook = writer.book
                for sheet_name in writer.sheets:
                    worksheet = writer.sheets[sheet_name]
                    worksheet.set_column('A:A', 20)
                    worksheet.set_column('B:H', 40)

            col_dl1, col_dl2 = st.columns(2)
            with col_dl1:
                st.download_button(
                    label="📊 下載完整 Excel 報告",
                    data=buffer.getvalue(),
                    file_name=f"seo_strategy_{int(time.time())}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
            with col_dl2:
                # JSON 備份
                full_json = {
                    "strategies": reports,
                    "content_direction": content_direction
                }
                json_data = json.dumps(full_json, ensure_ascii=False, indent=2)
                st.download_button(
                    label="📄 下載 JSON 備份",
                    data=json_data,
                    file_name=f"seo_strategy_{int(time.time())}.json",
                    mime="application/json"
                )
