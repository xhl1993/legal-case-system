# -*- coding: utf-8 -*-
"""
案件处理系统 V5 - 火山引擎OCR + 批量处理 + 汇总表 + 人工纠错
OCR：火山引擎通用文字识别（支持PDF直传base64，免图片转换）
功能：
  流程1：应诉材料批处理（按子文件夹批量OCR→汇总→纠错→统一写入+归档）
  流程2：判决书/裁决书批处理（批量OCR→匹配→汇总→纠错→统一更新+归档）
配置：密钥/路径统一在脚本同目录 .env 中配置（参考 .env.example），不再硬编码
"""

import os
import sys
import json
import glob
import shutil
import stat
import re
import logging
import argparse
import urllib.request
import urllib.error
import urllib.parse
import hashlib
import hmac
import base64
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ======== ANSI颜色（Windows Terminal兼容） ========
if sys.platform == 'win32':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

R = '\033[91m'     # 红
G = '\033[92m'     # 绿
Y = '\033[93m'     # 黄
B = '\033[94m'     # 蓝
C = '\033[96m'     # 青
BOLD = '\033[1m'
DIM = '\033[2m'
RST = '\033[0m'

# ======== .env 加载（标准库实现，无第三方依赖） ========

def _load_env_file(path):
    """轻量 .env 加载器：逐行解析 KEY=VALUE，跳过注释/空行，支持引号包裹。
    已存在的环境变量优先（不覆盖），便于部署时用系统变量覆盖。"""
    if not path or not os.path.exists(path):
        return
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


# 脚本同目录下的 .env 优先加载
_load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# ======== 日志与运行模式 ========
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'case_system.log')
AUTO_MODE = False      # --auto：跳过所有人工确认，使用推荐默认值
CLI_FOLDER = ''        # --folder：覆盖默认处理文件夹
CLI_DATE = ''          # --date：覆盖收文日期（YYYYMMDD / YYYY-MM-DD）


def setup_logging():
    """初始化日志：写入脚本同目录 case_system.log（终端输出保留不变）。"""
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        encoding='utf-8',
        force=True,
    )


def parse_cli_args(argv=None):
    """命令行参数解析：
    --flow 1|2   直接指定流程，跳过菜单
    --folder X   覆盖默认处理文件夹
    --date 20260804  覆盖收文日期
    --auto       跳过所有人工确认（取推荐默认值），便于定时自动化
    """
    p = argparse.ArgumentParser(description='案件处理系统 V5（火山引擎OCR）')
    p.add_argument('--flow', choices=['1', '2', '6'], help='直接指定流程：1=应诉材料批处理，2=判决书/裁决书批处理，6=保全裁定收文归档')
    p.add_argument('--folder', help='覆盖默认处理文件夹路径')
    p.add_argument('--date', help='覆盖收文日期（YYYYMMDD 或 YYYY-MM-DD）')
    p.add_argument('--auto', action='store_true', help='自动模式：跳过所有人工确认')
    return p.parse_args(argv)


class OcrBusinessError(Exception):
    """火山引擎返回的业务错误（如配额/参数问题），重试无意义，不触发重试"""


def retry_call(fn, *args, retries=2, base_delay=2.0, desc='API调用',
               retry_on=(Exception,), no_retry_on=(), **kwargs):
    """带指数退避的重试：首次失败后最多重试 retries 次，间隔 base_delay * 2^n 秒。
    仅重试 retry_on 指定的异常类型；命中 no_retry_on 的异常立即抛出（业务错误重试无意义）。
    全部失败后抛出最后一次异常，调用方自行处理。"""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except retry_on as e:
            if no_retry_on and isinstance(e, no_retry_on):
                raise
            last_exc = e
            if attempt < retries:
                wait = base_delay * (2 ** attempt)
                print(f'  {Y}{desc}失败（第{attempt+1}次），{wait:.0f}s后重试: {e}{RST}')
                logging.warning('%s失败（第%d次）: %s', desc, attempt + 1, e)
                time.sleep(wait)
    raise last_exc


def check_credentials():
    """启动前校验关键凭据，缺失时明确提示（避免运行时才报奇怪的签名错误）"""
    missing = []
    if not VOLCENGINE_ACCESS_KEY_ID or not VOLCENGINE_SECRET_ACCESS_KEY:
        missing.append('火山引擎密钥 (VOLCENGINE_AK / VOLCENGINE_SK)')
    if not DEEPSEEK_API_KEY:
        missing.append('DeepSeek API Key (DEEPSEEK_API_KEY)')
    if missing:
        print(f'{R}缺少必要配置：{", ".join(missing)}{RST}')
        print(f'  请在脚本同目录的 .env 文件中配置（参考 .env.example），或设置对应环境变量。')
        logging.warning('缺少必要配置: %s', ', '.join(missing))
        return False
    return True


# ======== 配置区（支持 .env / 环境变量覆盖） ========

# 默认登录凭据（远程模式使用）
DEFAULT_LOGIN_USERNAME = os.getenv('LOGIN_USERNAME', 'admin')
DEFAULT_LOGIN_PASSWORD = os.getenv('LOGIN_PASSWORD', '')

# ======== 火山引擎OCR配置 ========
VOLCENGINE_ACCESS_KEY_ID = os.getenv('VOLCENGINE_AK', '')
VOLCENGINE_SECRET_ACCESS_KEY = os.getenv('VOLCENGINE_SK', '')
VOLCENGINE_REGION = os.getenv('VOLCENGINE_REGION', 'cn-north-1')
VOLCENGINE_SERVICE = 'cv'
VOLCENGINE_OCR_ENDPOINT = 'https://visual.volcengineapi.com'

DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
DEEPSEEK_BASE_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1/chat/completions')
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-v4-pro')
DEEPSEEK_MAX_RETRIES = int(os.getenv('DEEPSEEK_MAX_RETRIES', '2'))   # DeepSeek 调用失败重试次数（指数退避）

OCR_MAX_RETRIES = int(os.getenv('OCR_MAX_RETRIES', '2'))            # 火山引擎OCR请求失败重试次数（指数退避）

SERVER_URL = os.getenv('SERVER_URL', 'http://127.0.0.1:8899')

# 各流程默认处理文件夹（在 .env 中配置；未配置时运行中会交互输入）
CASE_FOLDER = os.getenv('CASE_FOLDER', '')
CASE_FOLDER_2 = os.getenv('CASE_FOLDER_2', '')
RESPONSE_FOLDER = os.getenv('RESPONSE_FOLDER', '')
JUDGMENT_FOLDER = os.getenv('JUDGMENT_FOLDER', '')
PRESERVE_FOLDER = os.getenv('PRESERVE_FOLDER', '')

OCR_MAX_IMAGE_SIZE = 4 * 1024 * 1024
OCR_QUALITY_STEP = 10
OCR_PARALLEL_WORKERS = 3        # OCR并发线程数
DEEPSEEK_PARALLEL_WORKERS = 2   # DeepSeek并发数（需注意API限流）

# ======== 列定义 ========

CASES_COLUMNS = [
    '案件序号', '案件状态', '收到案件的时间', '开庭时间', '诉讼请求',
    '诉讼/仲裁', '类型', '原告', '被告', '案由', '法院/仲裁委', '案号',
    '二审案号',
    '收到判决时间', '上诉时间截止日', '判决内容（一审、二审）', '二审判决内容',
    '涉及项目', '涉及项目编号', '保全（是/否）', '保全金额',
    '作为原告涉案诉讼金额(元)', '作为原告挽回（回款）损失金额（元）',
    '作为被告涉案诉讼金额(元)', '作为被告支付金额（元）',
    '自营/非自营', '项目负责人', '合作办理/自行办理', '律师团队',
    '律师代理费', '责任人及跟进人', '重新分配案件责任人&跟进人',
    '案件进展', '商票号码', '其它备注'
]

UNPAID_COLUMNS = [
    '登记日期', '案件序号', '案号', '最迟付款日期', '金额',
    '分批笔次', '涉及单位', '空', '备注-2026/5/26', '涉及项目',
    '项目负责人', '是否支付', '备注', '登记人', '备注2',
    '收款账号', '开户行'
]


# ====================================================================
# 数据层（远程 API 版，实现见同目录 db.py，文件末尾导入）
# ====================================================================

SESSION_TOKEN = None  # 远程模式登录后的session token（由 db.py 写入/读取）
CURRENT_USER = {}    # 当前登录用户信息（由 db.py 写入/读取）





def _row_get(r, col_name):
    """兼容 dict / sqlite3.Row / 远程API包装dict 三种数据源取值。
    远程API返回格式为 {'data': [...]}（data 是按 CASES_COLUMNS 顺序排列的数组）。
    相比原实现增加了 None 与 sqlite3.Row（无 .get 方法）的防御，避免类型问题崩溃。"""
    if r is None:
        return ''
    # 仅当 data 是 list 时才走远程API的数组分支（避免本地dict中恰好有'data'键时误走此分支）
    if isinstance(r, dict) and 'data' in r and isinstance(r['data'], list):
        try:
            idx = CASES_COLUMNS.index(col_name)
            return r['data'][idx] if idx < len(r['data']) else ''
        except (ValueError, IndexError):
            return ''
    if isinstance(r, dict):
        return r.get(col_name, '')
    # sqlite3.Row：支持 keys() 与下标访问，但没有 .get()
    if hasattr(r, 'keys') and hasattr(r, '__getitem__'):
        try:
            return r[col_name]
        except (KeyError, IndexError):
            return ''
    return ''


def _row_id(r):
    """取记录主键 id。
    注意：不能用 _row_get(r, 'id') —— 远程API行是 {'id':..., 'data':[...]}，
    走 _row_get 会去 CASES_COLUMNS 里找 'id' 列而抛 ValueError，返回空串导致匹配被误判失败。"""
    if isinstance(r, dict):
        return r.get('id')
    if hasattr(r, 'keys') and hasattr(r, '__getitem__'):
        try:
            return r['id']
        except (KeyError, IndexError):
            return None
    return None


def _stored_case_no_list(stored):
    """把数据库中存储的案号字段拆成单个案号列表（兼容 \n 与 \\n 两种分隔）。"""
    return [n for n in str(stored).replace('\\n', '\n').split('\n') if n.strip()]


def _case_no_matches(stored, target):
    """判断存储的案号字段中是否包含目标案号（规范化后比较）。"""
    norm = normalize_case_no(target)
    if not norm:
        return False
    return any(normalize_case_no(n) == norm for n in _stored_case_no_list(stored))


def _search_case_no_in_rows(rows, case_no, status_filter=None):
    norm = normalize_case_no(case_no)
    for r in rows:
        if status_filter:
            st = _row_get(r, '案件状态')
            if isinstance(status_filter, list):
                if st not in status_filter:
                    continue
            elif st != status_filter:
                continue
        if _case_no_matches(_row_get(r, '案号'), case_no):
            return _row_id(r), r
    return None, None






# ====================================================================
# 工具函数
# ====================================================================

def normalize_case_no(no):
    if not no:
        return ''
    no = re.sub(r'\s+', '', no)  # 去除所有空白字符
    return no.replace('\uff08', '(').replace('\uff09', ')')


def parse_amount(val):
    """把金额字符串解析为元（int）。
    支持逗号分隔、'万'/'万元'、'亿'/'亿元' 等单位换算。
    解析失败返回 0，避免金额被静默丢成空字符串。"""
    if val is None:
        return 0
    s = str(val).strip().replace(',', '').replace('，', '')
    if not s:
        return 0
    multiplier = 1
    if '亿' in s:
        multiplier = 100000000
        s = s.replace('亿', '')
    elif '万' in s:
        multiplier = 10000
        s = s.replace('万', '')
    m = re.search(r'\d+(?:\.\d+)?', s)
    if not m:
        return 0
    try:
        return int(round(float(m.group()) * multiplier))
    except ValueError:
        return 0


def to_bool(val):
    """规范化布尔值：True/False、'true'/'false'、'是'/'否'、1/0 等。
    DeepSeek 可能返回字符串 'false'，直接 `if val` 会误判为真。"""
    if isinstance(val, bool):
        return val
    s = str(val or '').strip().lower()
    return s in ('true', '1', 'yes', 'y', '是', '要', '有')


_DATE_FORMATS = ('%Y-%m-%d', '%Y%m%d', '%Y/%m/%d', '%Y年%m月%d日', '%Y.%m.%d')


def _parse_date_flexible(val):
    """解析常见日期格式，失败返回 None。兼容 '2026年6月15日'、'2026-06-15 09:30' 等。"""
    if not val:
        return None
    s = str(val).strip()
    # 去掉可能附带的时间部分
    s = re.sub(r'[ T]\d{1,2}:\d{2}(:\d{2})?.*$', '', s)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def unique_name(path):
    """生成不冲突的文件路径：文件已存在时追加 _2、_3... 后缀；本身不存在则原样返回。"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f'{base}_{i}{ext}'):
        i += 1
    return f'{base}_{i}{ext}'


# ====================================================================
# 标准取值（与2026-07方案一数据清洗后的取值口径一致，勿随意改动）
# ====================================================================

STD_LITIGATION_TYPES = [
    '诉讼·诉前调', '诉讼·一审', '诉讼·二审', '诉讼·再审', '诉讼·执行',
    '商事仲裁', '劳动仲裁',
    '协助执行', '协助调查', '破产清算', '非诉', '检察监督',
]

# 未结案状态集合（去重检查用）：除"已结案"外的所有在办状态
UNCLOSED_STATUSES = ['待开庭', '审理中', '已判决待上诉', '执行中', '排期']


def normalize_litigation_type(val, court=''):
    """把用户输入/旧取值映射为标准程序类型取值。
    court 用于区分 商事仲裁/劳动仲裁。"""
    v = str(val or '').strip()
    ct = str(court or '')
    if not v or v in ('诉讼', '一审'):
        return '诉讼·一审'
    if v in STD_LITIGATION_TYPES:
        return v
    if '仲裁' in v:
        return '劳动仲裁' if '劳动' in ct else '商事仲裁'
    if '诉前调' in v:
        return '诉讼·诉前调'
    if '再审' in v:
        return '诉讼·再审'
    if '执行' in v:
        return '诉讼·执行'
    if '二审' in v:
        return '诉讼·二审'
    if '一审' in v:
        return '诉讼·一审'
    return '诉讼·一审'


def is_arbitration(lit_type):
    """是否仲裁类案件（商事仲裁/劳动仲裁），用于确定地位称谓（申请人/被申请人）"""
    return '仲裁' in str(lit_type or '')


# ======== 辅助判断函数 ========

INSTANCE_SECOND_KW = ['民终', '终字', '商终', '行终']
INSTANCE_ARBITRATION_KW = ['仲裁', '仲案字', '劳仲', '裁字']


def _detect_instance_level(case_no):
    """通过案号关键词判断审级/程序：二审 / 仲裁 / 一审"""
    cn = str(case_no or '')
    if any(kw in cn for kw in INSTANCE_SECOND_KW):
        return '二审'
    if any(kw in cn for kw in INSTANCE_ARBITRATION_KW):
        return '仲裁'
    return '一审'


def _resolve_instance_level(case_no, doc_type=''):
    """综合案号与文书类型判断程序类型，返回 一审/二审/劳动仲裁/商事仲裁。
    doc_type 用于区分 劳动仲裁裁决书/商事仲裁裁决书。"""
    level = _detect_instance_level(case_no)
    if level == '仲裁':
        return '劳动仲裁' if '劳动' in str(doc_type or '') else '商事仲裁'
    return level


def _calc_appeal_deadline(judgment_date, instance_level=''):
    """计算上诉/起诉截止日：
    - 一审 / 劳动仲裁：+15天（不服判决上诉 / 不服劳动仲裁裁决向法院起诉）
    - 二审：判决日期本身（送达即生效）
    - 商事仲裁：不适用（撤销期为6个月，需人工处理），返回空
    日期格式做了多格式容错。"""
    if not judgment_date:
        return ''
    jd_dt = _parse_date_flexible(judgment_date)
    if not jd_dt:
        return ''
    if instance_level in ('一审', '劳动仲裁'):
        return (jd_dt + timedelta(days=15)).strftime('%Y-%m-%d')
    elif instance_level == '二审':
        return str(judgment_date)
    return ''


def _find_case_by_seq(case_seq, conn=None):
    """按案件序号在数据库中查找案件记录（远程 API 版）。返回 (db_id, row_dict) 或 (None, None)
    conn 参数仅为兼容旧调用保留，已不再使用。"""
    if not case_seq:
        return None, None
    try:
        rows = _api_get_all('案件情况表')
        found = next((r for r in rows if str(_row_get(r, '案件序号')) == str(case_seq)), None)
        if found:
            return found.get('id'), found
    except Exception:
        pass
    return None, None


def new_case_status(court_date_str):
    """新登记案件的状态：开庭时间在未来 → 待开庭；无开庭时间或已过 → 审理中"""
    s = str(court_date_str or '').strip().split(' ')[0]
    try:
        d = datetime.strptime(s, '%Y-%m-%d').date()
        if d >= datetime.now().date():
            return '待开庭'
    except ValueError:
        pass
    return '审理中'



def confirm_input(prompt, default=''):
    """输入确认；AUTO 模式直接返回默认值（--auto 时用于自动化运行）。"""
    if AUTO_MODE:
        return default
    try:
        if default:
            val = input(f'{prompt}（默认 {default}，直接回车确认）：').strip()
            return val if val else default
        return input(f'{prompt}：').strip()
    except (EOFError, KeyboardInterrupt):
        return default


def yes_no(prompt, default=True):
    """交互确认；AUTO 模式直接返回默认值。"""
    if AUTO_MODE:
        return default
    hint = 'Y/n' if default else 'y/N'
    try:
        val = input(f'{prompt}（{hint}）：').strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    if val == '':
        return default
    return val in ('y', '是', 'yes', '1')


def smart_date_input(prompt, default=''):
    """日期输入（多格式容错）；AUTO 模式优先用 --date 覆盖值，其次默认值。"""
    if AUTO_MODE:
        return CLI_DATE or default
    display_default = default.replace('-', '') if default else ''
    hint = f'（默认 {display_default}，直接回车确认）' if display_default else '（YYYYMMDD或YYYY-MM-DD）'
    while True:
        try:
            raw = input(f'{prompt}{hint}：').strip()
        except (EOFError, KeyboardInterrupt):
            return default
        if not raw:
            return default
        dt = _parse_date_flexible(raw)
        if dt is not None:
            return dt.strftime('%Y-%m-%d')
        print('  日期格式不支持，请使用YYYYMMDD或YYYY-MM-DD格式')


def cleanup_temp(tmp_dir):
    """删除临时目录，Windows下重试3次防文件锁"""
    if not tmp_dir or not os.path.exists(tmp_dir):
        return
    for attempt in range(3):
        try:
            shutil.rmtree(tmp_dir)
            return
        except PermissionError:
            if attempt < 2:
                time.sleep(0.5)
            else:
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass
                    pass


def find_case_folder(case_id):
    def _search(folder):
        if not folder or not os.path.exists(folder):
            return None
        for name in os.listdir(folder):
            full = os.path.join(folder, name)
            if os.path.isdir(full) and name.startswith(str(case_id)):
                return full
        return None
    result = _search(CASE_FOLDER)
    if result:
        return result
    return _search(CASE_FOLDER_2)


def safe_move(src, dst, on_conflict='ask'):
    """安全移动文件，自动处理只读文件（Windows）。
    on_conflict: 'ask'=目标已存在时询问用户；'auto'=自动追加序号；'overwrite'=直接覆盖。
    返回 True 表示已移动，False 表示跳过（用户选择跳过）。"""
    if not os.path.exists(src):
        return False
    if os.path.exists(dst):
        if on_conflict == 'overwrite':
            pass
        elif on_conflict == 'auto':
            dst = unique_name(dst)
        else:
            print(f'  {Y}⚠ 目标文件已存在: {os.path.basename(dst)}{RST}')
            act = input(f'    [1]覆盖 [2]自动改名 [3]跳过（默认 2）：').strip()
            if act == '1':
                pass
            elif act == '3':
                return False
            else:
                dst = unique_name(dst)
    # 去除只读属性（Windows read-only flag）
    if sys.platform == 'win32':
        os.chmod(src, stat.S_IWRITE)
    shutil.move(src, dst)
    return True


def archive_file(pdf_path, case_id, receive_date, doc_type_label):
    if not case_id:
        unsorted_dir = os.path.join(CASE_FOLDER, '未分类')
        os.makedirs(unsorted_dir, exist_ok=True)
        date_prefix = receive_date.replace('-', '')
        new_name = f'{date_prefix} {doc_type_label}{os.path.splitext(pdf_path)[1]}'
        if safe_move(pdf_path, os.path.join(unsorted_dir, new_name)):
            print(f'  已归档到未分类：{new_name}')
        return
    target_dir = find_case_folder(case_id)
    if not target_dir:
        target_dir = os.path.join(CASE_FOLDER, str(case_id))
        os.makedirs(target_dir, exist_ok=True)
    date_prefix = receive_date.replace('-', '')
    new_name = f'{date_prefix} {doc_type_label}{os.path.splitext(pdf_path)[1]}'
    new_path = os.path.join(target_dir, new_name)
    if os.path.exists(pdf_path) and safe_move(pdf_path, new_path):
        print(f'  已归档：{new_name} → {os.path.basename(target_dir)}/')


# ====================================================================
# 图片压缩
# ====================================================================

def compress_to_size(img_path, max_size=OCR_MAX_IMAGE_SIZE):
    if os.path.getsize(img_path) <= max_size:
        return
    from PIL import Image
    img = Image.open(img_path)
    orig = img.copy()  # 保留原始像素，避免多次有损压缩叠加导致画质急剧劣化
    # JPEG 不支持透明通道，统一转 RGB
    if orig.mode in ('RGBA', 'LA', 'P'):
        orig = orig.convert('RGB')
    quality = 90
    while quality >= 20:
        orig.save(img_path, 'JPEG', quality=quality)
        if os.path.getsize(img_path) <= max_size:
            return
        quality -= OCR_QUALITY_STEP
    w, h = orig.size
    ratio = 0.7
    while ratio >= 0.3:
        new_img = orig.resize((int(w * ratio), int(h * ratio)), Image.Resampling.LANCZOS)
        new_img.save(img_path, 'JPEG', quality=70)
        if os.path.getsize(img_path) <= max_size:
            return
        ratio -= 0.1


# ====================================================================
# PDF转图片
# ====================================================================

_PDF_BACKEND = None
try:
    import fitz
    _PDF_BACKEND = 'fitz'
except Exception:
    try:
        from pdf2image import convert_from_path
        _PDF_BACKEND = 'pdf2image'
    except ImportError:
        _PDF_BACKEND = None


def pdf_to_images(pdf_path, mode='smart'):
    if _PDF_BACKEND == 'fitz':
        return _pdf_to_images_fitz(pdf_path, mode)
    elif _PDF_BACKEND == 'pdf2image':
        return _pdf_to_images_pdf2image(pdf_path, mode)
    else:
        raise ImportError('未找到PDF处理库，请安装：pip install PyMuPDF')


def _pdf_to_images_fitz(pdf_path, mode='smart'):
    doc = fitz.open(pdf_path)
    total = len(doc)
    images = []
    tmp_dir = os.path.join(os.path.dirname(pdf_path), f'_ocr_tmp_{os.path.splitext(os.path.basename(pdf_path))[0]}')
    os.makedirs(tmp_dir, exist_ok=True)
    if mode == 'smart':
        page_list = list(range(total)) if total <= 4 else sorted(set(list(range(2)) + list(range(max(0, total - 2), total))))
    elif isinstance(mode, list):
        page_list = [p for p in mode if p < total]
    else:
        page_list = list(range(total))
    for page_num in sorted(page_list):
        page = doc[page_num]
        pix = page.get_pixmap(dpi=200)  # V4: 降DPI提速
        img_path = os.path.join(tmp_dir, f'page_{page_num + 1}.jpg')
        pix.save(img_path)
        compress_to_size(img_path)
        images.append(img_path)
    doc.close()
    return images, tmp_dir


def _pdf_to_images_pdf2image(pdf_path, mode='smart'):
    from pdf2image import convert_from_path
    tmp_dir = os.path.join(os.path.dirname(pdf_path), f'_ocr_tmp_{os.path.splitext(os.path.basename(pdf_path))[0]}')
    os.makedirs(tmp_dir, exist_ok=True)
    # 一次渲染全部页面（dpi=200），避免原先"低清数页 + 逐页重渲"的重复解析，大幅提速
    all_pages = convert_from_path(pdf_path, dpi=200, fmt='jpeg')
    total = len(all_pages)
    if mode == 'smart':
        page_list = list(range(total)) if total <= 4 else sorted(set(list(range(2)) + list(range(max(0, total - 2), total))))
    elif isinstance(mode, list):
        page_list = [p for p in mode if p < total]
    else:
        page_list = list(range(total))
    images = []
    for page_num in sorted(page_list):
        img_path = os.path.join(tmp_dir, f'page_{page_num + 1}.jpg')
        all_pages[page_num].save(img_path, 'JPEG', quality=95)
        compress_to_size(img_path)
        images.append(img_path)
    return images, tmp_dir


def get_pdf_page_count(pdf_path):
    if _PDF_BACKEND == 'fitz':
        doc = fitz.open(pdf_path)
        n = len(doc)
        doc.close()
        return n
    else:
        from pdf2image import convert_from_path
        return len(convert_from_path(pdf_path, dpi=1, fmt='png'))


# ====================================================================
# 火山引擎OCR（HMAC-SHA256签名 + PDF直传）
# ====================================================================

def _volcengine_sign(method, query, body, content_type, x_date):
    """
    火山引擎 HMAC-SHA256 签名（类似AWS SigV4）
    返回 Authorization header 值
    """
    host = 'visual.volcengineapi.com'
    signed_headers = 'content-type;host;x-date'

    # Step 1: Canonical Request
    canonical_headers = f'content-type:{content_type}\nhost:{host}\nx-date:{x_date}\n'
    body_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
    canonical_request = f'{method}\n/\n{query}\n{canonical_headers}\n{signed_headers}\n{body_hash}'

    # Step 2: String to Sign
    short_date = x_date[:8]
    credential_scope = f'{short_date}/{VOLCENGINE_REGION}/{VOLCENGINE_SERVICE}/request'
    string_to_sign = f'HMAC-SHA256\n{x_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()}'

    # Step 3: Signing Key
    secret = VOLCENGINE_SECRET_ACCESS_KEY.encode('utf-8')
    k_date = hmac.new(secret, short_date.encode('utf-8'), hashlib.sha256).digest()
    k_region = hmac.new(k_date, VOLCENGINE_REGION.encode('utf-8'), hashlib.sha256).digest()
    k_service = hmac.new(k_region, VOLCENGINE_SERVICE.encode('utf-8'), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b'request', hashlib.sha256).digest()

    # Step 4: Signature
    signature = hmac.new(k_signing, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()

    # Step 5: Authorization header
    return f'HMAC-SHA256 Credential={VOLCENGINE_ACCESS_KEY_ID}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}'


def _volcengine_ocr(image_base64_data):
    """
    调用火山引擎通用文字识别API（带指数退避重试）。
    image_base64_data: 图片或PDF文件的base64编码字符串
    返回: 识别文本
    网络错误/5xx 自动重试；业务错误（code!=10000）不重试（重试无意义）。
    """
    def _do_ocr():
        x_date = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        query = 'Action=OCRNormal&Version=2020-08-26'
        body = 'image_base64=' + urllib.parse.quote(image_base64_data)
        auth = _volcengine_sign('POST', query, body, 'application/x-www-form-urlencoded', x_date)
        url = f'{VOLCENGINE_OCR_ENDPOINT}/?{query}'
        req = urllib.request.Request(url, data=body.encode('utf-8'))
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        req.add_header('X-Date', x_date)
        req.add_header('Authorization', auth)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8', errors='replace')
            # 5xx 交给重试；4xx 重试无意义
            if e.code >= 500:
                raise Exception(f'火山引擎HTTP {e.code}: {error_body}')
            raise OcrBusinessError(f'火山引擎HTTP {e.code}: {error_body}')
        except Exception as e:
            raise Exception(f'火山引擎请求失败: {e}')
        if result.get('code') == 10000:
            lines = result.get('data', {}).get('line_texts', [])
            return '\n'.join(lines)
        msg = result.get('message', 'unknown')
        code = result.get('code', '?')
        raise OcrBusinessError(f'火山引擎OCR错误: {msg} (code={code})')

    return retry_call(_do_ocr, retries=OCR_MAX_RETRIES, desc='火山引擎OCR',
                      no_retry_on=(OcrBusinessError,))


def _volcengine_ocr_pdf_direct(pdf_path):
    """
    PDF直接OCR：读取PDF文件做base64编码，直接发送给火山引擎。
    仅适用于单页PDF（火山引擎对多页PDF只识别第一页）。
    返回: 识别文本
    """
    with open(pdf_path, 'rb') as f:
        pdf_data = base64.b64encode(f.read()).decode('ascii')
    return _volcengine_ocr(pdf_data)


def ocr_images_clean(images, _token_unused=None):
    """
    火山引擎版OCR：逐张图片base64编码后调用API
    保持与百度版相同接口以兼容现有调用
    images: 图片文件路径列表
    返回: 合并文本
    """
    all_text = []
    for i, img_path in enumerate(images):
        with open(img_path, 'rb') as f:
            img_b64 = base64.b64encode(f.read()).decode('ascii')
        try:
            text = _volcengine_ocr(img_b64)
            all_text.append(text)
        except Exception as e:
            print(f'  {Y}第{i+1}页OCR失败: {e}{RST}')
            all_text.append('')
    full_text = '\n'.join(all_text)
    full_text = re.sub(r'[ \t]+', ' ', full_text)
    full_text = re.sub(r'\n\s*\n', '\n', full_text)
    full_text = full_text.strip()
    return full_text


def _split_pdf_to_single_pages(pdf_path, tmp_dir):
    """
    将多页PDF拆分为单页PDF文件，存入tmp_dir。
    返回单页PDF文件路径列表。
    """
    if _PDF_BACKEND != 'fitz':
        # fitz不可用时回退图片模式
        return None
    doc = fitz.open(pdf_path)
    pages = []
    try:
        for i in range(len(doc)):
            single = fitz.open()
            single.insert_pdf(doc, from_page=i, to_page=i)
            page_path = os.path.join(tmp_dir, f'page_{i+1}.pdf')
            single.save(page_path)
            single.close()
            pages.append(page_path)
    finally:
        doc.close()
    return pages


def ocr_pdf_safe(pdf_path, mode='all'):
    """
    OCR安全包装，自动选择最优方式：
    - 单页PDF → PDF直传 → 失败回退图片模式
    - 多页PDF → 拆分为单页PDF直传（快）→ 全部失败回退图片模式
    """
    try:
        total_pages = get_pdf_page_count(pdf_path)
    except Exception as e:
        print(f'  {R}OCR失败 {os.path.basename(pdf_path)}: 无法打开PDF - {e}{RST}')
        return ''

    def _fallback_images():
        """图片模式回退：渲染→OCR→清理临时目录，返回文本"""
        try:
            images, img_tmp = pdf_to_images(pdf_path, mode=mode)
            text = ocr_images_clean(images)
            cleanup_temp(img_tmp)   # 图片回退产生的临时目录也要清理，避免泄漏
            return text
        except Exception as e2:
            print(f'  {R}OCR失败 {os.path.basename(pdf_path)}: 图片回退失败 - {e2}{RST}')
            logging.warning('OCR图片回退失败 %s: %s', os.path.basename(pdf_path), e2)
            return ''

    # 单页PDF → 直传；失败直接回退图片模式
    if total_pages == 1:
        try:
            text = _volcengine_ocr_pdf_direct(pdf_path)
            if text:
                return text
        except Exception as e:
            print(f'  {Y}PDF直传失败: {e}{RST}')
            logging.warning('单页PDF直传失败 %s: %s', os.path.basename(pdf_path), e)
        return _fallback_images()

    # 多页PDF → 拆分直传（首选，快）
    tmp_dir = os.path.join(os.path.dirname(pdf_path), f'_ocr_tmp_{os.path.splitext(os.path.basename(pdf_path))[0]}')
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        single_pages = _split_pdf_to_single_pages(pdf_path, tmp_dir)
        if single_pages is not None:
            texts = []
            fail_count = 0
            for page_path in single_pages:
                try:
                    t = _volcengine_ocr_pdf_direct(page_path)
                    if not t:
                        fail_count += 1
                    texts.append(t)
                except Exception as e:
                    fail_count += 1
                    print(f'  {Y}第{len(texts)+1}页PDF直传失败: {e}{RST}')
                    texts.append('')
            # 全部页面都失败 → 回退图片模式（避免返回非空的 '\n' 让上层误判 OCR 成功）
            if fail_count == len(texts):
                print(f'  {Y}拆分直传全部失败，回退图片模式{RST}')
                logging.warning('拆分直传全部失败，回退图片模式 %s', os.path.basename(pdf_path))
                return _fallback_images()
            return '\n'.join(texts)
        # fitz 不可用时直接图片模式
        return _fallback_images()
    except Exception as e:
        print(f'  {R}OCR失败 {os.path.basename(pdf_path)}: {e}{RST}')
        logging.warning('OCR失败 %s: %s', os.path.basename(pdf_path), e)
        return _fallback_images()
    finally:
        cleanup_temp(tmp_dir)


# ====================================================================
# DeepSeek API
# ====================================================================

def call_deepseek(system_prompt, user_text):
    """调用 DeepSeek 提取结构化信息，返回 dict。
    网络/超时类异常自动指数退避重试（DEEPSEEK_MAX_RETRIES 次）；
    API 业务错误（返回 error 字段）不重试。"""
    import requests
    headers = {'Authorization': f'Bearer {DEEPSEEK_API_KEY}', 'Content-Type': 'application/json'}
    payload = {
        'model': DEEPSEEK_MODEL,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_text}
        ],
        'temperature': 0.1,
        'max_tokens': 4000
    }

    def _do():
        resp = requests.post(DEEPSEEK_BASE_URL, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()  # 4xx/5xx 抛 HTTPError（属于 RequestException，可重试）
        result = resp.json()
        if 'error' in result:
            raise Exception(f'DeepSeek API错误：{result["error"]}')
        content = result['choices'][0]['message']['content'].strip()
        if content.startswith('```'):
            lines = content.split('\n')
            content = '\n'.join(lines[1:]) if len(lines) > 1 else content[3:]
            if content.rstrip().endswith('```'):
                content = content.rstrip()[:-3]
            content = content.strip()
        start = content.find('{')
        end = content.rfind('}')
        if start >= 0 and end > start:
            content = content[start:end+1]
        return json.loads(content)

    return retry_call(_do, retries=DEEPSEEK_MAX_RETRIES, desc='DeepSeek',
                      retry_on=(requests.RequestException,))


def _extract_batch(texts_by_pdf, prompt, entry):
    """并行调用 DeepSeek 提取结构化信息，返回 {pdf: info_dict}。
    失败的条目自动写入 entry['notes']，避免漏记。"""
    infos = {}
    if not texts_by_pdf:
        return infos
    with ThreadPoolExecutor(max_workers=DEEPSEEK_PARALLEL_WORKERS) as executor:
        futures = {executor.submit(call_deepseek, prompt, text): pdf
                   for pdf, text in texts_by_pdf.items()}
        for future in as_completed(futures):
            pdf = futures[future]
            try:
                infos[pdf] = future.result()
                print(f'  DeepSeek提取成功: {os.path.basename(pdf)}')
            except Exception as e:
                entry['notes'].append(f'DeepSeek失败: {os.path.basename(pdf)}')
                print(f'  {R}DeepSeek失败 {os.path.basename(pdf)}: {e}{RST}')
                logging.warning('DeepSeek提取失败 %s: %s', os.path.basename(pdf), e)
    return infos


# ====================================================================
# 文件类型识别
# ====================================================================





# ====================================================================
# DeepSeek提示词
# ====================================================================

PROMPT_COMPLAINT = """你是法律文书信息提取助手。请从以下OCR识别的法律文书文本中提取结构化信息。
严格以JSON格式返回，不要有任何其他文字。

返回格式：
{"plaintiff": "原告或申请人名称（完整名称）", "defendant": "被告或被申请人名称（完整名称）", "claim": "诉讼或仲裁请求的完整内容", "amount": "请求金额合计（纯数字，单位元。如无法提取请返回null）", "court": "受理法院或仲裁委员会名称", "caseNo": "案号（如：2026粤0305民初123号）", "cause": "案由", "keywords": "涉及的项目或工程名称（多个用逗号分隔。如：XX大厦工程、XX项目）", "commercialDraftNo": "商票号码（票据号码，不是项目名。提取不到返回空字符串）"}

注意：
1. 原告/被告可能是公司名称或个人姓名，要完整提取
2. 金额只提取数字，不带"元""万元"等单位文字
3. 如果某个字段提取不到，返回null
4. keywords 从诉状事实与理由中提取涉及的项目/工程名称，多个用逗号分隔。不含商票号
5. commercialDraftNo 专门提取商票/承兑汇票的票号"""

PROMPT_SUBPOENA = """你是法律文书信息提取助手。请从以下OCR识别的法院传票文本中提取结构化信息。
严格以JSON格式返回，不要有任何其他文字。

返回格式：
{"courtDate": "开庭时间（格式如：2026-06-15 09:30）", "court": "受理法院名称（完整名称）", "caseNo": "案号", "cause": "案由"}

注意：
1. 开庭时间保留到时分，格式YYYY-MM-DD HH:MM（如传票上只有日期无具体时间，则只返回日期YYYY-MM-DD）
2. 如果某个字段提取不到，返回null"""

PROMPT_JUDGMENT = """你是法律文书信息提取助手。请从以下OCR识别的判决书/裁决书文本中提取结构化信息。
文本可能只包含部分页面（前几页和后几页），请根据已有信息提取。

严格以JSON格式返回，不要有任何其他文字。

返回格式：
{"docType": "文件类型（判决书/商事仲裁裁决书/劳动仲裁裁决书/裁定书）", "caseNo": "案号或裁决号", "court": "法院或仲裁委员会名称", "plaintiff": "原告或申请人", "defendant": "被告或被申请人", "judgmentDate": "判决或裁决日期(YYYY-MM-DD)", "judgmentResult": "判决或裁决结果摘要", "needPayment": true或false, "paymentAmount": "需支付金额（纯数字，单位元）"}

注意：
1. 案号/裁决号是关键标识，通常格式为：（年份）省份法院类型字第XXX号
2. 如果文本只有前面几页，案号通常在第一页
3. 如果文本只有后面几页，重点关注判决结果和金额
4. 如果某个字段提取不到，返回null"""


PROMPT_PRESERVE = """你是法律文书信息提取助手。请从以下OCR识别的法院/仲裁机构文书文本中提取结构化信息。
文本通常是财产保全裁定书（财保号）或执行保全裁定书（执保号）、查封扣押冻结财产通知书等保全类文书，也可能是其他类型文书。
严格以JSON格式返回，不要有任何其他文字。

返回格式：
{"docType": "文书类型（保全裁定书/查封冻结通知书/判决书/其他）", "caseNo": "本案案号（诉讼案号如(2026)粤0303民初21942号，或仲裁案号如(2026)深国仲受5628号；没有则null）", "preserveNo": "保全案号（财保号或执保号，如(2026)粤0303财保413号、(2026)粤0303执保13769号；有多个用空格分隔；没有则null）", "court": "法院或仲裁机构名称", "plaintiff": "申请人（原告）全称", "defendant": "被申请人（被告）全称", "cause": "案由", "preserveAmount": "保全金额（纯数字，单位元）", "preserveDeadline": "保全到期日(YYYY-MM-DD)", "judge": "审判员/仲裁员姓名", "judgeContact": "法官/书记员/助理/联系人及其联系方式（如：黄助理25038249；没有则null）"}

注意：
1. caseNo 是本案的诉讼/仲裁案号，用于在案件库中定位案件；preserveNo 是保全程序自己的案号（财保/执保开头），两者不同
2. 查封、扣押、冻结财产通知书末尾可能标注【查封文号：（2026）粤0303执保XXXX号】，这属于执保号，也要提取进 preserveNo
3. 财保裁定书正文中通常写明源案号（如仲裁案号(2026)深国仲受5628号或民初案号），提取为 caseNo
4. 保全金额以"以人民币XXX元为限"或"价值人民币XXX元"为准
5. 如果某个字段提取不到，返回null"""


# ====================================================================
# 批量处理框架
# ====================================================================

def _truncate(s, width):
    """截断字符串到指定宽度（中文算2宽）"""
    s = str(s or '')
    width_count = 0
    result = []
    for ch in s:
        w = 2 if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f' or '\uff00' <= ch <= '\uffef' else 1
        if width_count + w > width - 1:
            result.append('…')
            break
        result.append(ch)
        width_count += w
    return ''.join(result)


def _pad_str(s, width, align='<'):
    """填充字符串到指定显示宽度"""
    s = str(s or '')
    w = sum(2 if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f' or '\uff00' <= ch <= '\uffef' else 1 for ch in s)
    pad = max(0, width - w)
    if align == '<':
        return s + ' ' * pad
    elif align == '>':
        return ' ' * pad + s
    else:
        left = pad // 2
        return ' ' * left + s + ' ' * (pad - left)


def print_summary_table(entries, columns, title='批量处理结果汇总', notes_width=24):
    """
    打印汇总表。
    columns: [(key, label, width), ...]
    entries: [dict, ...]  每项需包含 key 对应的值 以及 'status', 'notes'
    """
    status_icons = {
        'ok': f'{G}✅{RST}',
        'warning': f'{Y}⚠️{RST}',
        'error': f'{R}❌{RST}',
        'duplicate': f'{C}🔄{RST}',
    }

    # 构建表头
    headers = ['状态'] + [col[1] for col in columns] + ['备注']
    col_widths = [4] + [col[2] for col in columns] + [notes_width]

    def _make_sep(left, mid, right, fill='═'):
        parts = [left]
        for i, w in enumerate(col_widths):
            parts.append(fill * (w + 2))
            parts.append(mid if i < len(col_widths) - 1 else right)
        return ''.join(parts)

    # 顶部
    print()
    width_total = sum(col_widths) + len(col_widths) * 3 + 2
    print(f'  ┌{"─" * (width_total - 4)}┐')
    print(f'  │ {title.center(width_total - 4)} │')
    print(f'  ├{"─" * (width_total - 4)}┤')

    # 表头
    header_row = '  │'
    for i, h in enumerate(headers):
        header_row += f' {_pad_str(h, col_widths[i], "^")} │'
    print(header_row)
    print(_make_sep('  ├', '┼', '┤'))

    # 数据行
    for idx, entry in enumerate(entries):
        icon = status_icons.get(entry.get('status', 'warning'), f'{Y}?{RST}')
        row_str = f'  │ {icon} '
        remaining = col_widths[0] - 2  # icon算2宽

        for i, (key, label, width) in enumerate(columns):
            val = _truncate(entry.get(key, ''), width)
            row_str += f'│ {_pad_str(val, width)} '

        notes = _truncate('; '.join(entry.get('notes', [])), notes_width)
        row_str += f'│ {_pad_str(notes, notes_width)} │'
        print(row_str)

    # 底部
    print(_make_sep('  └', '┴', '┘'))

    # 统计
    counts = {}
    for e in entries:
        s = e.get('status', 'warning')
        counts[s] = counts.get(s, 0) + 1
    parts = []
    if 'ok' in counts:
        parts.append(f'{G}✅{counts["ok"]}{RST}')
    if 'warning' in counts:
        parts.append(f'{Y}⚠️{counts["warning"]}{RST}')
    if 'error' in counts:
        parts.append(f'{R}❌{counts["error"]}{RST}')
    if 'duplicate' in counts:
        parts.append(f'{C}🔄{counts["duplicate"]}{RST}')
    print(f'  共 {len(entries)} 项：{"  ".join(parts)}')
    print()


def interact_correct(entries, fields_config, prefix='序号'):
    """
    交互式纠错 — 一次性展示全部字段，直观批量修改。
    fields_config: [(key, label, extra_choices), ...]  按顺序显示
    entries: 会被原地修改

    操作方式：
      - 输入 "2=值,5=值" 修改第2和第5个字段
      - 输入 "2= 3=值"  仅修改第3个字段（2= 后面为空=不修改）
      - 输入 "2=" 清空第2个字段
      - 直接回车跳过当前记录
      - 输入 "!done" 结束全部纠错
      - 输入 "!ocr N" 用外部OCR文本重新识别第N条
    """
    while True:
        try:
            choice = input(f'\n输入要修改的{prefix}（逗号分隔多个，回车跳过，!done结束，!ocr N外部OCR）：').strip()
        except (EOFError, KeyboardInterrupt):
            print(f'  {G}✓ 纠错完成，进入确认写入步骤{RST}')
            break
        if not choice:
            print(f'  {G}✓ 纠错完成，进入确认写入步骤{RST}')
            break
        if choice.lower() == '!done':
            break

        # --- !ocr 命令：外部OCR文本重识别 ---
        if choice.lower().startswith('!ocr'):
            _handle_external_ocr(choice, entries, fields_config)
            continue

        # 解析序号列表 + 检测OCR失败
        idx_list = []
        has_ocr_fails = False
        for part in choice.split(','):
            part = part.strip()
            if not part:
                continue
            try:
                idx = int(part) - 1
                if idx < 0 or idx >= len(entries):
                    print(f'  {R}无效序号: {part}{RST}')
                    continue
                idx_list.append(idx)
                # 检查是否有OCR失败
                if any('OCR失败' in n for n in entries[idx].get('notes', [])):
                    has_ocr_fails = True
            except ValueError:
                print(f'  {R}无效序号: {part}{RST}')
                continue

        if not idx_list:
            continue

        # 如果有OCR失败的条目，先让用户粘贴外部OCR文本
        if has_ocr_fails:
            for idx in idx_list:
                entry = entries[idx]
                ocr_fails = [n for n in entry.get('notes', []) if 'OCR失败' in n]
                if not ocr_fails:
                    continue
                name = entry.get('folder_name') or f'#{idx+1}'
                print(f'\n  {Y}⚠ {name} 有OCR失败: {"; ".join(ocr_fails)}{RST}')
                if not yes_no(f'    是否粘贴外部AI识别的文本？', True):
                    continue
                _paste_ocr_for_entry(entry, name, fields_config)

        # 正常字段编辑
        for idx in idx_list:

            entry = entries[idx]
            name = entry.get('folder_name') or entry.get('pdf_name') or f'#{idx+1}'
            print(f'\n  {BOLD}── 修改 {name} ──{RST}')

            # 展示所有字段当前值
            for i, (key, label, _ec) in enumerate(fields_config, 1):
                current = entry.get(key, '')
                if isinstance(current, list):
                    current = ', '.join(str(x) for x in current)
                display = str(current) if current else f'{DIM}(空){RST}'
                print(f'    [{Y}{i}{RST}] {label:12}: {display}')

            # 一次输入多个字段：格式 "编号1=值1, 编号3=值3"
            print(f'    {DIM}格式: 编号=新值, 如 2=张三,3=天津公司（回车跳过）{RST}')
            try:
                line = input(f'    → ').strip()
            except (EOFError, KeyboardInterrupt):
                line = ''
            if not line:
                print(f'    {DIM}未修改{RST}')
                continue

            changed = False
            for chunk in line.split(','):
                chunk = chunk.strip()
                if not chunk:
                    continue
                if '=' not in chunk:
                    continue
                try:
                    num_str, val = chunk.split('=', 1)
                    num = int(num_str.strip())
                    val = val.strip()
                    if 1 <= num <= len(fields_config):
                        key, label, _ec = fields_config[num - 1]
                        entry[key] = val
                        print(f'    {G}  [{num}] {label} → {val}{RST}')
                        changed = True
                    else:
                        print(f'    {R}  无效字段编号: {num}{RST}')
                except ValueError:
                    print(f'    {R}  格式错误: {chunk}{RST}')

            if changed:
                print(f'    {G}✓ 已更新{RST}')
            else:
                print(f'    {DIM}未修改{RST}')

    return entries


def _paste_ocr_for_entry(entry, name, fields_config):
    """粘贴外部OCR文本并用DeepSeek提取字段，结果直接写入entry"""
    print(f'  {DIM}请粘贴其他AI识别出的完整文本，输入空行结束：{RST}')

    lines = []
    while True:
        line = input()
        if line == '':
            break
        lines.append(line)
    text = '\n'.join(lines)

    if not text.strip():
        print(f'  {Y}未输入文本，取消{RST}')
        return

    # 选择类型
    print(f'  {DIM}文件类型：[1]起诉状/申请书  [2]传票  默认1{RST}')
    doc_type = input('    → ').strip()
    prompt = PROMPT_SUBPOENA if doc_type == '2' else PROMPT_COMPLAINT

    print(f'  {DIM}正在发送DeepSeek识别...{RST}')
    try:
        info = call_deepseek(prompt, text)
    except Exception as e:
        print(f'  {R}DeepSeek调用失败: {e}{RST}')
        return

    print(f'  {G}DeepSeek返回: {json.dumps(info, ensure_ascii=False)}{RST}')

    # 合并到entry
    if info.get('caseNo'):
        entry['case_no'] = info['caseNo']
    if info.get('plaintiff'):
        entry['plaintiff'] = info['plaintiff']
    if info.get('defendant'):
        entry['defendant'] = info['defendant']
    if info.get('cause'):
        entry['cause'] = info['cause']
    if info.get('court'):
        entry['court'] = info['court']
    if info.get('courtDate'):
        entry['court_date'] = info['courtDate']
    if info.get('claim'):
        entry['claim'] = info['claim']
    if info.get('amount'):
        entry['amount'] = info['amount']
    if info.get('keywords'):
        entry['keywords'] = info['keywords']
    if info.get('commercialDraftNo'):
        entry['commercial_draft_no'] = info['commercialDraftNo']
    if '仲裁' in str(entry.get('court', '')) or '仲裁' in str(entry.get('claim', '')):
        entry['litigation_type'] = '仲裁'
    elif not entry.get('litigation_type'):
        entry['litigation_type'] = '诉讼'

    # 移除外OCR失败备注
    entry['notes'] = [n for n in entry.get('notes', []) if 'OCR失败' not in n and '缺少' not in n]

    # 重新评估状态
    missing = []
    if not entry.get('case_no'):
        missing.append('案号')
    if not entry.get('plaintiff'):
        missing.append('原告')
    if missing:
        entry['status'] = 'warning'
        entry['notes'].append(f'缺少: {",".join(missing)}')
    else:
        entry['status'] = 'ok'

    print(f'  {G}✓ 已更新，状态: {entry["status"]}{RST}')

    # 展示更新后全部字段
    for i, (key, label, _ec) in enumerate(fields_config, 1):
        current = entry.get(key, '')
        display = str(current) if current else f'{DIM}(空){RST}'
        print(f'    [{Y}{i}{RST}] {label:12}: {display}')


def _handle_external_ocr(choice, entries, fields_config):
    """处理 !ocr N 命令：用户粘贴外部OCR文本，用DeepSeek提取字段"""
    parts = choice.split(maxsplit=1)
    if len(parts) < 2:
        print(f'  {R}用法: !ocr N  例如 !ocr 6{RST}')
        return
    try:
        idx = int(parts[1]) - 1
        if idx < 0 or idx >= len(entries):
            print(f'  {R}无效序号: {parts[1]}{RST}')
            return
    except ValueError:
        print(f'  {R}无效序号: {parts[1]}{RST}')
        return

    entry = entries[idx]
    name = entry.get('folder_name') or entry.get('pdf_name') or f'#{idx+1}'
    _paste_ocr_for_entry(entry, name, fields_config)


# ====================================================================
# ====================================================================
# 流程1：应诉材料批量处理（子文件夹模式）
# ====================================================================
# ====================================================================



# ====================================================================
# ====================================================================
# 流程2：判决书/裁决书批量处理
# ====================================================================





# ====================================================================
# 流程6：保全裁定收文归档（执保/财保统一处理）
# 模式：OCR → DeepSeek提取 → 三级匹配 → 写案件进展(追加) + 归档
# ====================================================================













# ====================================================================
# OCR 识别结果 md 缓存（断点续跑通用机制，流程1/2/6 共用）
# 每个处理单元（PDF 或案件子文件夹）识别完成后生成一个 md 文件：
#   - 流程正常完成 → 删除 md
#   - 中途中断     → md 保留，下次运行直接读取复用，跳过重新 OCR
# md 内同时保存"人可读摘要"与"<!-- OCRDATA {...json...} -->"机读块
# ====================================================================

def _ocr_md_path(pdf_or_dir):
    """md 缓存路径：PDF → 同级 'xxx.pdf.ocr.md'；文件夹 → 内部 '_ocr_result.md'"""
    if os.path.isdir(pdf_or_dir):
        return os.path.join(pdf_or_dir, '_ocr_result.md')
    return pdf_or_dir + '.ocr.md'


def _ocr_md_save(entry, md_path, source_label=''):
    """把 entry 的识别结果写入 md（人可读 + OCRDATA JSON 块）。失败仅提示不中断。"""
    try:
        data = {k: v for k, v in entry.items() if k != 'dup_row'}
        data.pop('dup_row', None)
        src = source_label or entry.get('pdf_path') or entry.get('folder_name') or entry.get('pdf_name') or ''
        lines = [
            '# 识别结果缓存',
            f'> 源文件：{os.path.basename(str(src))}',
            f'> 生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
            f'> 状态：{entry.get("status", "")} ｜ 已写入进展：{bool(entry.get("written"))} ｜ 已归档：{bool(entry.get("archived"))}',
            '',
            '## 提取信息',
        ]
        info = entry.get('info') or {}
        if isinstance(info, dict) and info:
            for k, v in info.items():
                if v not in (None, ''):
                    lines.append(f'- {k}: {v}')
        else:
            for f in ('case_no', 'plaintiff', 'defendant', 'court_date', 'judgment_date', 'preserve_no'):
                if entry.get(f):
                    lines.append(f'- {f}: {entry[f]}')
        lines.append('')
        lines.append('<!-- OCRDATA')
        lines.append(json.dumps(data, ensure_ascii=False, indent=1))
        lines.append('-->')
        tmp = md_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        os.replace(tmp, md_path)
    except Exception as ex:
        print(f'  {Y}⚠ 识别缓存写入失败（不影响本次运行）: {ex}{RST}')
        logging.exception('md缓存写入失败')


def _ocr_md_load(md_path):
    """读取 md 中的 OCRDATA JSON 块，返回 dict；文件不存在或损坏返回 None"""
    try:
        with open(md_path, encoding='utf-8') as f:
            content = f.read()
        m = re.search(r'<!--\s*OCRDATA\s*\n(.*?)\n\s*-->', content, re.S)
        if not m:
            return None
        return json.loads(m.group(1))
    except Exception:
        return None


def _ocr_md_remove(md_path):
    """删除 md 缓存（批次完成后调用）"""
    try:
        if os.path.exists(md_path):
            os.remove(md_path)
    except Exception:
        pass


def _ocr_md_try_resume(entry, md_path):
    """断点恢复：md 存在则把其中保存的识别结果填回 entry，返回 True；否则 False。
    dup_row 按保存的 dup_id 重建；written/archived 一并恢复，防止重复写入/归档。"""
    if not os.path.exists(md_path):
        return False
    data = _ocr_md_load(md_path)
    if not data:
        print(f'  {Y}⚠ 缓存文件损坏（{os.path.basename(md_path)}），将重新识别该文件{RST}')
        return False
    for k, v in data.items():
        if k != 'dup_row':
            entry[k] = v
    entry.setdefault('notes', [])
    entry.setdefault('info', {})
    entry.setdefault('written', False)
    entry.setdefault('archived', False)
    entry['dup_row'] = None
    if entry.get('dup_id'):
        entry['dup_row'] = db_find_case_by_id(entry.get('dup_id'))   # dup_id 可能是数据库 id
        if not entry.get('dup_row'):
            _db_id, _row = _find_case_by_seq(str(entry.get('dup_id')))  # 也可能是案件序号
            if _db_id:
                entry['dup_row'] = _row
                entry['dup_id'] = _db_id
    if entry.get('dup_id') and not entry.get('dup_row'):
        entry['status'] = 'warning'
        entry['dup_id'] = None
        entry['notes'] = list(entry.get('notes', [])) + ['缓存匹配记录已失效，请重新匹配']
    print(f'  {C}♻️  复用上次识别结果（缓存: {os.path.basename(md_path)}）{RST}')
    return True


# ====================================================================
# 流程6 检查点存档（断点续跑）与结果报告
# ====================================================================












# ====================================================================
# 主菜单
# ====================================================================

def main():
    global SERVER_URL, AUTO_MODE, CLI_FOLDER, CLI_DATE

    args = parse_cli_args()
    setup_logging()
    AUTO_MODE = args.auto
    CLI_FOLDER = (args.folder or '').strip().strip('"').strip("'")
    if args.date:
        dt = _parse_date_flexible(args.date)
        if dt is None:
            print(f'{R}--date 格式无法解析：{args.date}（请用 YYYYMMDD 或 YYYY-MM-DD）{RST}')
            return
        CLI_DATE = dt.strftime('%Y-%m-%d')
    logging.info('程序启动 auto=%s flow=%s', AUTO_MODE, args.flow)

    # --- 服务器连接（远程 API 模式） ---
    if not SERVER_URL:
        print('未检测到服务器地址，请输入服务器地址')
        SERVER_URL = input('服务器地址（如 http://127.0.0.1:8899）：').strip()
        if not SERVER_URL:
            print('未配置服务器，无法运行')
            return
    mode_str = f'远程模式 → {SERVER_URL}'

    count = db_get_case_count()
    if count == 0 and _api_get_all('案件情况表') is None:
        print(f'无法连接到服务器 {SERVER_URL}，请确认服务器已启动')
        return

    # 先登录获取 token
    if not SESSION_TOKEN:
        if not _api_login():
            return

    print('╔' + '═' * 58 + '╗')
    print('║' + '  案件处理系统 V5 — 火山引擎OCR版'.center(52) + '║')
    print('║' + mode_str.center(50) + '║')
    print('║' + f'  共{db_get_case_count()}条记录'.center(50) + '║')
    print('║' + f'  时间：{datetime.now().strftime("%Y-%m-%d %H:%M")}'.center(52) + '║')
    if CURRENT_USER:
        print('║' + f'  登录用户: {CURRENT_USER.get("display_name", "")} ({CURRENT_USER.get("role", "")})'.center(42) + '║')
    print('╚' + '═' * 58 + '╝')

    print('\n可用操作：')
    print(f'  {BOLD}1.{RST} 应诉材料批量处理（子文件夹=案件 → 批量OCR → 汇总纠错 → 统一写入）')
    print(f'  {BOLD}2.{RST} 判决书/裁决书批量处理（批量OCR → 匹配 → 汇总纠错 → 统一更新）')
    print(f'  {BOLD}6.{RST} 保全裁定收文归档（执保/财保统一：OCR → 三级匹配 → 写案件进展 + 归档）')
    print(f'  {BOLD}3.{RST} 退出')

    # --flow 参数直接指定流程，跳过菜单
    if args.flow:
        choice = args.flow
    else:
        choice = input('\n请选择操作（1/2/6/3）：').strip()

    # 写操作：未登录则自动用管理员登录
    if choice in ('1', '2', '6'):
        if not SESSION_TOKEN:
            if not _api_login():
                return

    if choice == '1':
        if not check_credentials():
            return
        from flow_response import process_response_materials
        process_response_materials()
    elif choice == '2':
        if not check_credentials():
            return
        from flow_judgment import process_judgments
        process_judgments()
    elif choice == '6':
        if not check_credentials():
            return
        from flow_preserve import process_preserve_docs
        process_preserve_docs()
    else:
        print('退出')
    logging.info('流程选择: %s', choice)

    print('\n操作完成。')


# ====================================================================
# 数据层（远程 API 版）导入
# db.py 通过 `import 案件处理_v5 as _app` 共享本模块状态，故必须在本模块
# 顶层全部执行完后导入（放在文件末尾），避免循环导入问题。
# ====================================================================
import db
from db import (_api_login, _api_request, _api_get_all, _api_insert, _api_update, db_find_case_by_id,
                db_get_max_case_id, db_check_duplicate_unclosed, db_find_case_in_all_tables,
                db_insert_case, db_update_case, db_insert_unpaid, db_get_case_count)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n用户中断，已退出')
    except Exception as e:
        logging.error('程序异常退出: %s', e, exc_info=True)
        print(f'{R}程序异常退出: {e}{RST}')
        print(f'  详细信息已写入日志：{LOG_FILE}')
        raise
