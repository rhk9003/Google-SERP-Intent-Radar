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

# =================================================
# 0. 固定設定
# =================================================
SEARCH_ENGINE_ID = "23e43fb5e029f4b50"  # CX 寫死（非機密）

# =================================================
# 1. Page Config
# =================================================
st.set_page_config(
    page_title="Google SERP 戰略雷達 v3.3 (Parallel)",
    page_icon="🎯",
    layout="wide"
)

st.title("🎯 Google SERP 戰略雷達 v3.3")
st.markdown("""
### Private SEO Weapon: Battlefield Strategy Reader  
**SERP 戰場判讀 → 策略輸出（Excel）｜平行處理版**
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
        ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"],
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
# 2.1 Google CSE 預覽（不耗 Quota）
# =================================================
with st.expander("👀 Google 搜尋預覽（不耗 API）"):
    components.html(
        f"""
        <script async src="https://cse.google.com/cse.js?cx={SEARCH_ENGINE_ID}"></script>
        <div class="gcse-search"></div>
        """,
        height=600,
        scrolling=True
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
# 4. Helper Functions
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
    """
    抓取 SERP 資料（不使用 cache，因為要在 thread 中呼叫）
    """
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

        # 頁面間的間隔
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
        # 清理可能的 markdown 標記
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception:
        return None


def analyze_strategy_raw(api_key, keyword, df, gl, model_name):
    """
    執行 Gemini 策略分析（不使用 cache）
    """
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
        # 嘗試清理並解析
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned), raw
    except json.JSONDecodeError as e:
        # 嘗試修復
        fixed = repair_json(api_key, raw, str(e))
        if fixed:
            return fixed, raw
        return {"error": str(e), "raw_response": raw}, raw
    except Exception as e:
        return {"error": str(e)}, str(e)


def process_single_keyword(kw, executor, google_key, gemini_key, gl, hl, pages, model_name):
    """
    處理單一關鍵字的完整流程（SERP + 分析）
    設計為可在 ThreadPool 中執行
    """
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
# 5. Main App
# =================================================
keywords_input = st.text_area(
    "輸入關鍵字（每行一個，自動去重）",
    height=100,
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
        st.metric("預估 Gemini 呼叫", len(keywords_preview))


if st.button("🚀 啟動戰略分析", type="primary"):
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
    
    # 用於收集結果（保持順序）
    all_results = OrderedDict()
    completed_count = 0
    total_start_time = time.time()
    
    # =================================================
    # 平行執行
    # =================================================
    max_workers = max(MAX_CONCURRENT_SERP, MAX_CONCURRENT_GEMINI) + 1
    
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        # 提交所有任務
        future_to_kw = {
            pool.submit(
                process_single_keyword,
                kw, executor, GOOGLE_API_KEY, GEMINI_API_KEY,
                TARGET_GL, TARGET_HL, MAX_PAGES, MODEL_NAME
            ): kw for kw in keywords
        }
        
        # 收集完成的結果
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
            
            # 更新進度
            progress_bar.progress(completed_count / len(keywords))
            status_text.text(f"✅ 完成：{kw} ({completed_count}/{len(keywords)})")
    
    total_time = time.time() - total_start_time
    
    # 清理進度顯示
    status_header.success(f"✅ 全部完成！總耗時 {total_time:.1f} 秒")
    status_text.empty()
    
    # =================================================
    # 顯示統計
    # =================================================
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
    
    # =================================================
    # 按原始順序顯示結果
    # =================================================
    reports = []
    serp_all_rows = []  # 收集所有 SERP 資料
    
    for kw in keywords:
        r = all_results.get(kw)
        if not r:
            continue
        
        st.subheader(f"🔍 {kw}")
        
        # 顯示處理時間
        if r.get("timing"):
            timing = r["timing"]
            st.caption(f"⏱️ SERP: {timing.get('serp', 0):.1f}s ｜ Gemini: {timing.get('gemini', 0):.1f}s")
        
        # 錯誤處理
        if r.get("error"):
            st.error(f"❌ 處理失敗：{r['error']}")
            st.divider()
            continue
        
        df = r.get("serp_df")
        strategy = r.get("strategy")
        
        # 收集 SERP 原始資料（加入關鍵字欄位）
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

            # 加入報告
            strategy["Keyword"] = kw
            reports.append(strategy)
        
        elif strategy and "error" in strategy:
            st.error("❌ 策略解析失敗")
            with st.expander("查看原始回應"):
                st.code(r.get("raw_response", "N/A"))
        
        st.divider()

    # =================================================
    # 6. Excel 輸出（雙工作表版）
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

        # 寫入 Excel
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
            df_strategy.to_excel(writer, sheet_name="Strategy", index=False)
            
            if not df_serp_all.empty:
                df_serp_all.to_excel(writer, sheet_name="SERP_Raw", index=False)
            
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
            json_data = json.dumps(reports, ensure_ascii=False, indent=2)
            st.download_button(
                label="📄 下載 JSON 備份",
                data=json_data,
                file_name=f"seo_strategy_{int(time.time())}.json",
                mime="application/json"
            )
