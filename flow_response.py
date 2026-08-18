# -*- coding: utf-8 -*-
"""流程模块（从 案件处理_v5 拆分）：复用主文件的配置/工具/OCR/DeepSeek 能力。
通过 from 案件处理_v5 import * 获取共享函数与常量；下划线内部函数在此显式导入。
"""
from 案件处理_v5 import *
from 案件处理_v5 import (
    _row_get, _row_id, _stored_case_no_list, _case_no_matches, _search_case_no_in_rows,
    _parse_date_flexible, _detect_instance_level, _resolve_instance_level, _calc_appeal_deadline,
    _find_case_by_seq, _truncate, _pad_str, _extract_batch, _handle_external_ocr, _paste_ocr_for_entry,
    _ocr_md_path, _ocr_md_save, _ocr_md_load, _ocr_md_remove, _ocr_md_try_resume,
)
from db import (
    _api_login, _api_request, _api_get_all, _api_insert, _api_update, db_find_case_by_id,
    db_get_max_case_id, db_check_duplicate_unclosed, db_find_case_in_all_tables,
    db_insert_case, db_update_case, db_insert_unpaid, db_get_case_count,
)


# ====================================================================
# 流程1：应诉材料批处理
# ====================================================================


def identify_file_type(filename):
    """识别文件类型。起诉状/传票类走 OCR+DeepSeek，其余一律当证据直接归档。"""
    name = os.path.splitext(filename)[0]
    # 传票类 → OCR
    if any(kw in name for kw in ['传票', '开庭传票', '传唤', '应诉通知', '开庭通知', '举证通知']):
        return 'subpoena'
    # 起诉状/申请书类 → OCR
    if any(kw in name for kw in ['起诉状', '民事起诉', '诉状', '起诉书',
                                   '仲裁申请书', '仲裁申请', '申请书',
                                   '反诉状', '答辩状', '仲裁反请求']):
        return 'complaint'
    # 其余一律当证据（不OCR，直接归档）
    return 'evidence'


def process_response_materials():
    print('\n' + '═' * 60)
    print('  流程1：应诉材料批量处理 V5（火山引擎OCR）')
    print('  模式：子文件夹=案件 → 批量OCR → 汇总 → 纠错 → 统一写入')
    print('═' * 60)
    logging.info('流程1 开始（应诉材料批处理）')

    folder = CLI_FOLDER or RESPONSE_FOLDER
    if not folder or not os.path.exists(folder):
        if AUTO_MODE:
            print(f'{R}错误：文件夹不存在 - {folder}{RST}')
            logging.error('流程1 文件夹不存在: %s', folder)
            return
        folder = input('请输入应诉材料PDF所在文件夹路径：').strip().strip('"').strip("'")
    if not os.path.exists(folder):
        print(f'{R}错误：文件夹不存在 - {folder}{RST}')
        logging.error('流程1 文件夹不存在: %s', folder)
        return
    logging.info('流程1 处理文件夹: %s', folder)

    # --- 扫描子文件夹 ---
    subdirs = []
    all_items = sorted(os.listdir(folder))
    for item in all_items:
        item_path = os.path.join(folder, item)
        if os.path.isdir(item_path) and not item.startswith('_ocr_tmp'):
            pdfs_in = sorted(glob.glob(os.path.join(item_path, '*.pdf')))
            if pdfs_in:
                subdirs.append((item, item_path, pdfs_in))

    if not subdirs:
        # 兼容：无子文件夹时，把整个文件夹当单个案件
        pdfs = sorted(glob.glob(os.path.join(folder, '*.pdf')))
        if pdfs:
            subdirs = [(os.path.basename(folder), folder, pdfs)]
        else:
            print(f'{R}未找到PDF文件或子文件夹{RST}')
            return

    print(f'\n扫描到 {len(subdirs)} 个案件子文件夹：')
    for i, (name, path, pdfs) in enumerate(subdirs):
        print(f'  {BOLD}{i+1}.{RST} {name}/  ({len(pdfs)}个PDF)')
        for pdf in pdfs:
            ftype = identify_file_type(os.path.basename(pdf))
            label = {'complaint': '起诉状', 'subpoena': '传票', 'evidence': '证据'}.get(ftype, '证据')
            print(f'      [{label}] {os.path.basename(pdf)}')

    if not yes_no(f'\n开始批量处理 {len(subdirs)} 个案件？', True):
        return

    # --- 日期输入（统一收文日期） ---
    receive_date = smart_date_input('\n请输入收文日期（所有案件共用）', datetime.now().strftime('%Y-%m-%d'))

    # --- 批量OCR + DeepSeek ---
    entries = []  # 批量结果收集

    for idx, (folder_name, folder_path, pdfs) in enumerate(subdirs):
        print(f'\n{"━" * 55}')
        print(f'  [{idx+1}/{len(subdirs)}] {BOLD}{folder_name}/{RST}')
        print(f'{"━" * 55}')

        entry = {
            'idx': idx + 1,
            'folder_name': folder_name,
            'folder_path': folder_path,
            'status': 'warning',
            'case_no': '',
            'plaintiff': '',
            'defendant': '',
            'cause': '',
            'court': '',
            'court_date': '',
            'claim': '',
            'amount': '',
            'keywords': '',
            'commercial_draft_no': '',
            'litigation_type': '',
            'dup_id': None,
            'dup_row': None,
            'notes': [],
            'files': {'complaint': [], 'subpoena': [], 'evidence': []},
            'info': {},
            'assigned_case_id': '',  # 用户手动指定的案件序号
            'match_seq': '',
            'written': False,
            'archived': False,
        }

        # --- 断点恢复：该案件已有识别缓存（上次中断）则直接复用，跳过重新OCR ---
        md_path = _ocr_md_path(folder_path)
        if _ocr_md_try_resume(entry, md_path):
            # 从 info 重建字段
            entry['case_no'] = entry.get('info', {}).get('caseNo', '')
            entry['plaintiff'] = entry.get('info', {}).get('plaintiff', '')
            entry['defendant'] = entry.get('info', {}).get('defendant', '')
            entry['cause'] = entry.get('info', {}).get('cause', '')
            entry['court'] = entry.get('info', {}).get('court', '')
            entry['court_date'] = entry.get('info', {}).get('courtDate', '')
            entry['claim'] = entry.get('info', {}).get('claim', '')
            entry['amount'] = entry.get('info', {}).get('amount', '')
            entry['keywords'] = entry.get('info', {}).get('keywords', '')
            entry['commercial_draft_no'] = entry.get('info', {}).get('commercialDraftNo', '')
            if '仲裁' in str(entry['court']) or '仲裁' in str(entry['claim']):
                entry['litigation_type'] = '仲裁'
            else:
                entry['litigation_type'] = '诉讼'
            # 匹配：优先缓存 match_seq / dup_id，其次按案号重查
            if entry.get('match_seq'):
                db_id, row = _find_case_by_seq(entry['match_seq'])
                if db_id:
                    entry['dup_id'], entry['dup_row'] = db_id, row
                    entry['status'] = 'duplicate'
            if not entry.get('dup_row') and entry.get('case_no'):
                dup_id, dup_row = db_check_duplicate_unclosed(entry['case_no'])
                if dup_id:
                    entry['dup_id'], entry['dup_row'] = dup_id, dup_row
                    entry['status'] = 'duplicate'
                    entry['notes'] = [n for n in entry.get('notes', []) if '已存在' not in n and '重新匹配' not in n]
                    entry['notes'].append(f'重新匹配: 序号{_row_get(dup_row, "案件序号")}')
                    print(f'  {C}🔄 重新匹配: {entry["case_no"]} (序号{_row_get(dup_row, "案件序号")}){RST}')
            entries.append(entry)
            continue

        # 分类文件
        for pdf in pdfs:
            ftype = identify_file_type(os.path.basename(pdf))
            entry['files'].setdefault(ftype, []).append(pdf)

        complaint_files = entry['files'].get('complaint', [])
        subpoena_files = entry['files'].get('subpoena', [])
        evidence_files = entry['files'].get('evidence', [])

        if not complaint_files and not subpoena_files:
            entry['status'] = 'error'
            entry['notes'].append('无起诉状/传票')
            print(f'  {Y}跳过：无起诉状/传票文件{RST}')
            entries.append(entry)
            continue

        # --- 并行OCR ---
        ocr_needed = complaint_files + subpoena_files
        ocr_results = {}
        print(f'  并行OCR {len(ocr_needed)} 个文件...')
        with ThreadPoolExecutor(max_workers=OCR_PARALLEL_WORKERS) as executor:
            futures = {executor.submit(ocr_pdf_safe, pdf, 'all'): pdf for pdf in ocr_needed}
            for future in as_completed(futures):
                pdf = futures[future]
                try:
                    ocr_results[pdf] = future.result()
                except Exception as e:
                    ocr_results[pdf] = ''
                    print(f'  {R}OCR异常 {os.path.basename(pdf)}: {e}{RST}')

        # --- DeepSeek处理（并行，用上 DEEPSEEK_PARALLEL_WORKERS） ---
        merged_info = {}

        # 记录 OCR 失败的文件
        for pdf in complaint_files + subpoena_files:
            if not ocr_results.get(pdf):
                entry['notes'].append(f'OCR失败: {os.path.basename(pdf)}')

        complaint_texts = {pdf: ocr_results[pdf] for pdf in complaint_files if ocr_results.get(pdf)}
        subpoena_texts = {pdf: ocr_results[pdf] for pdf in subpoena_files if ocr_results.get(pdf)}

        complaint_infos = _extract_batch(complaint_texts, PROMPT_COMPLAINT, entry)
        subpoena_infos = _extract_batch(subpoena_texts, PROMPT_SUBPOENA, entry)

        # 合并起诉状信息
        for pdf, info in complaint_infos.items():
            merged_info.update({k: v for k, v in info.items() if v is not None})
        # 合并传票信息（传票的开庭时间/案号优先级更高，覆盖起诉状）
        for pdf, info in subpoena_infos.items():
            for key in ('courtDate', 'court', 'caseNo', 'cause'):
                if info.get(key):
                    merged_info[key] = info[key]

        entry['info'] = merged_info

        # 填充字段
        entry['case_no'] = merged_info.get('caseNo', '')
        entry['plaintiff'] = merged_info.get('plaintiff', '')
        entry['defendant'] = merged_info.get('defendant', '')
        entry['cause'] = merged_info.get('cause', '')
        entry['court'] = merged_info.get('court', '')
        entry['court_date'] = merged_info.get('courtDate', '')
        entry['claim'] = merged_info.get('claim', '')
        entry['amount'] = merged_info.get('amount', '')
        entry['keywords'] = merged_info.get('keywords', '')
        entry['commercial_draft_no'] = merged_info.get('commercialDraftNo', '')

        if '仲裁' in str(entry['court']) or '仲裁' in str(entry['claim']):
            entry['litigation_type'] = '仲裁'
        else:
            entry['litigation_type'] = '诉讼'

        # 查重（V5.1: 不再跳过重复条目，保留完整OCR信息用于后续更新开庭时间/案号）
        if entry['case_no']:
            dup_id, dup_row = db_check_duplicate_unclosed(entry['case_no'])
            if dup_id:
                entry['dup_id'] = dup_id
                entry['dup_row'] = dup_row
                exist_id = _row_get(dup_row, '案件序号')
                entry['notes'].append(f'已存在: 序号{exist_id}')
                print(f'  {C}🔄 案号已存在: {entry["case_no"]} (序号{exist_id})，将收集传票信息用于更新{RST}')

        # 检查关键字段
        missing = []
        if not entry['case_no']:
            missing.append('案号')
        if not entry['plaintiff']:
            missing.append('原告')

        # 确定状态：有匹配到已有案件→duplicate；缺关键字段且无匹配→warning；其余→ok
        if entry.get('dup_id'):
            entry['status'] = 'duplicate'
        elif missing:
            entry['status'] = 'warning'
            entry['notes'].append(f'缺少: {",".join(missing)}')
            print(f'  {Y}⚠ 缺失关键字段: {",".join(missing)}{RST}')
        else:
            entry['status'] = 'ok'
            print(f'  {G}✅ 提取完整{RST}')

        entry['match_seq'] = _row_get(entry.get('dup_row'), '案件序号') if entry.get('dup_row') else ''
        entry['written'] = False
        entry['archived'] = False
        # 识别完成 → 写 md 缓存（断点续跑：中断后下次直接复用）
        _ocr_md_save(entry, md_path)
        entries.append(entry)

    # --- 显示汇总表 ---
    # 计算案件序号（决定这条传票/应诉材料归属到哪个案件）
    # 优先自动匹配 → 其次手动指定 → 其他留空
    for e in entries:
        if e.get('dup_row'):
            e['case_id'] = str(_row_get(e['dup_row'], '案件序号'))
        elif e.get('assigned_case_id'):
            e['case_id'] = e['assigned_case_id']
        else:
            e['case_id'] = ''

    columns = [
        ('idx', '序号', 4),
        ('folder_name', '案件文件夹', 24),
        ('case_no', '案号', 20),
        ('case_id', '案件序号', 8),
        ('plaintiff', '原告', 8),
        ('defendant', '被告', 8),
        ('cause', '案由', 8),
        ('keywords', '涉及项目', 12),
        ('commercial_draft_no', '商票号码', 12),
        ('court_date', '开庭时间', 14),
    ]
    print_summary_table(entries, columns, '流程1 批量处理结果汇总', notes_width=18)

    # --- 纠错 ---
    print(f'\n{BOLD}--- 人工纠错 ---{RST}')
    print(f'  输入序号可修改对应案件的所有字段')
    print(f'  {DIM}（回车跳过=确认无误，直接写入）{RST}')

    fields_config = [
        ('case_id', '案件序号', False),
        ('case_no', '案号', False),
        ('plaintiff', '原告', False),
        ('defendant', '被告', False),
        ('cause', '案由', False),
        ('keywords', '涉及项目', False),
        ('commercial_draft_no', '商票号码', False),
        ('court', '法院/仲裁委', False),
        ('court_date', '开庭时间', False),
        ('claim', '诉讼请求', False),
        ('amount', '请求金额(元)', False),
        ('litigation_type', '诉讼/仲裁', True),
    ]
    entries = interact_correct(entries, fields_config)

    # 纠错后把手动填写的案件序号同步为一个伪 assigned_case_id，用于后续归属判断
    for e in entries:
        if e.get('case_id') and not e.get('assigned_case_id'):
            e['assigned_case_id'] = str(e['case_id']).strip()

    # 如果手动填的案件序号能在库里找到，自动建立关联（等同于手动指定）
    conn_sync = None  # 远程 API 模式无需本地连接
    try:
        for e in entries:
            if e.get('assigned_case_id') and not e.get('dup_row'):
                cid = e['assigned_case_id']
                db_id, row = _find_case_by_seq(cid, conn_sync)
                if db_id:
                    e['dup_row'] = row
                    e['dup_id'] = db_id
                    e['status'] = 'duplicate'
                    print(f'  {G}  🔗 序号{cid} → {_row_get(row, "原告")}（自动匹配）{RST}')
    finally:
        if conn_sync:
            conn_sync.close()

    # --- 手动指定案件序号（V5.1: 适用于传票/应诉文档找不到自动匹配的情况） ---
    print(f'\n{BOLD}--- 手动指定案件序号 ---{RST}')
    print(f'  对于 warning/duplicate 条目，可手工指定归属的案件序号')
    print(f'  格式: "条目序号=案件序号"，如 "3=1725", "1=1725,2=1726"')
    print(f'  {DIM}（回车跳过，不修改归属）{RST}')
    try:
        manual_assign = input('  → ').strip()
    except (EOFError, KeyboardInterrupt):
        manual_assign = ''

    if manual_assign:
        conn_assign = None  # 远程 API 模式无需本地连接
        try:
            for chunk in manual_assign.split(','):
                chunk = chunk.strip()
                if '=' not in chunk:
                    continue
                try:
                    num_str, case_id_str = chunk.split('=', 1)
                    idx = int(num_str.strip()) - 1
                    case_id_str = case_id_str.strip()
                    if 0 <= idx < len(entries):
                        entry = entries[idx]
                        entry['assigned_case_id'] = case_id_str
                        # 按案件序号查库
                        db_id, row = _find_case_by_seq(case_id_str, conn_assign)
                        if db_id:
                            entry['dup_row'] = row
                            entry['dup_id'] = db_id
                            entry['status'] = 'duplicate'
                            old_notes = [n for n in entry.get('notes', []) if '缺少' not in n and '已存在' not in n and '手动归属' not in n]
                            entry['notes'] = old_notes + [f'手动归属: 序号{case_id_str}']
                            print(f'  {G}  #{num_str} → 案件序号{case_id_str} ({_row_get(row, "原告")}){RST}')
                        else:
                            print(f'  {R}  #{num_str} → 案件序号{case_id_str} 在数据库中不存在！{RST}')
                    else:
                        print(f'  {R}  无效条目序号: {num_str}{RST}')
                except ValueError:
                    print(f'  {R}  格式错误: {chunk}{RST}')
        finally:
            if conn_assign:
                conn_assign.close()

    # --- 手动指定后重新显示汇总表 ---
    if manual_assign:
        for e in entries:
            if e.get('dup_row'):
                e['case_id'] = str(_row_get(e['dup_row'], '案件序号'))
            elif e.get('assigned_case_id'):
                e['case_id'] = e['assigned_case_id']
            else:
                e['case_id'] = ''
        print_summary_table(entries, columns, '流程1 批量处理结果汇总（手动指定后）', notes_width=18)

    # --- 交互完成后：更新 md 缓存（保留最终人工匹配结果，中断后可续） ---
    for e in entries:
        if e.get('folder_path'):
            e['match_seq'] = str(e.get('case_id') or e.get('assigned_case_id') or '').strip()
            _ocr_md_save(e, _ocr_md_path(e['folder_path']))

    # --- 统一写入 ---
    print(f'\n{BOLD}--- 确认写入 ---{RST}')
    to_write = [e for e in entries if e['status'] in ('ok', 'warning')]
    to_skip = [e for e in entries if e['status'] == 'error']
    to_dup = [e for e in entries if e['status'] == 'duplicate']

    if to_write:
        print(f'  将新增 {G}{len(to_write)}{RST} 条记录')
    if to_dup:
        print(f'  将更新 {C}{len(to_dup)}{RST} 条（归档+更新开庭时间/案号）')
    if to_skip:
        print(f'  处理失败跳过 {R}{len(to_skip)}{RST} 条')

    if not yes_no('\n确认写入数据库并归档文件？', True):
        print('已取消')
        return

    # --- 写入 ---
    try:
        for i, entry in enumerate(entries, 1):
            # 上次已写入（中断恢复）→ 跳过，避免重复新增/更新
            if entry.get('written'):
                print(f'  [{i}/{len(entries)}] {DIM}⏭ 上次已写入，跳过: {entry["folder_name"]}{RST}')
                continue
            if entry['status'] == 'error':
                print(f'  [{i}/{len(entries)}] {R}❌ 跳过: {entry["folder_name"]} (处理失败){RST}')
                continue

            if entry['status'] == 'duplicate':
                dup_row = entry.get('dup_row', {})
                case_id = _row_get(dup_row, '案件序号')

                # --- V5.1: 更新数据库——开庭时间、案号、案件状态 ---
                updates = {}
                new_court_date = entry.get('court_date', '')
                new_case_no = entry.get('case_no', '')
                existing_case_nos = _row_get(dup_row, '案号') or ''
                existing_court_date = _row_get(dup_row, '开庭时间') or ''
                db_id = entry.get('dup_id')

                # 更新开庭时间（传票上的新开庭时间）
                if new_court_date and new_court_date != existing_court_date:
                    updates['开庭时间'] = new_court_date
                    updates['案件状态'] = new_case_status(new_court_date)
                    print(f'  [{i}/{len(entries)}] {C}📅 更新开庭时间: {existing_court_date or "(空)"} → {new_court_date}{RST}')

                # 更新案号（追加新案号，不覆盖已有的）
                if new_case_no and existing_case_nos:
                    existing_list = [normalize_case_no(n) for n in str(existing_case_nos).replace('\\n', '\n').split('\n') if n.strip()]
                    if normalize_case_no(new_case_no) not in existing_list:
                        updates['案号'] = existing_case_nos + '\n' + new_case_no
                        print(f'  [{i}/{len(entries)}] {C}📝 追加案号: {new_case_no}{RST}')
                elif new_case_no and not existing_case_nos:
                    updates['案号'] = new_case_no
                    print(f'  [{i}/{len(entries)}] {C}📝 设置案号: {new_case_no}{RST}')

                # 更新涉及项目（如果OCR识别出了新关键词）
                new_keywords = entry.get('keywords', '')
                existing_keywords = _row_get(dup_row, '涉及项目') or ''
                if new_keywords and new_keywords != existing_keywords:
                    updates['涉及项目'] = new_keywords
                    print(f'  [{i}/{len(entries)}] {C}🏷️ 更新涉及项目: {existing_keywords or "(空)"} → {new_keywords}{RST}')

                # 更新商票号码
                new_draft = entry.get('commercial_draft_no', '')
                existing_draft = _row_get(dup_row, '商票号码') or ''
                if new_draft and new_draft != existing_draft:
                    updates['商票号码'] = new_draft
                    print(f'  [{i}/{len(entries)}] {C}🎫 更新商票号码: {existing_draft or "(空)"} → {new_draft}{RST}')

                # 执行数据库更新
                if updates and db_id:
                    db_update_case(db_id, updates)
                    print(f'  [{i}/{len(entries)}] {G}✅ 已更新案件{case_id}数据库{RST}')
                elif not updates and db_id:
                    print(f'  [{i}/{len(entries)}] {C}📋 案件{case_id}无需更新（开庭时间/案号无变化）{RST}')

                # 归档文件到已有案件文件夹
                all_files = entry['files'].get('complaint', []) + \
                            entry['files'].get('subpoena', []) + \
                            entry['files'].get('evidence', [])
                for pdf in all_files:
                    if os.path.exists(pdf):
                        ftype = identify_file_type(os.path.basename(pdf))
                        label_map = {'complaint': '起诉状', 'subpoena': '传票', 'evidence': '证据'}
                        doc_label = label_map.get(ftype, os.path.splitext(os.path.basename(pdf))[0])
                        archive_file(pdf, case_id, receive_date, doc_label)
                print(f'  [{i}/{len(entries)}] {C}🔄 已归档: {entry["folder_name"]} → 案件{case_id}/{RST}')
                entry['written'] = True
                entry['archived'] = True
                _ocr_md_save(entry, _ocr_md_path(entry['folder_path']))
                continue

            # 新增
            print(f'  [{i}/{len(entries)}] 正在写入 {entry["folder_name"]}...', end=' ')
            sys.stdout.flush()

            # 序号生成：远程模式查一次最大值
            case_id = str(db_get_max_case_id() + 1)

            # 金额解析：兼容 '100万'/'万元'/逗号分隔，解析失败按 0 处理（不再静默丢失）
            amount_num = parse_amount(entry.get('amount'))

            lit_type = normalize_litigation_type(entry.get('litigation_type', ''), entry.get('court', ''))
            new_case = {
                '案件序号': case_id,
                '案件状态': new_case_status(entry.get('court_date', '')),
                '收到案件的时间': receive_date,
                '开庭时间': entry.get('court_date', ''),
                '诉讼请求': entry.get('claim', ''),
                '诉讼/仲裁': lit_type,
                '类型': '被申请人' if is_arbitration(lit_type) else '被告',
                '原告': entry.get('plaintiff', ''),
                '被告': entry.get('defendant', ''),
                '案由': entry.get('cause', ''),
                '涉及项目': entry.get('keywords', ''),
                '商票号码': entry.get('commercial_draft_no', ''),
                '法院/仲裁委': entry.get('court', ''),
                '案号': entry.get('case_no', ''),
                '作为被告涉案诉讼金额(元)': str(amount_num) if amount_num else '',
            }

            db_insert_case(new_case)

            print(f'{G}✅ 序号{case_id}{RST}')

            # 归档
            all_files = entry['files'].get('complaint', []) + \
                        entry['files'].get('subpoena', []) + \
                        entry['files'].get('evidence', [])
            folder_name = f'{case_id}_{entry["plaintiff"]}' if entry.get('plaintiff') else str(case_id)
            target_dir = os.path.join(CASE_FOLDER, folder_name)
            os.makedirs(target_dir, exist_ok=True)
            date_prefix = receive_date.replace('-', '')
            for pdf in all_files:
                if not os.path.exists(pdf):
                    continue
                new_name = f'{date_prefix}_{os.path.basename(pdf)}'
                if safe_move(pdf, os.path.join(target_dir, new_name)):
                    print(f'    归档: {new_name} → {folder_name}/')
            entry['written'] = True
            entry['archived'] = True
            _ocr_md_save(entry, _ocr_md_path(entry['folder_path']))
    except Exception as e:
        print(f'  {R}写入过程出错（本次可能未全部写入）：{e}{RST}')
        logging.error('流程1 写入出错: %s', e)
        return

    # 全部完成：删除各案件的 md 缓存（防止下次误恢复）
    for _name, _path, _pdfs in subdirs:
        _ocr_md_remove(_ocr_md_path(_path))

    # 清理空子文件夹
    for name, path, pdfs in subdirs:
        try:
            remaining = glob.glob(os.path.join(path, '*'))
            if not remaining:
                os.rmdir(path)
        except Exception:
            pass

    print(f'\n{G}{"═" * 50}{RST}')
    print(f'{G}  批量处理完成！{RST}')
    print(f'{G}{"═" * 50}{RST}')
    logging.info('流程1 完成')
