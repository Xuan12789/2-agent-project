from fastapi import FastAPI, Request, Form, UploadFile, File, Query
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
import requests
import sqlite3
import os
import tempfile
import re
import uuid
import json
from collections import Counter
from datetime import datetime, timedelta
import whisper
from rapidocr_onnxruntime import RapidOCR
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
font_name = "STSong-Light"

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ======================【全局：十类诈骗枚举清单，统一维护】======================
FRAUD_TYPE_LIST = [
    "刷单返利",
    "虚假网络投资理财",
    "虚假网络贷款",
    "冒充电商物流客服",
    "冒充公检法",
    "虚假征信",
    "虚假购物",
    "网络游戏产品虚假交易",
    "冒充领导熟人",
    "其他诈骗类型",
    "无诈骗风险"
]
FRAUD_TYPE_STR = "、".join(FRAUD_TYPE_LIST)
# =============================================================================

# ========= 加载轻量模型（CPU） =========
whisper_model = whisper.load_model("base", device="cpu")
ocr_reader = RapidOCR()

# ========= Ollama配置 =========
#OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
#MODEL_NAME = "qwen2.5:7b-instruct"

# ========= 套餐基础配置 =========
PACKAGE_LIST = {
    "free": {
        "name": "个人免费版",
        "price": "免费",
        "desc": "基础反诈知识库，永久免费",
        "cycle": "永久"
    },
    "elder": {
        "name": "老年守护单人包",
        "price": "¥88.00/年",
        "desc": "老年人反诈知识库，针对性防护",
        "cycle": "年付"
    },
    "teen": {
        "name": "青少年财商守护套餐",
        "price": "¥99.00/年",
        "desc": "青少年财商反诈防护知识库",
        "cycle": "年付"
    },
    "family": {
        "name": "家庭守护套餐",
        "price": "¥129.00/年",
        "desc": "长辈+全家防护库，多人反诈保护",
        "cycle": "年付"
    },
    "premium_year": {
        "name": "个人进阶会员(包年)",
        "price": "¥198.00/年",
        "desc": "全套反诈知识库，最高权限，包年",
        "cycle": "年付"
    },
    "premium_month": {
        "name": "个人进阶会员(包月)",
        "price": "¥25.00/月",
        "desc": "全套反诈知识库，最高权限，包月",
        "cycle": "月付"
    }
}
CHECK_TYPE_MAPPING = {
    "check_free": "free",
    "check_elder": "elder",
    "check_teen": "teen",
    "check_family": "family",
    "check_premium": ["premium_month", "premium_year"]
}

# -------------------- LLM输出JSON清洗函数 --------------------
def clean_llm_json(raw_text: str):
    pattern = r'```(?:json)?\s*\n(.*?)\n```'
    match = re.search(pattern, raw_text, re.DOTALL)
    if match:
        raw_text = match.group(1)
    raw_text = raw_text.strip()
    try:
        jdata = json.loads(raw_text)
        fraud_t = jdata.get("fraud_type", "其他诈骗类型")
        risk_t = jdata.get("risk_level", "中风险")
        if fraud_t not in FRAUD_TYPE_LIST:
            fraud_t = "其他诈骗类型"
        return {"fraud_type": fraud_t, "risk_level": risk_t}
    except Exception as e:
        print(f"⚠ JSON解析异常: {e},原始文本:{raw_text[:300]}")
        return {
            "fraud_type": "其他诈骗类型",
            "risk_level": "中风险"
        }

# ========= 数据库初始化 =========
def init_db():
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        phone TEXT
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS user_package(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        pkg_code TEXT,
        pkg_name TEXT,
        buy_time TIMESTAMP,
        expire_time TEXT
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS check_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        check_type TEXT,
        input_text TEXT,
        fact_result TEXT,
        bias_result TEXT,
        fraud_type TEXT,
        risk_level TEXT,
        create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    try:
        cur.execute("ALTER TABLE check_history ADD COLUMN fraud_type TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE check_history ADD COLUMN risk_level TEXT")
    except sqlite3.OperationalError:
        pass

    cur.execute('''
    CREATE TABLE IF NOT EXISTS buy_order(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        pkg_code TEXT,
        pkg_name TEXT,
        pay_price TEXT,
        pay_method TEXT,
        buy_time TIMESTAMP,
        expire_time TEXT
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS chat_session(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        check_type TEXT,
        origin_input TEXT,
        ocr_or_audio_text TEXT,
        fact_result TEXT,
        bias_result TEXT,
        create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT "open"
    )
    ''')
    try:
        cur.execute('ALTER TABLE chat_session ADD COLUMN session_type TEXT DEFAULT "normal"')
    except sqlite3.OperationalError:
        pass

    cur.execute('''
    CREATE TABLE IF NOT EXISTS chat_message(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        sender_type TEXT,
        content TEXT,
        create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS user_bind (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_user_id INTEGER,
        bind_user_id INTEGER,
        bind_type TEXT,
        create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS user_notice (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        receive_user_id INTEGER,
        msg TEXT,
        is_read INTEGER DEFAULT 0,
        create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    conn.commit()
    conn.close()
init_db()

def get_user_valid_pkg_list(uid: int):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    pkg_set = set()
    cur.execute("""
        SELECT pkg_code FROM user_package
        WHERE user_id=? AND (expire_time='永久' OR expire_time >= ?)
    """, (uid, now_str))
    rows_self = cur.fetchall()
    for r in rows_self:
        pkg_set.add(r[0])
    cur.execute('''
        SELECT DISTINCT ub.bind_type
        FROM user_bind ub
        INNER JOIN user_package up
            ON ub.owner_user_id = up.user_id AND ub.bind_type = up.pkg_code
        WHERE ub.bind_user_id=? AND (up.expire_time='永久' OR up.expire_time >= ?)
    ''', (uid, now_str))
    rows_share = cur.fetchall()
    for r in rows_share:
        pkg_set.add(r[0])
    conn.close()
    return list(pkg_set)

def get_own_pkg_list(uid:int):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
        SELECT pkg_code FROM user_package
        WHERE user_id=? AND (expire_time='永久' OR expire_time >= ?)
    """, (uid, now_str))
    res = [row[0] for row in cur.fetchall()]
    conn.close()
    return res

def get_uid_by_username(username:str):
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username=?", (username.strip(),))
    row = cur.fetchone()
    conn.close()
    if row:
        return row[0]
    return None

def add_notice(cur, receive_uid:int, msg:str):
    cur.execute("INSERT INTO user_notice(receive_user_id,msg) VALUES (?,?)", (receive_uid,msg))

def parse_fraud_money(text:str) -> int:
    import re
    max_val = 0
    matches_num_wan = re.findall(r"(\d+(?:\.\d+)?)万", text)
    for s in matches_num_wan:
        try:
            val = float(s) * 10000
            if val > max_val:
                max_val = val
        except Exception:
            pass
    cn_digit = {
        "零":0,"一":1,"二":2,"三":3,"四":4,"五":5,
        "六":6,"七":7,"八":8,"九":9,"十":10
    }
    units = [("千万",10000000),("百万",1000000),("十万",100000),("万",10000)]
    for unit_name,unit_num in units:
        pattern = re.compile(rf"([一二三四五六七八九十]+){unit_name}")
        res_list = pattern.findall(text)
        for num_str in res_list:
            base = 0
            for ch in num_str:
                base = base * 10 + cn_digit.get(ch,0)
            money = base * unit_num
            if money>max_val:
                max_val = money
    single_unit = [("千万",10000000),("百万",1000000),("十万",100000)]
    for unit_name,unit_num in single_unit:
        if re.search(rf"一{unit_name}",text):
            money = 1 * unit_num
            if money>max_val:
                max_val = money
    matches_yuan = re.findall(r"(\d+)元", text)
    for s in matches_yuan:
        try:
            val = int(s)
            if val>max_val:
                max_val = val
        except Exception:
            pass
    return int(max_val)

def generate_bind_notice(cur, trigger_uid:int, check_type:str, detect_text:str):
    print(f"[提醒调试] trigger_uid={trigger_uid}, check_type={check_type}")
    cur.execute("SELECT id FROM user_bind WHERE owner_user_id=? AND bind_type=?", (trigger_uid, check_type))
    is_owner = cur.fetchone()
    cur.execute("SELECT owner_user_id FROM user_bind WHERE bind_user_id=? AND bind_type=?", (trigger_uid, check_type))
    bind_owner_row = cur.fetchone()
    owner_id = None
    if is_owner:
        owner_id = trigger_uid
        print(f"[提醒调试] 当前用户是套餐主账号 owner_id={owner_id}")
    elif bind_owner_row:
        owner_id = bind_owner_row[0]
        print(f"[提醒调试] 当前用户是被绑定账号，所属主账号 owner_id={owner_id}")
    else:
        print("[提醒调试] 没有找到对应绑定关系，跳过提醒")
        return
    if check_type == "elder" or check_type == "teen":
        msg = f"⚠️提醒：你绑定的账号检测识别出诈骗风险金额超过10万元，请留意。检测摘要：{detect_text[:120]}"
        add_notice(cur, owner_id, msg)
        print(f"[提醒调试] elder/teen，向主账号{owner_id}写入提醒消息")
    elif check_type == "family":
        all_uids = set()
        all_uids.add(owner_id)
        cur.execute("SELECT bind_user_id FROM user_bind WHERE owner_user_id=? AND bind_type='family'",(owner_id,))
        bind_rows = cur.fetchall()
        for buid in bind_rows:
            all_uids.add(buid[0])
        msg = f"⚠️家庭组提醒：组内账号识别诈骗风险金额超过10万元，请留意。检测摘要：{detect_text[:120]}"
        for member_uid in all_uids:
            add_notice(cur, member_uid, msg)
        print(f"[提醒调试] family套餐，发送提醒给全部组员：{list(all_uids)}")

import os
from dotenv import load_dotenv
from openai import OpenAI

# 加载环境变量
load_dotenv()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

def ollama_chat(prompt: str) -> str:
    if not DEEPSEEK_API_KEY:
        return "错误：未配置 DEEPSEEK_API_KEY，请检查环境变量"
    try:
        client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com"
        )
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role":"user","content": prompt}],
            temperature=0.05,
            stream=False,
            timeout=360
        )
        content = resp.choices[0].message.content
        return content
    except Exception as e:
        print("【DeepSeek API调用异常】", e)
        return f"大模型调用失败：{str(e)}"

def load_knowledge_file(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    chunks = [c.strip() for c in text.split("\n") if len(c.strip()) > 4]
    return chunks

def rag_search(query_text: str, pkg_code: str, top_k=3):
    file_list = ["knowledge/base_fraud.txt"]
    if pkg_code in ("premium_year", "premium_month"):
        file_list.extend(["knowledge/elder_fraud.txt", "knowledge/family_fraud.txt", "knowledge/premium_fraud.txt"])
    elif pkg_code == "elder":
        file_list.append("knowledge/elder_fraud.txt")
    elif pkg_code == "teen":
        file_list.append("knowledge/teen_fraud.txt")
    elif pkg_code == "family":
        file_list.extend(["knowledge/elder_fraud.txt", "knowledge/family_fraud.txt"])
    all_chunks = []
    for fp in file_list:
        all_chunks.extend(load_knowledge_file(fp))
    if len(all_chunks) == 0:
        return "暂无知识库资料"
    query_words = set(query_text.lower().split())
    scored = []
    for chunk in all_chunks:
        chunk_words = set(chunk.lower().split())
        score = len(query_words & chunk_words)
        scored.append((-score, chunk))
    scored.sort()
    result_chunks = [item[1] for item in scored[:top_k]]
    return "\n".join(result_chunks)

import json
import re
import ollama

FRAUD_TYPE_LIST = [
    "刷单返利",
    "虚假网络投资理财",
    "虚假网络贷款",
    "冒充电商物流客服",
    "冒充公检法",
    "虚假征信",
    "虚假购物",
    "网络游戏产品虚假交易",
    "冒充领导熟人",
    "其他诈骗类型",
    "无诈骗风险"
]

def run_detect_logic(input_text: str, pkg_code: str):
    knowledge_context = rag_search(input_text, pkg_code)
    # ============调试打印，看RAG拿到的知识库内容=========
    print("【RAG知识库检索结果】", knowledge_context)

    fact_prompt = f"""
你是金融反诈事实核验员，**必须严格参考下面给到的【知识库检索材料】**分析用户输入内容，识别虚假宣传、违规荐股、高收益骗局。
输出固定包含3个小节，每个小节必须带上小标题，**不许省略任何一个小节**：
1.【风险结论】：判断属于哪一类风险，简述风险点
2.【具体违规点】：逐条说明话术里面的诈骗/违规特征
3.【引用知识库依据】：摘抄知识库原文片段作为依据；
👉如果知识库没有匹配信息，该小节固定写：**资料不足，建议人工进一步核实。**

【知识库检索材料】
{knowledge_context}
【用户待检测内容】
{input_text}

必须从列表【{FRAUD_TYPE_STR}】，选择一个最匹配的诈骗类型标签；同时给出风险等级：高风险/中风险/低风险/无风险。
普通闲聊、工作通知请选fraud_type="无诈骗风险",risk_level="无风险"。

在输出内容的**最后一行**，只输出纯JSON，格式严格为{{"诈骗类型":"xxx","风险等级":"xxx"}}，不要markdown、不要任何多余文字，仅一行JSON。
"""
    full_output = ollama_chat(fact_prompt)

    bias_prompt = f"""
你是行为金融学投教顾问。结合【用户原始内容】以及【事实核验结果】，分析该骗局利用投资者哪些心理弱点（暴富幻想、从众心理、迷信权威、损失厌恶等），
输出通俗心理纠偏科普，解释普通人为什么容易受骗，给出实用防范建议。
如果判定无诈骗风险，则简单说明文本无诈骗诱导，保持警惕即可。

【事实核验结果】
{full_output}
【用户原始内容】
{input_text}
"""
    bias_res = ollama_chat(bias_prompt)

    fraud_type = "其他诈骗类型"
    risk_level = "中风险"
    fact_res = full_output
    json_pattern = re.search(r'\{.*?"fraud_type".*?\}', full_output, re.DOTALL)
    if json_pattern:
        raw_json_str = json_pattern.group(0)
        json_result = clean_llm_json(raw_json_str)
        fraud_type = json_result["fraud_type"]
        risk_level = json_result["risk_level"]
    else:
        print("⚠未捕获JSON片段，使用默认诈骗标签")

    print("-----DEBUG------")
    print("fact_res完整内容：", fact_res[:500])
    print(f"fraud_type={fraud_type}, risk_level={risk_level}")
    print("----------------")
    return fact_res, bias_res, fraud_type, risk_level

# ========= 路由 =========
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = request.session.get("user")
    return templates.TemplateResponse("index.html", {"request": request, "user": user})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    cur.execute("SELECT id,username FROM users WHERE username=? AND password=?", (username, password))
    row = cur.fetchone()
    conn.close()
    if row:
        uid = row[0]
        valid_pkgs = get_user_valid_pkg_list(uid)
        request.session["user"] = {"id": uid, "username": row[1], "valid_pkgs": valid_pkgs}
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "msg": "账号密码错误"})

@app.get("/register", response_class=HTMLResponse)
async def reg_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.post("/register")
async def reg_submit(request: Request, username: str = Form(""), password: str = Form(""), phone: str = Form("")):
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO users(username,password,phone) VALUES (?,?,?)", (username, password, phone))
        uid = cur.lastrowid
        cur.execute('''INSERT INTO user_package(user_id,pkg_code,pkg_name,buy_time,expire_time)
            VALUES (?,?,?,?,?)''', (uid, "free", PACKAGE_LIST["free"]["name"], datetime.now(), "永久"))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return templates.TemplateResponse("register.html", {"request": request, "msg": "账号已存在"})
    conn.close()
    return RedirectResponse("/login", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse("/", status_code=303)

@app.get("/package", response_class=HTMLResponse)
async def package_page(request: Request):
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse("package.html", {"request": request, "user": user, "pkg_list": PACKAGE_LIST})

@app.get("/go_pay", response_class=HTMLResponse)
async def go_pay(request: Request, pkg: str):
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login", status_code=303)
    if pkg == "free":
        return RedirectResponse("/package", status_code=303)
    pkg_info = PACKAGE_LIST[pkg]
    return templates.TemplateResponse("pay.html", {
        "request": request,
        "user": user,
        "pkg": pkg,
        "pkg_name": pkg_info["name"],
        "pkg_desc": pkg_info["desc"],
        "pkg_price": pkg_info["price"],
        "err":""
    })

@app.post("/pay")
async def pay_page(request: Request, pkg: str = Form("")):
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login", status_code=303)
    if pkg == "free":
        return RedirectResponse("/package", status_code=303)
    pkg_info = PACKAGE_LIST[pkg]
    return templates.TemplateResponse("pay.html", {
        "request": request,
        "user": user,
        "pkg": pkg,
        "pkg_name": pkg_info["name"],
        "pkg_desc": pkg_info["desc"],
        "pkg_price": pkg_info["price"],
        "err":""
    })

@app.post("/confirm_pay")
async def confirm_pay(request: Request,
                      pkg: str = Form(""),
                      pkg_name: str = Form(""),
                      pkg_price: str = Form(""),
                      pay_method: str = Form(""),
                      bind_usernames: str = Form("")):
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login", status_code=303)
    uid = user["id"]
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    bind_uid_list = []
    need_bind = pkg in ("elder", "teen", "family")
    try:
        if need_bind:
            raw_names = [x.strip() for x in bind_usernames.split(",") if x.strip()]
            if pkg in ("elder", "teen"):
                if len(raw_names) != 1:
                    return templates.TemplateResponse("pay.html", {
                        "request": request, "user": user, "pkg": pkg,
                        "pkg_name": pkg_name, "pkg_desc": "", "pkg_price": pkg_price,
                        "err": "老年/青少年套餐必须填写1个家长用户名"
                    })
            if pkg == "family":
                if not (1 <= len(raw_names) <= 5):
                    return templates.TemplateResponse("pay.html", {
                        "request": request, "user": user, "pkg": pkg,
                        "pkg_name": pkg_name, "pkg_desc": "", "pkg_price": pkg_price,
                        "err": "家庭版至少绑定1个账号，最多5个账号，多个用户名用英文逗号分隔"
                    })
            for uname in raw_names:
                buid = get_uid_by_username(uname)
                if buid is None:
                    return templates.TemplateResponse("pay.html", {
                        "request": request, "user": user, "pkg": pkg,
                        "pkg_name": pkg_name, "pkg_desc": "", "pkg_price": pkg_price,
                        "err": f"用户名【{uname}】不存在"
                    })
                if buid == uid:
                    return templates.TemplateResponse("pay.html", {
                        "request": request, "user": user, "pkg": pkg,
                        "pkg_name": pkg_name, "pkg_desc": "", "pkg_price": pkg_price,
                        "err": "不能绑定自己账号"
                    })
                bind_uid_list.append(buid)

        if pkg == "premium_month":
            expire_dt = now + timedelta(days=30)
            expire_time = expire_dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            expire_dt = now + timedelta(days=365)
            expire_time = expire_dt.strftime("%Y-%m-%d %H:%M:%S")

        conn = sqlite3.connect("user.db", check_same_thread=False)
        cur = conn.cursor()
        cur.execute('''INSERT INTO buy_order(user_id,pkg_code,pkg_name,pay_price,pay_method,buy_time,expire_time)
                       VALUES (?,?,?,?,?,?,?)''', (uid, pkg, pkg_name, pkg_price, pay_method, now_str, expire_time))
        cur.execute('''INSERT INTO user_package(user_id,pkg_code,pkg_name,buy_time,expire_time)
                       VALUES (?,?,?,?,?)''', (uid, pkg, pkg_name, now_str, expire_time))

        for buid in bind_uid_list:
            cur.execute("INSERT INTO user_bind(owner_user_id,bind_user_id,bind_type) VALUES (?,?,?)",
                        (uid, buid, pkg))
        conn.commit()
        conn.close()

        valid_pkgs = get_user_valid_pkg_list(uid)
        request.session["user"] = {
            "id": uid,
            "username": user["username"],
            "valid_pkgs": valid_pkgs
        }
        return RedirectResponse("/user_center", status_code=303)
    except Exception as e:
        print("====购买异常====", str(e))
        return templates.TemplateResponse("pay.html", {
            "request": request, "user": user, "pkg": pkg,
            "pkg_name": pkg_name, "pkg_desc": "", "pkg_price": pkg_price,
            "err": f"处理异常：{str(e)}"
        })

@app.get("/user_center", response_class=HTMLResponse)
async def user_center(request: Request, err_msg: str = Query(None)):
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login", 303)
    uid = user["id"]
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    own_pkg_list = get_own_pkg_list(uid)
    is_elder_owner = "elder" in own_pkg_list
    is_teen_owner = "teen" in own_pkg_list
    is_family_owner = "family" in own_pkg_list

    cur.execute("SELECT pkg_code,pkg_name,buy_time,expire_time FROM user_package WHERE user_id=? ORDER BY buy_time DESC", (uid,))
    user_packages = cur.fetchall()
    cur.execute("SELECT id,input_text,check_type,fact_result,bias_result,fraud_type,risk_level,create_time FROM check_history WHERE user_id=? ORDER BY id DESC", (uid,))
    check_history = cur.fetchall()
    cur.execute("SELECT pkg_name,pay_price,pay_method,buy_time,expire_time FROM buy_order WHERE user_id=? ORDER BY buy_time DESC", (uid,))
    order_history = cur.fetchall()
    cur.execute("SELECT b.id,u.username,b.bind_type FROM user_bind b LEFT JOIN users u ON b.bind_user_id=u.id WHERE owner_user_id=?",(uid,))
    bind_list = cur.fetchall()
    cur.execute('''
        SELECT b.id, owner_u.username, b.bind_type
        FROM user_bind b
        LEFT JOIN users owner_u ON b.owner_user_id = owner_u.id
        WHERE b.bind_user_id=?
    ''',(uid,))
    be_bind_list = cur.fetchall()
    cur.execute("SELECT id,msg,is_read,create_time FROM user_notice WHERE receive_user_id=? ORDER BY id DESC",(uid,))
    notice_list = cur.fetchall()
    conn.close()
    return templates.TemplateResponse("user_center.html", {
        "request": request,
        "user": user,
        "user_packages": user_packages,
        "history": check_history,
        "order_list": order_history,
        "bind_list": bind_list,
        "be_bind_list": be_bind_list,
        "notice_list": notice_list,
        "err_msg": err_msg,
        "is_elder_owner": is_elder_owner,
        "is_teen_owner": is_teen_owner,
        "is_family_owner": is_family_owner
    })

@app.post("/api/unbind_account")
async def api_unbind_account(request: Request, bind_id:int=Form("")):
    sess_user = request.session.get("user")
    if not sess_user:
        return RedirectResponse("/login",303)
    uid = sess_user["id"]
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    cur.execute("SELECT owner_user_id FROM user_bind WHERE id=?",(bind_id,))
    row = cur.fetchone()
    if not row or row[0]!=uid:
        conn.close()
        return RedirectResponse("/user_center",303)
    cur.execute("DELETE FROM user_bind WHERE id=?",(bind_id,))
    conn.commit()
    conn.close()
    new_valid = get_user_valid_pkg_list(uid)
    sess_user["valid_pkgs"] = new_valid
    return RedirectResponse("/user_center",303)

@app.post("/api/add_family_bind")
async def add_family_bind(request: Request,
                          new_bind_username: str = Form(""),
                          bind_target_type: str = Form("")):
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login",303)
    uid = user["id"]
    new_bind_username = new_bind_username.strip()
    target_type = bind_target_type.strip()

    if target_type not in ("elder","teen","family"):
        return RedirectResponse("/user_center?err_msg=❌非法套餐类型", status_code=303)
    own_pkgs = get_own_pkg_list(uid)
    if target_type not in own_pkgs:
        return RedirectResponse("/user_center?err_msg=❌你不是该套餐的购买主账号，无权绑定账号", status_code=303)

    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username=?", (new_bind_username,))
    res = cur.fetchone()
    if not res:
        conn.close()
        return RedirectResponse("/user_center?err_msg=❌该用户名不存在！请核对用户名后重试", status_code=303)

    bind_uid = res[0]
    if bind_uid == uid:
        conn.close()
        return RedirectResponse("/user_center?err_msg=❌不能绑定自己账号！", status_code=303)

    cur.execute("SELECT bind_user_id FROM user_bind WHERE owner_user_id=? AND bind_type=?", (uid,target_type))
    binded_rows = cur.fetchall()
    binded_count = len(binded_rows)

    if target_type in ("elder","teen"):
        max_limit =1
    else:
        max_limit =5

    if binded_count >= max_limit:
        conn.close()
        return RedirectResponse(f"/user_center?err_msg=❌{target_type}套餐已达到绑定上限（最多{max_limit}人）！请先解绑旧账号", status_code=303)

    cur.execute("INSERT INTO user_bind(owner_user_id,bind_user_id,bind_type) VALUES (?,?,?)",
                (uid, bind_uid, target_type))
    conn.commit()
    conn.close()

    new_valid = get_user_valid_pkg_list(uid)
    request.session["user"]["valid_pkgs"] = new_valid
    return RedirectResponse("/user_center", status_code=303)

@app.post("/api/read_notice")
async def api_read_notice(request: Request, nid:int=Form("")):
    sess_user = request.session.get("user")
    if not sess_user:
        return RedirectResponse("/login",303)
    uid = sess_user["id"]
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    cur.execute("UPDATE user_notice SET is_read=1 WHERE id=? AND receive_user_id=?",(nid,uid))
    conn.commit()
    conn.close()
    return RedirectResponse("/user_center",303)

@app.post("/check")
async def check_text(request: Request, user_text: str = Form(...), check_type: str = Form(...)):
    sess_user = request.session.get("user")
    if not sess_user:
        return RedirectResponse("/login", 303)
    user_id = sess_user["id"]
    valid_pkgs = sess_user["valid_pkgs"]
    CHECK_TYPE_MAPPING = {
        "check_free":"free",
        "check_elder":"elder",
        "check_teen":"teen",
        "check_family":"family",
        "check_premium":["premium_month","premium_year"]
    }
    need_pkg = CHECK_TYPE_MAPPING[check_type]
    if isinstance(need_pkg, str):
        if need_pkg not in valid_pkgs:
            return RedirectResponse(f"/go_pay?pkg={need_pkg}",303)
        actual_pkg = need_pkg
    else:
        if not any(p in valid_pkgs for p in need_pkg):
            return RedirectResponse("/package",303)
        actual_pkg = "premium"

    fact_res, bias_res, fraud_type, risk_level = run_detect_logic(user_text, actual_pkg)
    money_val = parse_fraud_money(user_text)
    print("==========诈骗提醒调试==========")
    print(f"actual_pkg(当前套餐): {actual_pkg}")
    print(f"解析金额(元): {money_val}")
    print(f"fraud_type:{fraud_type}, risk_level:{risk_level}")
    print(f"AI返回fact_res片段：{fact_res[:300]}")
    if money_val >= 100000:
        print(">>>>>条件满足，准备写入提醒消息")
    else:
        print(">>>>>条件不满足，跳过提醒")
    print("==================================")

    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    if money_val >= 100000:
        generate_bind_notice(cur, user_id, actual_pkg, fact_res)

    save_bias = bias_res
    cur.execute('''INSERT INTO check_history(user_id,check_type,input_text,fact_result,bias_result,fraud_type,risk_level)
                   VALUES (?,?,?,?,?,?,?)''',(user_id,check_type,user_text,fact_res,save_bias,fraud_type,risk_level))
    conn.commit()
    conn.close()

    result_text = f"识别文本:\n{user_text}\n\n====事实核验结果====\n{fact_res}"
    if save_bias.strip()!="":
        result_text += f"\n\n====心理纠偏分析====\n{save_bias}"

    return templates.TemplateResponse("index.html",{
        "request":request,
        "user":sess_user,
        "result":result_text,
        "res_check_type": check_type,
        "res_input_text": user_text,
        "res_fact": fact_res,
        "res_bias": save_bias
    })

@app.post("/api/upload_audio")
async def api_upload_audio(request: Request, file: UploadFile = File(...), check_type: str = Form(...)):
    sess_user = request.session.get("user")
    if not sess_user:
        return {"code": 401, "msg": "请登录","redirect_url":"/login"}
    CHECK_TYPE_MAPPING = {
        "check_free":"free",
        "check_elder":"elder",
        "check_teen":"teen",
        "check_family":"family",
        "check_premium":["premium_month","premium_year"]
    }
    need_pkg = CHECK_TYPE_MAPPING[check_type]
    valid_pkg_list = sess_user["valid_pkgs"]
    actual_pkg = None
    if isinstance(need_pkg, str):
        if need_pkg not in valid_pkg_list:
            return {"code": 403, "msg": "无权限","redirect_url":f"/go_pay?pkg={need_pkg}"}
        actual_pkg = need_pkg
    else:
        if not any(p in valid_pkg_list for p in need_pkg):
            return {"code": 403, "msg": "无权限","redirect_url":"/package"}
        actual_pkg = "premium"

    with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as tmpf:
        tmpf.write(await file.read())
        tmp_path = tmpf.name
    try:
        trans = whisper_model.transcribe(tmp_path)
        text = trans["text"]
    finally:
        os.unlink(tmp_path)

    fact_res, bias_res, fraud_type, risk_level = run_detect_logic(text, actual_pkg)
    money_val = parse_fraud_money(text)
    save_bias = bias_res
    uid = sess_user["id"]
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    if money_val >=100000:
        generate_bind_notice(cur, uid, actual_pkg, fact_res)

    cur.execute("INSERT INTO check_history(user_id,check_type,input_text,fact_result,bias_result,fraud_type,risk_level) VALUES (?,?,?,?,?,?,?)",
                (uid, check_type, f"【语音转文字】{text}", fact_res, save_bias, fraud_type, risk_level))
    conn.commit()
    conn.close()
    return {"code": 0, "transcript": text, "fact": fact_res, "bias": save_bias}

@app.post("/api/upload_video")
async def api_upload_video(request: Request, file: UploadFile = File(...), check_type: str = Form(...)):
    sess_user = request.session.get("user")
    if not sess_user:
        return {"code": 401, "msg": "请登录","redirect_url":"/login"}
    CHECK_TYPE_MAPPING = {
        "check_free":"free",
        "check_elder":"elder",
        "check_teen":"teen",
        "check_family":"family",
        "check_premium":["premium_month","premium_year"]
    }
    need_pkg = CHECK_TYPE_MAPPING[check_type]
    valid_pkg_list = sess_user["valid_pkgs"]
    actual_pkg = None
    if isinstance(need_pkg, str):
        if need_pkg not in valid_pkg_list:
            return {"code": 403, "msg": "无权限","redirect_url":f"/go_pay?pkg={need_pkg}"}
        actual_pkg = need_pkg
    else:
        if not any(p in valid_pkg_list for p in need_pkg):
            return {"code": 403, "msg": "无权限","redirect_url":"/package"}
        actual_pkg = "premium"

    import ffmpeg
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp_video:
        tmp_video.write(await file.read())
        vid_path = tmp_video.name
    audio_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    audio_path = audio_tmp.name
    audio_tmp.close()
    try:
        (
            ffmpeg
            .input(vid_path)
            .output(audio_path, format='wav', acodec='pcm_s16le', vn=None)
            .overwrite_output()
            .run(quiet=True)
        )
        trans = whisper_model.transcribe(audio_path)
        text = trans["text"]
    finally:
        os.unlink(vid_path)
        os.unlink(audio_path)

    fact_res, bias_res, fraud_type, risk_level = run_detect_logic(text, actual_pkg)
    money_val = parse_fraud_money(text)
    save_bias = bias_res
    uid = sess_user["id"]
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    if money_val >=100000:
        generate_bind_notice(cur, uid, actual_pkg, fact_res)

    cur.execute("INSERT INTO check_history(user_id,check_type,input_text,fact_result,bias_result,fraud_type,risk_level) VALUES (?,?,?,?,?,?,?)",
                (uid, check_type, f"【视频音频转文字】{text}", fact_res, save_bias, fraud_type, risk_level))
    conn.commit()
    conn.close()
    return {"code": 0, "transcript": text, "fact": fact_res, "bias": save_bias}

@app.post("/api/upload_image")
async def api_upload_image(request: Request, file: UploadFile = File(...), check_type: str = Form(...)):
    sess_user = request.session.get("user")
    if not sess_user:
        return {"code": 401, "msg": "请登录","redirect_url":"/login"}
    CHECK_TYPE_MAPPING = {
        "check_free":"free",
        "check_elder":"elder",
        "check_teen":"teen",
        "check_family":"family",
        "check_premium":["premium_month","premium_year"]
    }
    need_pkg = CHECK_TYPE_MAPPING[check_type]
    valid_pkg_list = sess_user["valid_pkgs"]
    actual_pkg = None
    if isinstance(need_pkg, str):
        if need_pkg not in valid_pkg_list:
            return {"code": 403, "msg": "无权限","redirect_url":f"/go_pay?pkg={need_pkg}"}
        actual_pkg = need_pkg
    else:
        if not any(p in valid_pkg_list for p in need_pkg):
            return {"code": 403, "msg": "无权限","redirect_url":"/package"}
        actual_pkg = "premium"

    suffix = os.path.splitext(file.filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmpf:
        tmp_path = tmpf.name
        tmpf.write(await file.read())
    try:
        result, _ = ocr_reader(tmp_path)
        ocr_text = ""
        if result:
            ocr_text = "\n".join([item[1] for item in result])
        if not ocr_text.strip():
            ocr_text = "图片未识别到文字"
        fact_res, bias_res, fraud_type, risk_level = run_detect_logic(ocr_text, actual_pkg)
    finally:
        os.unlink(tmp_path)

    money_val = parse_fraud_money(ocr_text)
    save_bias = bias_res
    uid = sess_user["id"]
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    if money_val >=100000:
        generate_bind_notice(cur, uid, actual_pkg, fact_res)

    cur.execute("INSERT INTO check_history(user_id,check_type,input_text,fact_result,bias_result,fraud_type,risk_level) VALUES (?,?,?,?,?,?,?)",
                (uid, check_type, f"【图片OCR文字】{ocr_text}", fact_res, save_bias, fraud_type, risk_level))
    conn.commit()
    conn.close()
    return {"code": 0, "ocr_text": ocr_text, "fact": fact_res, "bias": save_bias}

# ====================== 人工客服模块开始 ======================
@app.post("/api/create_chat_session")
async def api_create_chat_session(
    request: Request,
    check_type: str = Form(...),
    origin_input: str = Form(""),
    media_text: str = Form(""),
    fact_result: str = Form(""),
    bias_result: str = Form(""),
    session_type:str=Form("normal")
):
    sess_user = request.session.get("user")
    if not sess_user:
        return {"code":401,"redirect":"/login"}
    uid = sess_user["id"]
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    cur.execute('''
    INSERT INTO chat_session(user_id,check_type,origin_input,ocr_or_audio_text,fact_result,bias_result,session_type)
    VALUES (?,?,?,?,?,?,?)
    ''',(uid,check_type,origin_input,media_text,fact_result,bias_result,session_type))
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"code":0,"session_id":sid}

@app.get("/user_chat", response_class=HTMLResponse)
async def user_chat_page(request: Request, session_id:int=Query(...)):
    sess_user = request.session.get("user")
    if not sess_user:
        return RedirectResponse("/login",303)
    uid = sess_user["id"]
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    cur.execute("SELECT * FROM chat_session WHERE id=? AND user_id=?",(session_id,uid))
    session_row = cur.fetchone()
    conn.close()
    if not session_row:
        return HTMLResponse("<h3>会话不存在或无权访问</h3>")
    return templates.TemplateResponse("user_chat.html",{
        "request":request,
        "session_id":session_id,
        "session_data":session_row
    })

@app.post("/api/user_send_msg")
async def api_user_send_msg(request: Request, session_id:int=Form(...), content:str=Form("")):
    sess_user = request.session.get("user")
    if not sess_user:
        return {"code":401}
    uid = sess_user["id"]
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    cur.execute("SELECT id FROM chat_session WHERE id=? AND user_id=?",(session_id,uid))
    if not cur.fetchone():
        conn.close()
        return {"code":-1,"msg":"会话非法"}
    cur.execute('''INSERT INTO chat_message(session_id,sender_type,content) VALUES (?,?,?)''',
                (session_id,"user",content.strip()))
    conn.commit()
    conn.close()
    return {"code":0}

@app.get("/api/get_chat_msg")
async def api_get_chat_msg(session_id:int=Query(...)):
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
    SELECT sender_type,content,create_time FROM chat_message WHERE session_id=? ORDER BY id ASC
    """,(session_id,))
    msgs = cur.fetchall()
    conn.close()
    return {"code":0,"messages":msgs}

@app.get("/admin_chat", response_class=HTMLResponse)
async def admin_chat_page(request: Request, select_session:int=Query(None)):
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    cur.execute('''
    SELECT s.id, u.username, s.check_type, s.create_time, s.session_type
    FROM chat_session s LEFT JOIN users u ON s.user_id = u.id
    ORDER BY s.id DESC
    ''')
    session_list = cur.fetchall()
    selected_session_data = None
    msg_list = []
    if select_session:
        cur.execute("SELECT * FROM chat_session WHERE id=?",(select_session,))
        selected_session_data = cur.fetchone()
        cur.execute("SELECT sender_type,content,create_time FROM chat_message WHERE session_id=? ORDER BY id ASC",(select_session,))
        msg_list = cur.fetchall()
    conn.close()
    return templates.TemplateResponse("admin_chat.html",{
        "request":request,
        "session_list":session_list,
        "select_session":select_session,
        "selected_session_data":selected_session_data,
        "msg_list":msg_list
    })

@app.post("/api/admin_send_msg")
async def api_admin_send_msg(session_id:int=Form(...), content:str=Form("")):
    conn = sqlite3.connect("user.db", check_same_thread=False)
    cur = conn.cursor()
    cur.execute('''INSERT INTO chat_message(session_id,sender_type,content) VALUES (?,?,?)''',
                (session_id,"admin",content.strip()))
    conn.commit()
    conn.close()
    return {"code":0}
# ======================人工客服模块结束 ======================

@app.post("/api/calc_campus_loan")
async def calc_campus_loan(request:Request,
                           principal:float=Form(...),
                           annual_rate:float=Form(...),
                           months:int=Form(...),
                           repay_type:str=Form(...)):
    user = request.session.get("user")
    if not user:
        return {"code":401,"msg":"请登录"}
    valid_pkgs = user["valid_pkgs"]
    if "teen" not in valid_pkgs:
        return {"code":403,"msg":"仅青少年守护套餐用户可使用校园贷计算器"}
    monthly_rate = annual_rate / 100 / 12
    total_pay = 0.0
    month_list=[]
    if repay_type == "one_time":
        total_interest = principal * (annual_rate/100) * (months/12)
        total_pay = principal + total_interest
        month_list.append({"month":months,"pay":round(total_pay,2)})
    elif repay_type == "equal_principal_interest":
        if monthly_rate==0:
            month_pay = principal/months
            total_pay=principal
        else:
            month_pay = principal * monthly_rate * pow(1+monthly_rate,months)/(pow(1+monthly_rate,months)-1)
            total_pay = month_pay*months
        for m in range(1,months+1):
            month_list.append({"month":m,"pay":round(month_pay,2)})
    elif repay_type == "equal_principal":
        month_principal = principal/months
        remain = principal
        for m in range(1,months+1):
            interest = remain*monthly_rate
            pay = month_principal+interest
            month_list.append({"month":m,"pay":round(pay,2)})
            remain -= month_principal
        total_pay = sum([item["pay"] for item in month_list])
    return {
        "code":0,
        "principal":principal,
        "annual_rate":annual_rate,
        "months":months,
        "repay_type":repay_type,
        "total_pay":round(total_pay,2),
        "total_interest":round(total_pay-principal,2),
        "detail":month_list
    }

@app.get("/teen_calc",response_class=HTMLResponse)
async def teen_calc_page(request:Request):
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login",303)
    if "teen" not in user["valid_pkgs"]:
        return HTMLResponse("<h3>该页面仅青少年守护套餐用户开放</h3>")
    return templates.TemplateResponse("teen_calc.html",{"request":request,"user":user})

# ==================青少年简易维权PDF =================
@app.post("/api/export_report_pdf")
async def export_report_pdf(request: Request,
                            evidence_list: str = Form(""),
                            case_money: str = Form(""),
                            fraud_account: str = Form(""),
                            timeline: str = Form(""),
                            origin_input: str = Form(""),
                            fact_result: str = Form("")):
    sess_user = request.session.get("user")
    if not sess_user:
        return RedirectResponse("/login", 303)

    import os
    import uuid
    pdf_filename = f"report_{uuid.uuid4().hex}.pdf"
    save_path = os.path.join("temp", pdf_filename)
    os.makedirs("temp", exist_ok=True)
    c = canvas.Canvas(save_path, pagesize=A4)
    width, height = A4

    c.setFont(font_name, 16)
    c.drawCentredString(width / 2, height - 50, "反诈维权举报材料")
    c.setFont(font_name, 11)
    y = height - 90
    line_height = 22

    def draw_wrap_text(text, x, max_width):
        nonlocal y
        char_list = list(text)
        line_buf = ""
        for ch in char_list:
            if c.stringWidth(line_buf + ch, font_name, 11) > max_width:
                if y < 50:
                    c.showPage()
                    y = height - 50
                    c.setFont(font_name, 11)
                c.drawString(x, y, line_buf)
                y -= line_height
                line_buf = ch
            else:
                line_buf += ch
        if line_buf:
            if y < 50:
                c.showPage()
                y = height - 50
                c.setFont(font_name, 11)
            c.drawString(x, y, line_buf)
            y -= line_height

    content_lines = [
        "核验时间：实时生成",
        "原始核验内容："
    ]
    for line in content_lines:
        if y < 50:
            c.showPage()
            y = height - 50
            c.setFont(font_name, 11)
        c.drawString(40, y, line)
        y -= line_height
    draw_wrap_text(origin_input, 60, width - 100)

    c.drawString(40, y, "核验结论：")
    y -= line_height
    draw_wrap_text(fact_result, 60, width - 80)

    extra_lines = [
        "",
        "==== 用户补充证据信息 ====",
        f"涉案金额：{case_money}",
        f"对方账号信息：{fraud_account}",
        "事件时间线描述："
    ]
    for line in extra_lines:
        if y < 50:
            c.showPage()
            y = height - 50
            c.setFont(font_name, 11)
        c.drawString(40, y, line)
        y -= line_height
    draw_wrap_text(timeline, 60, width - 100)

    if y < 50:
        c.showPage()
        y = height - 50
        c.setFont(font_name, 11)
    c.drawString(40, y, f"证据清单勾选内容：{evidence_list}")
    y -= line_height

    footer_lines = [
        "",
        "==== 建议维权渠道 ====",
        "1. 就近前往辖区派出所报警，携带全部聊天、转账证据材料",
        "2. 12321网络不良与垃圾信息举报受理中心官网提交线上举报",
        "3. 在校学生可前往学校学生处、保卫处提交维权材料求助",
        "",
        "声明：本维权材料由反诈核验系统辅助生成，仅作为整理线索参考，最终提交材料请以公安、学校官方要求为准。",
        "本 APP 信息仅供参考，不构成任何投资建议，据此操作风险自担。"
    ]
    for line in footer_lines:
        if y < 50:
            c.showPage()
            y = height - 50
            c.setFont(font_name, 11)
        c.drawString(40, y, line)
        y -= line_height

    c.save()
    return FileResponse(save_path, filename="反诈维权举报材料.pdf")

# ============进阶会员：导出正式完整报告PDF ==========
@app.post("/api/export_full_report")
async def export_full_report(request: Request, history_id:int=Form(...)):
    sess_user = request.session.get("user")
    if not sess_user:
        return RedirectResponse("/login",303)
    uid = sess_user['id']
    valid_pkgs = sess_user.get("valid_pkgs",[])
    is_premium = ("premium_month" in valid_pkgs) or ("premium_year" in valid_pkgs)
    if not is_premium:
        return RedirectResponse("/package",303)

    import os,uuid
    conn=sqlite3.connect("user.db", check_same_thread=False)
    cur=conn.cursor()
    cur.execute('''
        SELECT id,input_text,fact_result,bias_result,fraud_type,risk_level,create_time
        FROM check_history WHERE id=? AND user_id=?
    ''',(history_id,uid))
    row=cur.fetchone()
    conn.close()
    if not row:
        return {"code":-1,"msg":"记录不存在"}
    hid,inp_text,fact_res,bias_res,ftype,rlevel,ctime = row

    pdf_filename = f"full_report_{uuid.uuid4().hex}.pdf"
    save_path = os.path.join("temp",pdf_filename)
    os.makedirs("temp",exist_ok=True)
    c=canvas.Canvas(save_path,pagesize=A4)
    w,h=A4
    y=h-50
    line_h=24

    def wrap_text(txt,x,max_w):
        nonlocal y
        buf=""
        for ch in list(txt):
            if c.stringWidth(buf+ch,font_name,11)>max_w:
                if y<40:
                    c.showPage()
                    y=h-50
                    c.setFont(font_name,11)
                c.drawString(x,y,buf)
                y -= line_h
                buf=ch
            else:
                buf += ch
        if buf:
            if y<40:
                c.showPage()
                y=h-50
                c.setFont(font_name,11)
            c.drawString(x,y,buf)
            y -= line_h

    c.setFont(font_name,18)
    c.drawCentredString(w/2,y,"反诈风险正式核验报告")
    y -= 40
    c.setFont(font_name,11)
    info=[
        f"核验编号: {hid}",
        f"核验时间: {ctime}",
        f"诈骗类型标签: {ftype if ftype else '未识别'}",
        f"风险等级: {rlevel if rlevel else '未判定'}",
        "",
        "=== 用户原始核验内容 ==="
    ]
    for line in info:
        c.drawString(40,y,line)
        y -= line_h
    wrap_text(inp_text,60,w-100)
    c.drawString(40,y,"=== 事实核验分析结果 ===")
    y -= line_h
    wrap_text(fact_res,60,w-100)
    c.drawString(40,y,"=== 心理纠偏分析 ===")
    y -= line_h
    wrap_text(bias_res,60,w-100)
    y -= 30
    wrap_text("免责声明：本报告仅作为风险分析参考，不具备法律效应。本 APP 信息仅供参考，不构成任何投资建议，据此操作风险自担。",40,w-80)
    c.save()
    return FileResponse(save_path,filename=f"反诈正式报告_{ctime}.pdf")

# =========风险画像饼图统计接口【只统计fraud_type诈骗类型】 =========
@app.get("/api/user_risk_stat")
async def user_risk_stat(request:Request):
    sess_user = request.session.get("user")
    if not sess_user:
        return {"code":-1,"msg":"未登录"}
    valid_pkgs = sess_user.get("valid_pkgs",[])
    is_premium = ("premium_month" in valid_pkgs) or ("premium_year" in valid_pkgs)
    if not is_premium:
        return {"code":-2,"msg":"需要开通个人进阶会员"}
    uid = sess_user['id']
    conn=sqlite3.connect("user.db", check_same_thread=False)
    cur=conn.cursor()
    cur.execute('''SELECT fraud_type
                   FROM check_history
                   WHERE user_id=? AND fraud_type IS NOT NULL AND fraud_type != ''
                   ''',(uid,))
    rows = cur.fetchall()
    conn.close()
    cnt = Counter()
    for (ft,) in rows:
        cnt[ft] += 1
    labels = list(cnt.keys())
    data = list(cnt.values())
    return {"code":0,"labels":labels,"data":data}

if __name__ == "__main__":
    from starlette.middleware.sessions import SessionMiddleware
    app.add_middleware(SessionMiddleware, secret_key="demo‑secret‑key‑123456789")
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)