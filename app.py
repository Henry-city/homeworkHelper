import streamlit as st
import pandas as pd
import hashlib
import re
import base64
import requests
import fitz  # PyMuPDF
from io import BytesIO

# --- 页面配置 ---
st.set_page_config(
    page_title="作业查重与智能批改",
    page_icon="🎓",
    layout="wide"
)

# --- CSS 美化 ---
st.markdown("""
<style>
    .metric-card {
        background-color: #f0f2f6;
        border-radius: 10px;
        padding: 20px;
        text-align: center;
        border: 1px solid #e0e0e0;
    }
    .metric-value { font-size: 28px; font-weight: bold; color: #1f77b4; }
    .metric-label { color: #666; font-size: 14px; }
    .stDataFrame { border: 1px solid #eee; border-radius: 5px; }
    .stChatMessage { padding: 10px; border-radius: 5px; }
</style>
""", unsafe_allow_html=True)

# --- 核心功能函数 ---

def get_md5(file_bytes):
    """计算文件 MD5"""
    m = hashlib.md5()
    m.update(file_bytes)
    return m.hexdigest()

def extract_id(text):
    """从字符串中提取9位数字学号"""
    if not isinstance(text, str):
        text = str(text)
    match = re.search(r'\d{9}', text)
    return match.group() if match else None

def get_pdf_images_base64(file_bytes):
    """
    【升级版】读取 PDF 的每一页，并转换为 Base64 图片列表
    """
    images_b64 = []
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        # 循环处理每一页
        for page_num in range(len(doc)):
            page = doc[page_num]
            # 2倍缩放以保证 OCR 清晰度
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            img_data = pix.tobytes("png")
            b64_str = base64.b64encode(img_data).decode("utf-8")
            images_b64.append(b64_str)
        return images_b64
    except Exception as e:
        st.error(f"PDF 解析失败: {e}")
        return []

def call_vl_ocr(api_key, file_bytes, filename):
    """
    调用视觉大模型进行 OCR (支持多页)
    """
    # 1. 准备图片数据列表
    base64_list = []
    
    if filename.lower().endswith('.pdf'):
        base64_list = get_pdf_images_base64(file_bytes)
        mime = "image/png"
    else:
        # 单张图片处理
        b64 = base64.b64encode(file_bytes).decode("utf-8")
        base64_list = [b64]
        mime = "image/jpeg"
        
    if not base64_list: return "❌ 无法读取文件图像"

    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    
    # 2. 构建多图消息体
    content_payload = [{"type": "text", "text": "请识别以下所有图片中的文字，按顺序拼接，保持原有排版格式，输出 Markdown。"}]
    
    # 将每一页都加进去
    for b64_img in base64_list:
        content_payload.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64_img}"}
        })

    # 使用 Qwen2-VL (视觉能力最强)
    payload = {
        "model": "Qwen/Qwen2-VL-72B-Instruct",
        "messages": [{"role": "user", "content": content_payload}],
        "temperature": 0.1,
        "max_tokens": 4096 
    }
    
    try:
        # 因为图片多，可能传输慢，设置较长的超时
        resp = requests.post(url, headers=headers, json=payload, timeout=180)
        if resp.status_code != 200: return f"OCR API 错误 {resp.status_code}: {resp.text}"
        return resp.json()['choices'][0]['message']['content']
    except Exception as e:
        return f"OCR 请求异常: {str(e)}"

def call_ai_grader(api_key, content):
    """调用 API 进行评分 (已切换为更快的 Qwen2.5)"""
    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        # 🚀 切换为 Qwen2.5-72B，速度更快
        "model": "Qwen/Qwen2.5-72B-Instruct",
        "messages": [
            {"role": "system", "content": "你是一位严格的大学助教。"},
            {"role": "user", "content": f"请对以下作业进行评分(0-100)并给出简短评语：\n\n{content}"}
        ]
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        return resp.json()['choices'][0]['message']['content']
    except:
        return "评分服务超时或失败"

def call_chat_bot(api_key, messages):
    """调用 API 进行对话 (已切换为更快的 Qwen2.5)"""
    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        # 🚀 切换为 Qwen2.5-72B
        "model": "Qwen/Qwen2.5-72B-Instruct",
        "messages": messages,
        "stream": False 
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        if resp.status_code != 200: return f"API 报错: {resp.text}"
        return resp.json()['choices'][0]['message']['content']
    except Exception as e:
        return f"对话连接失败: {str(e)}"

# --- 主程序逻辑 ---

# 侧边栏
with st.sidebar:
    st.header("🛠️ 设置与上传")
    
    # --- 🔑 直接使用明文 Key，不再报错 ---
    default_key = "sk-mbmefdriwcavkosajtsgssddeerqiccggiuxmysydsnalghm"
    api_key = st.text_input("SiliconFlow API Key", value=default_key, type="password")
    
    st.divider()
    
    roster_file = st.file_uploader("1. 上传花名册 (Excel)", type=['xlsx', 'xls'])
    homework_files = st.file_uploader("2. 上传作业文件", accept_multiple_files=True)
    
    st.info("提示：花名册必须包含一列9位数字的学号。")

# 主界面
st.title("📊 作业检查看板")

if not roster_file:
    st.warning("👈 请先在左侧上传【花名册 Excel】")
    st.stop()

# 1. 处理花名册
try:
    df_roster = pd.read_excel(roster_file, dtype=str)
    roster_dict = {} 
    for idx, row in df_roster.iterrows():
        row_str = " ".join(row.fillna("").astype(str).values)
        sid = extract_id(row_str)
        if sid:
            name = "未知姓名"
            for item in row.values:
                item = str(item).strip()
                if item != sid and not item.isdigit() and len(item) >= 2:
                    name = item
                    break
            roster_dict[sid] = name
    all_students = set(roster_dict.keys())
    if not all_students:
        st.error("❌ 花名册读取失败：未找到任何9位学号。")
        st.stop()
except Exception as e:
    st.error(f"Excel 读取错误: {e}")
    st.stop()

# 2. 处理作业文件
submitted_data = []
files_map = {}
md5_map = {}
empty_files = []

if homework_files:
    for f in homework_files:
        fname = f.name
        if fname.startswith("~$") or fname.startswith("."): continue
        sid = extract_id(fname)
        if sid and sid in all_students:
            files_map[fname] = f
            f_bytes = f.getvalue()
            f_size = f.size
            submitted_data.append(sid)
            if f_size < 100:
                empty_files.append({"学号": sid, "姓名": roster_dict[sid], "文件名": fname, "大小": f"{f_size}B"})
            else:
                f_md5 = get_md5(f_bytes)
                if f_md5 not in md5_map: md5_map[f_md5] = []
                md5_map[f_md5].append((sid, fname))

# 3. 统计计算
submitted_ids = set(submitted_data)
missing_ids = all_students - submitted_ids
submit_rate = round(len(submitted_ids) / len(all_students) * 100, 1)

# 4. 显示顶部指标
c1, c2, c3, c4 = st.columns(4)
c1.markdown(f"<div class='metric-card'><div class='metric-value'>{len(all_students)}</div><div class='metric-label'>应交人数</div></div>", unsafe_allow_html=True)
c2.markdown(f"<div class='metric-card'><div class='metric-value'>{len(submitted_ids)}</div><div class='metric-label'>实交人数</div></div>", unsafe_allow_html=True)
c3.markdown(f"<div class='metric-card'><div class='metric-value' style='color:#d62728'>{len(missing_ids)}</div><div class='metric-label'>未交人数</div></div>", unsafe_allow_html=True)
c4.markdown(f"<div class='metric-card'><div class='metric-value'>{submit_rate}%</div><div class='metric-label'>提交率</div></div>", unsafe_allow_html=True)

st.write("") 

# 5. 功能选项卡
tab1, tab2, tab3 = st.tabs(["📋 名单详情", "🔍 异常检测", "🤖 AI 智能批改 + 答疑"])

with tab1:
    col_missing, col_submitted = st.columns(2)
    with col_missing:
        st.subheader("❌ 未交名单")
        if missing_ids:
            missing_list = [{"学号": sid, "姓名": roster_dict[sid]} for sid in sorted(missing_ids)]
            st.dataframe(missing_list, use_container_width=True, hide_index=True)
        else:
            st.success("🎉 所有人均已提交！")
    with col_submitted:
        st.subheader("✅ 已交名单")
        if submitted_ids:
            with st.expander("点击查看已交详情"):
                st.write(f"共 {len(submitted_ids)} 人")
                st.write(", ".join([f"{roster_dict[sid]}" for sid in submitted_ids]))
        else:
            st.info("暂无提交数据")

with tab2:
    col_dup, col_empty = st.columns(2)
    with col_dup:
        st.subheader("👯 疑似雷同")
        dup_groups = [v for k, v in md5_map.items() if len(v) > 1]
        if not dup_groups:
            st.success("✅ 未发现雷同文件")
        else:
            for i, group in enumerate(dup_groups, 1):
                st.warning(f"雷同组 #{i} (共{len(group)}人)")
                for sid, fname in group:
                    st.text(f"- {sid} {roster_dict[sid]} : {fname}")
    with col_empty:
        st.subheader("📄 异常/空文件")
        if not empty_files:
            st.success("✅ 文件大小正常")
        else:
            st.dataframe(empty_files, use_container_width=True)

with tab3:
    st.subheader("🤖 Qwen2.5 智能批改 & 互动")
    
    pdf_candidates = [f for f in files_map.keys() if f.lower().endswith('.pdf')]
    
    if not pdf_candidates:
        st.warning("⚠️ 请上传 PDF 格式的作业以使用此功能")
    else:
        sel_file = st.selectbox("选择要批改的作业:", pdf_candidates)
        
        # --- 会话状态管理 ---
        if "last_sel_file" not in st.session_state:
            st.session_state.last_sel_file = sel_file
        
        # 如果切换了文件，重置状态
        if st.session_state.last_sel_file != sel_file:
            st.session_state.current_analysis = None
            st.session_state.chat_messages = []
            st.session_state.last_sel_file = sel_file

        if "current_analysis" not in st.session_state:
            st.session_state.current_analysis = None
        if "chat_messages" not in st.session_state:
            st.session_state.chat_messages = []

        # --- 按钮与核心逻辑 ---
        if st.button("🚀 开始全页分析", type="primary"):
            target_f = files_map[sel_file]
            target_f.seek(0)
            file_data = target_f.read()
            
            with st.status("AI 正在全力处理...", expanded=True) as status:
                st.write("👀 正在阅读作业所有页面 (多页OCR)...")
                # 调用多页OCR
                ocr_res = call_vl_ocr(api_key, file_data, sel_file)
                
                if "❌" in ocr_res or "API 错误" in ocr_res:
                    status.update(label="处理失败", state="error")
                    st.error(ocr_res)
                else:
                    st.write("🧠 正在评分 (Qwen2.5)...")
                    eval_res = call_ai_grader(api_key, ocr_res)
                    status.update(label="分析完成", state="complete")
                    
                    st.session_state.current_analysis = {
                        "ocr": ocr_res,
                        "eval": eval_res
                    }
                    st.session_state.chat_messages = []

        # --- 结果与聊天 ---
        if st.session_state.current_analysis:
            data = st.session_state.current_analysis
            
            st.divider()
            c_left, c_right = st.columns(2)
            with c_left:
                st.markdown("#### 📄 作业识别内容 (全部页面)")
                st.text_area("", data["ocr"], height=300, disabled=True)
            with c_right:
                st.markdown("#### 📝 评价报告")
                st.markdown(data["eval"])
            
            st.divider()
            st.subheader("💬 作业助手 Qwen")
            st.caption("基于这份作业内容，您可以问：这道题为什么错了？如何改进？")

            for msg in st.session_state.chat_messages:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])

            if prompt := st.chat_input("输入问题..."):
                st.session_state.chat_messages.append({"role": "user", "content": prompt})
                with st.chat_message("user"):
                    st.markdown(prompt)

                system_prompt = f"""
                你是一个作业辅导助手。
                【作业全文内容】：
                {data['ocr']}
                
                【评分结果】：
                {data['eval']}
                
                请基于以上信息回答用户提问。
                """
                
                api_messages = [{"role": "system", "content": system_prompt}] + st.session_state.chat_messages

                with st.chat_message("assistant"):
                    with st.spinner("思考中..."):
                        response = call_chat_bot(api_key, api_messages)
                        st.markdown(response)
                
                st.session_state.chat_messages.append({"role": "assistant", "content": response})

with st.expander("🛠️ 调试面板"):
    st.write(f"花名册解析人数: {len(all_students)}")
    st.write(f"上传文件数: {len(homework_files) if homework_files else 0}")