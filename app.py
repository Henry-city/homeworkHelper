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

def pdf_to_image_base64(file_bytes):
    """PDF首页转图片Base64 (修复API格式报错)"""
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        if len(doc) < 1: return None
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        return base64.b64encode(pix.tobytes("png")).decode("utf-8")
    except Exception as e:
        return None

def call_deepseek_ocr(api_key, file_bytes, filename):
    """调用 API 进行 OCR"""
    if filename.lower().endswith('.pdf'):
        b64_img = pdf_to_image_base64(file_bytes)
        mime = "image/png"
    else:
        b64_img = base64.b64encode(file_bytes).decode("utf-8")
        mime = "image/jpeg"
        
    if not b64_img: return "❌ 无法处理文件图像"

    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    
    # 使用 Qwen2-VL 进行视觉识别
    payload = {
        "model": "Qwen/Qwen2-VL-72B-Instruct",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64_img}"}},
                {"type": "text", "text": "识别图片中的所有文字，保持排版，输出 Markdown。"}
            ]
        }],
        "temperature": 0.1,
        "max_tokens": 2048
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        if resp.status_code != 200: return f"API 错误 {resp.status_code}: {resp.text}"
        return resp.json()['choices'][0]['message']['content']
    except Exception as e:
        return f"请求异常: {str(e)}"

def call_ai_grader(api_key, content):
    """调用 API 进行评分"""
    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-ai/DeepSeek-V3",
        "messages": [
            {"role": "system", "content": "你是一位严格的大学助教。"},
            {"role": "user", "content": f"请对以下作业进行评分(0-100)并给出简短评语：\n\n{content}"}
        ]
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        return resp.json()['choices'][0]['message']['content']
    except:
        return "评分服务连接失败"

# --- 主程序逻辑 ---

# 侧边栏
with st.sidebar:
    st.header("🛠️ 设置与上传")
    api_key = st.text_input("API Key", value="sk-mbmefdriwcavkosajtsgssddeerqiccggiuxmysydsnalghm", type="password")
    st.divider()
    
    roster_file = st.file_uploader("1. 上传花名册 (Excel)", type=['xlsx', 'xls'])
    homework_files = st.file_uploader("2. 上传作业文件", accept_multiple_files=True)
    
    st.info("提示：花名册必须包含一列9位数字的学号。")

# 主界面
st.title("📊 作业检查看板")

if not roster_file:
    st.warning("👈 请先在左侧上传【花名册 Excel】")
    st.stop()

# 1. 处理花名册 (关键修复：强制转字符串)
try:
    # dtype=str 强制所有内容读取为文本，防止数字/字符串不匹配
    df_roster = pd.read_excel(roster_file, dtype=str)
    
    roster_dict = {} # 学号 -> 姓名
    for idx, row in df_roster.iterrows():
        # 将整行转为字符串搜索
        row_str = " ".join(row.fillna("").astype(str).values)
        sid = extract_id(row_str)
        if sid:
            # 尝试找姓名：排除学号本身和纯数字，找剩下的较短字符串
            name = "未知姓名"
            for item in row.values:
                item = str(item).strip()
                if item != sid and not item.isdigit() and len(item) >= 2:
                    name = item
                    break
            roster_dict[sid] = name

    all_students = set(roster_dict.keys())
    
    if not all_students:
        st.error("❌ 花名册读取失败：未找到任何9位学号，请检查Excel格式。")
        st.dataframe(df_roster.head()) # 展示前几行帮助调试
        st.stop()
        
except Exception as e:
    st.error(f"Excel 读取错误: {e}")
    st.stop()

# 2. 处理作业文件
submitted_data = []  # 存储已交作业信息
files_map = {}       # 文件名 -> 文件对象
md5_map = {}         # MD5 -> [学号列表]
empty_files = []     # 空文件列表

if homework_files:
    for f in homework_files:
        fname = f.name
        # 过滤临时文件
        if fname.startswith("~$") or fname.startswith("."): continue
        
        # 提取学号
        sid = extract_id(fname)
        
        # 只要文件名里有学号，并且学号在花名册里，就算已交
        if sid and sid in all_students:
            # 保存文件引用
            files_map[fname] = f
            f_bytes = f.getvalue()
            f_size = f.size
            
            # 记录提交
            submitted_data.append(sid)
            
            # 异常检测：空文件
            if f_size < 100:
                empty_files.append({"学号": sid, "姓名": roster_dict[sid], "文件名": fname, "大小": f"{f_size}B"})
            else:
                # 异常检测：查重
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

st.write("") # 间距

# 5. 功能选项卡
tab1, tab2, tab3 = st.tabs(["📋 名单详情", "🔍 异常检测", "🤖 AI 智能批改"])

with tab1:
    col_missing, col_submitted = st.columns(2)
    
    with col_missing:
        st.subheader("❌ 未交名单")
        if missing_ids:
            # 构建表格数据
            missing_list = [{"学号": sid, "姓名": roster_dict[sid]} for sid in sorted(missing_ids)]
            st.dataframe(missing_list, use_container_width=True, hide_index=True)
        else:
            st.success("🎉 太棒了，所有人均已提交！")
            
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
        st.subheader("👯 疑似雷同 (内容完全一致)")
        # 过滤出只有1个人的组（即无雷同）
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
            st.success("✅ 文件大小均正常")
        else:
            st.dataframe(empty_files, use_container_width=True)

with tab3:
    st.subheader("🤖 DeepSeek 智能批改")
    st.caption("支持对 PDF 文件进行 OCR 识别并自动评价")
    
    # 筛选PDF
    pdf_candidates = [f for f in files_map.keys() if f.lower().endswith('.pdf')]
    
    if not pdf_candidates:
        st.warning("⚠️ 请上传 PDF 格式的作业以使用此功能")
    else:
        sel_file = st.selectbox("选择要批改的作业:", pdf_candidates)
        
        if st.button("🚀 开始分析", type="primary"):
            target_f = files_map[sel_file]
            target_f.seek(0)
            file_data = target_f.read()
            
            # 步骤1：OCR
            with st.status("正在进行 AI 处理...", expanded=True) as status:
                st.write("👀 正在阅读文档 (OCR)...")
                ocr_res = call_deepseek_ocr(api_key, file_data, sel_file)
                
                if "❌" in ocr_res or "API 错误" in ocr_res:
                    status.update(label="处理失败", state="error")
                    st.error(ocr_res)
                else:
                    st.write("🧠 正在思考评分 (DeepSeek-V3)...")
                    eval_res = call_ai_grader(api_key, ocr_res)
                    status.update(label="处理完成", state="complete")
                    
                    st.divider()
                    c_left, c_right = st.columns([1, 1])
                    with c_left:
                        st.markdown("#### 📄 识别内容")
                        st.text_area("", ocr_res, height=300)
                    with c_right:
                        st.markdown("#### 📝 评价报告")
                        st.markdown(eval_res)

# 调试信息 (如果还是不显示，可以展开这个看原因)
with st.expander("🛠️ 调试面板 (如果数据不显示请点这里)"):
    st.write(f"花名册解析人数: {len(all_students)}")
    if all_students:
        st.write(f"花名册样例学号: {list(all_students)[0]} (类型: {type(list(all_students)[0])})")
    st.write(f"上传文件数: {len(homework_files) if homework_files else 0}")