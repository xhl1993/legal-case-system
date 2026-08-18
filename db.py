# -*- coding: utf-8 -*-
"""数据层（远程 API 版）：案件处理系统 V5 的数据库访问统一走 HTTP API。

本地 SQLite 模式已移除（当前环境只使用远程服务器，见 main() 中模式判定）。
注意：本模块通过从 sys.modules 中获取宿主脚本（案件处理_v5）共享其状态与常量
（SERVER_URL / SESSION_TOKEN / CURRENT_USER / CASES_COLUMNS / _row_get 等），
由宿主脚本在文件末尾 `import db` 触发加载。
不能直接 `import 案件处理_v5`：当它作为主脚本运行时模块名是 __main__，
直接导入会触发整个脚本被重复执行，与文件末尾的 `import db` 形成循环导入。
"""

import json
import logging
import sys
import urllib.request
import urllib.error
import urllib.parse

# 从 sys.modules 中取已初始化完毕（顶层代码全部执行）的宿主模块：
# - 宿主脚本被 import 时，模块名为 '案件处理_v5'；
# - 宿主脚本被直接运行时（python 案件处理_v5.py），模块名为 '__main__'；
# 用 SERVER_URL 属性校验避免取到无关模块。
_host = None
for _name in ('案件处理_v5', '__main__'):
    _mod = sys.modules.get(_name)
    if _mod is not None and hasattr(_mod, 'SERVER_URL'):
        _host = _mod
        break
if _host is None:
    raise ImportError('db.py 必须由案件处理_v5.py 作为宿主脚本导入（未在 sys.modules 中找到宿主模块）')
_app = _host


def _api_login(username=None, password=None):
    """远程模式登录，获取 session token"""
    if not username:
        username = _app.DEFAULT_LOGIN_USERNAME
    if not password:
        password = _app.DEFAULT_LOGIN_PASSWORD

    url = _app.SERVER_URL.rstrip('/') + '/api/login'
    body = json.dumps({'username': username, 'password': password}, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json; charset=utf-8')

    try:
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read().decode('utf-8'))
        if result.get('success') and result.get('token'):
            _app.SESSION_TOKEN = result['token']
            _app.CURRENT_USER = result.get('user', {})
            print(f'  {_app.G}✅ 登录成功: {_app.CURRENT_USER.get("display_name", username)} ({_app.CURRENT_USER.get("role", "")}){_app.RST}')
            return True
        else:
            print(f'  {_app.R}登录失败: {result.get("error", "未知错误")}{_app.RST}')
            return False
    except Exception as e:
        print(f'  {_app.R}登录失败: {e}{_app.RST}')
        return False


def _api_request(method, path, body=None):
    """通用 API 请求，返回解析后的 JSON；失败返回 None"""
    encoded_path = urllib.parse.quote(path, safe='/?=&')
    url = _app.SERVER_URL.rstrip('/') + encoded_path
    data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json; charset=utf-8')
    # 写操作带上 session token
    if method in ('POST', 'PUT', 'DELETE') and _app.SESSION_TOKEN:
        req.add_header('X-Auth-Token', _app.SESSION_TOKEN)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print(f'{_app.R}  [认证失败] 登录已过期或密码错误，请重新登录{_app.RST}')
            logging.warning('API 认证失败 (403)，会话已失效')
            # 清除失效token
            _app.SESSION_TOKEN = None
            _app.CURRENT_USER = None
            return None
        print(f'  [API错误] HTTP {e.code}: {url}')
        logging.error('API 请求失败 HTTP %s: %s', e.code, url)
        return None
    except urllib.error.URLError as e:
        print(f'  [API错误] 无法连接服务器 {_app.SERVER_URL}: {e}')
        logging.error('无法连接服务器 %s: %s', _app.SERVER_URL, e)
        return None
    except Exception as e:
        print(f'  [API错误] {e}')
        logging.error('API 请求异常: %s', e)
        return None


def _api_get_all(tab_name):
    """获取整表数据，返回行列表（兼容 list / {'data': [...]} 响应）"""
    result = _api_request('GET', f'/api/data/{tab_name}')
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get('data', result.get('items', []))
    return []


def _api_insert(tab_name, row_data):
    """新增记录，返回新记录 id（失败返回 None）"""
    result = _api_request('POST', f'/api/data/{tab_name}', {'data': row_data})
    if result is None:
        return None
    return result.get('id')


def _api_update(tab_name, row_id, updates):
    """更新记录，返回是否成功"""
    result = _api_request('PUT', f'/api/data/{tab_name}/{row_id}', {'data': updates})
    return result is not None


def db_get_max_case_id():
    """获取当前最大案件序号（用于新案件序号生成）"""
    result = _api_request('GET', '/api/max_case_id')
    if result and 'max_id' in result:
        return int(result['max_id'] or 0)
    return 0


def db_check_duplicate_unclosed(case_no, statuses=None):
    """在未结案/排期案件中按案号查重。返回 (db_id, row) 或 (None, None)"""
    if statuses is None:
        statuses = _app.UNCLOSED_STATUSES
    rows = _api_get_all('案件情况表')
    return _app._search_case_no_in_rows(rows, case_no, status_filter=statuses)


def db_find_case_in_all_tables(case_no):
    """在所有案件中按案号查找（不限状态）。返回 (table_name, id, row)"""
    rows = _api_get_all('案件情况表')
    result = _app._search_case_no_in_rows(rows, case_no)
    if result[0]:
        return 'cases', result[0], result[1]
    return None, None, None


def db_insert_case(data_dict):
    """新增案件记录，返回新记录 id"""
    filtered = {}
    for col in _app.CASES_COLUMNS:
        filtered[col] = data_dict.get(col, '')
    return _api_insert('案件情况表', list(filtered.values()))


def db_update_case(case_id, updates):
    """更新案件记录，返回受影响行数（1 或 0）"""
    all_data = _api_get_all('案件情况表')
    existing = next((r for r in all_data if r.get('id') == case_id), None)
    if existing:
        row_data = []
        for col in _app.CASES_COLUMNS:
            val = updates.get(col, _app._row_get(existing, col))
            row_data.append(val)
        ok = _api_update('案件情况表', case_id, row_data)
        if not ok:
            print(f'  {_app.R}⚠ 远程更新失败：案件 id={case_id}{_app.RST}')
            logging.error('远程更新案件失败: id=%s', case_id)
            return 0
        return 1
    print(f'  {_app.R}⚠ 远程数据库中不存在 id={case_id} 的记录，更新失败{_app.RST}')
    logging.warning('远程数据库未找到案件 id=%s', case_id)
    return 0


def db_insert_unpaid(data_dict):
    """登记未付款计划"""
    filtered = {}
    for col in _app.UNPAID_COLUMNS:
        filtered[col] = data_dict.get(col, '')
    _api_insert('未付款计划', list(filtered.values()))


def db_get_case_count():
    """获取案件总数"""
    rows = _api_get_all('案件情况表')
    return len(rows)


def db_find_case_by_id(db_id):
    """按数据库主键 id 查询案件行（远程 API 版，md 缓存恢复时重建 dup_row）"""
    if not db_id:
        return None
    try:
        rows = _api_get_all('案件情况表') or []
        return next((r for r in rows if r.get('id') == db_id), None)
    except Exception:
        return None
