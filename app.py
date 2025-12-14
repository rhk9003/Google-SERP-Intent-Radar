import streamlit as st
import pandas as pd
from googleapiclient.discovery import build
import google.generativeai as genai
import time
import random
import json

# --- 1. 頁面基礎設定 ---
st.set_page_config(
    page_title="Google SERP 戰略雷達 v2.0 (Pro)",
    page_icon="🌐",
    layout="wide"
)

st.title("🌐 Google SERP 戰略雷達 v2.0")
st.markdown("""
### Private SEO Weapon: Localized Intent Analysis
此工具透過 Google Custom Search API 抓取真實搜尋結果 (SERP)，並利用 Gemini 進行意圖解碼與內容缺口分析。
""")

# --- 2. 側邊欄：設定與金鑰 ---
with st.sidebar:
    st.header("🔑 啟動金鑰")
    st.info("請確保已啟用 Google Custom Search API")
    GOOGLE_API_KEY = st.text_input("Google API Key", type="password")
    
    # [防呆機制] 自動移除使用者可能不小心貼上的 "cx=" 前綴
    raw_cx = st.text_input("Search Engine ID (CX)", type="password")
    SEARCH_ENGINE_ID = raw_cx.replace("cx=", "").strip() if raw_cx else ""
    
    GEMINI_API_KEY = st.text_input("Gemini API Key", type="password")

    st.divider()
    st.header("🧠 模型設定")
    MODEL_NAME = st.selectbox(
        "選擇 AI 模型",
        ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-pro-preview"],
        index=0,
        help="Flash 速度快且便宜；Pro 推理能力強；3.0 Preview 為最新最強大模型 (需注意 API 配額)"
    )

    st.divider()
    st.header("🌍 戰場設定")
    TARGET_GL = st.text_input("地區 (gl)", value="tw", help="搜尋結果的地理位置，例如: tw, us, jp")
    TARGET_HL = st.text_input("語言 (hl)", value="zh-TW", help="介面語言，例如: zh-TW, en")
    MAX_PAGES = st.slider("抓取頁數", 1, 3, 2, help="1頁=Top10, 2頁=Top20 (注意：每多一頁會消耗一次 API Quota)")

# --- 3. 輔助功能：頁面類型偵測 ---
def detect_page_type(item):
    """根據 URL 特徵與 Snippet 結構，簡單判斷頁面屬性"""
    link = item.get('link', '').lower()
    
    # 特徵關鍵字庫
    if any(x in link for x in ['forum', 'ptt.cc', 'dcard.tw', 'mobile01', 'reddit']):
        return "🗣️ Forum (論壇/UGC)"
    if any(x in link for x in ['/product/', 'shopee', 'momo', 'pchome', 'amazon', 'rakuten']):
        return "🛒 E-commerce (電商)"
    if any(x in link for x in ['/news/', 'news.', 'udn.com', 'ltn.com']):
        return "📰 News (新聞)"
    if '.gov' in link:
        return "🏛️ Government (政府)"
    if 'wiki' in link or 'wikipedia' in link:
        return "📖 Wiki (維基)"
    if 'blog' in link or 'article' in link:
        return "📝 Blog (部落格)"
        
    return "📄 General (一般頁面)"

# --- 4. 核心功能：Google SERP 爬蟲 (含 Retry 機制) ---
def get_google_serp_data(api_key, cx, keyword, gl='tw', hl='zh-TW', pages=1):
    service = build("customsearch", "v1", developerKey=api_key)
    all_results = []
    
    # 進度條 (顯示在主畫面)
    status_text = st.empty()
    
    for page in range(pages):
        start_index = (page * 10) + 1  # Google API 分頁邏輯: 1, 11, 21...
        retries = 3
        
        status_text.text(f"正在抓取第 {page + 1} 頁 (Rank {start_index}-{start_index+9})...")
        
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
                    # 嘗試提取更豐富的描述 (og:description 優先)
                    pagemap = item.get('pagemap', {})
                    metatags = pagemap.get('metatags', [{}])[0]
                    description = metatags.get('og:description', item.get('snippet'))
                    
                    all_results.append({
                        "Rank": start_index + i,
                        "Type": detect_page_type(item),
                        "Title": item.get('title'),
                        "Description": description,
                        "DisplayLink": item.get('displayLink'),
                        "Link": item.get('link')
                    })
                break # 成功則跳出 retry
                
            except Exception as e:
                retries -= 1
                wait_time = (3 - retries) * 2 + random.uniform(0, 1) # Exponential Backoff
                st.warning(f"連線不穩，第 {3-retries} 次重試中... ({e})")
                time.sleep(wait_time)
                if retries == 0:
                    st.error(f"❌ 無法抓取第 {page+1} 頁: {e}")
        
        time.sleep(1) # 避免觸發 Rate Limit
        
    status_text.empty() # 清除狀態文字
    return all_results

# --- 5. 核心功能：Gemini 意圖分析 (JSON 輸出) ---
def analyze_intent_with_gemini(api_key, keyword, df, gl, model_name):
    genai.configure(api_key=api_key)
    # [更新] 使用使用者選擇的模型
    model = genai.GenerativeModel(model_name)
    
    # 精簡資料以節省 Token
    data_str = df[['Rank', 'Type', 'Title', 'Description', 'DisplayLink']].to_string(index=False)
    
    prompt = f"""
    你是一個專精於 SEO 的戰略顧問。我們正在分析關鍵字「{keyword}」在 Google 搜尋結果 ({gl} 地區) 的前 {len(df)} 名分佈。
    
    以下是競爭對手數據 (Type 為初步判斷的頁面類型)：
    {data_str}
    
    請進行深度的搜尋意圖 (Search Intent) 解碼，並嚴格按照以下 JSON 格式輸出 (不要包含 Markdown ```json 標記，直接輸出 JSON 字串)：
    
    {{
        "User_Intent_Analysis": "使用者意圖分析 (例如：他們主要是想比價、找教學、還是找評價？)",
        "Market_Landscape": "目前戰場概況 (例如：電商霸榜、論壇討論度高、還是被大媒體壟斷？)",
        "Content_Gap": "內容缺口發現 (前幾名有什麼痛點沒講清楚？或是 Rank 靠後但內容很好的遺珠？)",
        "Winning_Strategy": "降維打擊策略 (如果我們要贏，該採取什麼獨特切角？)",
        "Killer_Titles": [
            {{ "title": "必勝標題1", "reason": "為什麼這個標題能贏？" }},
            {{ "title": "必勝標題2", "reason": "為什麼這個標題能贏？" }},
            {{ "title": "必勝標題3", "reason": "為什麼這個標題能贏？" }}
        ]
    }}
    """
    
    # [修正] 初始化 response 變數，避免 UnboundLocalError
    response = None
    
    try:
        response = model.generate_content(prompt)
        # 清理可能存在的 markdown 標記
        clean_text = response.text.strip()
        if clean_text.startswith("```json"):
            clean_text = clean_text[7:]
        if clean_text.endswith("```"):
            clean_text = clean_text[:-3]
        return json.loads(clean_text)
    except Exception as e:
        # [修正] 更安全的錯誤處理邏輯
        raw_text_content = "無回應內容"
        if response:
            try:
                raw_text_content = response.text
            except:
                raw_text_content = "無法讀取回應文字"
                
        return {"error": f"AI 解析失敗: {str(e)}", "raw_text": raw_text_content}

# --- 6. 主程式執行邏輯 ---
keywords_input = st.text_area("輸入關鍵字 (一行一個)", height=100, placeholder="空氣清淨機 推薦\nCRM 系統比較\n台北 燒肉 2025")

if st.button("🚀 啟動戰略雷達", type="primary"):
    # 檢查 Key
    if not (GOOGLE_API_KEY and SEARCH_ENGINE_ID and GEMINI_API_KEY):
        st.warning("⚠️ 請先在左側欄位輸入所有 API Key")
        st.stop()
        
    if not keywords_input.strip():
        st.warning("⚠️ 請輸入至少一個關鍵字")
        st.stop()

    keywords = [k.strip() for k in keywords_input.split('\n') if k.strip()]
    
    # 總體進度條
    main_progress = st.progress(0)
    
    # 建立一個列表來儲存所有報告數據
    report_data = []
    
    for idx, kw in enumerate(keywords):
        st.subheader(f"🔍 分析目標：{kw}")
        
        # 1. 抓取資料
        with st.spinner(f"正在掃描 Google SERP (Top {MAX_PAGES*10})..."):
            raw_data = get_google_serp_data(GOOGLE_API_KEY, SEARCH_ENGINE_ID, kw, TARGET_GL, TARGET_HL, MAX_PAGES)
            
        if raw_data:
            df = pd.DataFrame(raw_data)
            
            # 2. 顯示數據
            with st.expander(f"📊 {kw} - SERP 戰場數據 (點擊展開)", expanded=False):
                st.dataframe(df, use_container_width=True)
            
            # 3. AI 分析
            with st.spinner(f"🧠 {MODEL_NAME} 正在計算戰略 ({kw})..."):
                # [更新] 傳入 MODEL_NAME
                analysis_result = analyze_intent_with_gemini(GEMINI_API_KEY, kw, df, TARGET_GL, MODEL_NAME)
                
                if "error" in analysis_result:
                    st.error(f"❌ {analysis_result['error']}")
                    # 只有當有原始文字時才顯示，避免畫面混亂
                    if analysis_result["raw_text"] != "無回應內容":
                        st.text(f"Raw Output: {analysis_result['raw_text']}")
                else:
                    # 美化輸出
                    st.markdown("#### 📝 戰略分析報告")
                    
                    c1, c2 = st.columns(2)
                    with c1:
                        st.info(f"**使用者意圖：**\n{analysis_result.get('User_Intent_Analysis', '')}")
                    with c2:
                        st.warning(f"**戰場概況：**\n{analysis_result.get('Market_Landscape', '')}")
                        
                    st.success(f"**💡 內容缺口與機會：**\n{analysis_result.get('Content_Gap', '')}")
                    
                    st.markdown("##### 🎯 降維打擊策略")
                    st.write(analysis_result.get('Winning_Strategy', ''))
                    
                    st.markdown("##### 🏆 建議必勝標題")
                    for t in analysis_result.get('Killer_Titles', []):
                        st.markdown(f"- **{t['title']}**\n  - *{t['reason']}*")

                    # [新增] 收集數據供下載
                    titles_formatted = "\n".join([f"- {t['title']} ({t['reason']})" for t in analysis_result.get('Killer_Titles', [])])
                    report_data.append({
                        "Keyword": kw,
                        "User_Intent_Analysis": analysis_result.get('User_Intent_Analysis', ''),
                        "Market_Landscape": analysis_result.get('Market_Landscape', ''),
                        "Content_Gap": analysis_result.get('Content_Gap', ''),
                        "Winning_Strategy": analysis_result.get('Winning_Strategy', ''),
                        "Killer_Titles": titles_formatted
                    })
        else:
            st.error(f"❌ 無法抓取 {kw} 的資料，請檢查 API 配額。")
            
        st.divider()
        main_progress.progress((idx + 1) / len(keywords))
        
    st.success("✅ 所有關鍵字分析完成！")

    # [新增] 下載區塊
    if report_data:
        st.header("📥 下載戰略報告")
        st.caption("將所有分析結果匯出保存")
        
        # 準備 DataFrame
        df_report = pd.DataFrame(report_data)
        
        # 產生 CSV (使用 utf-8-sig 以確保 Excel 開啟中文不亂碼)
        csv_data = df_report.to_csv(index=False).encode('utf-8-sig')
        
        # 產生 JSON
        json_data = json.dumps(report_data, ensure_ascii=False, indent=2)
        
        col_d1, col_d2 = st.columns(2)
        
        with col_d1:
            st.download_button(
                label="📄 下載 Excel 友善 CSV",
                data=csv_data,
                file_name=f"seo_strategy_report_{int(time.time())}.csv",
                mime="text/csv"
            )
            
        with col_d2:
            st.download_button(
                label="📋 下載 JSON (完整結構)",
                data=json_data,
                file_name=f"seo_strategy_report_{int(time.time())}.json",
                mime="application/json"
            )
