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
# 流程2：判决书/裁决书批处理
# ====================================================================


def identify_judgment_type(filename):
    name = os.path.splitext(filename)[0]
    if '劳动仲裁' in name or '劳仲' in name:
        return '劳动仲裁裁决书'
    if '商事仲裁' in name or '仲裁裁决' in name:
        return '商事仲裁裁决书'
    if '裁决书' in name:
        return '裁决书'
    if '判决书' in name:
        return '判决书'
    for kw in ['判决', '裁定']:
        if kw in name:
            return kw + '书'
    return '判决书/裁决书'


def process_judgments():
    print('\n' + '═' * 60)
    print('  流程2：判决书/裁决书批量处理 V5（火山引擎OCR）')
    print('  模式：批量OCR → 数据库匹配 → 汇总 → 纠错 → 统一更新')
    print('═' * 60)
    logging.info('流程2 开始（判决书/裁决书批处理）')

    folder = CLI_FOLDER or JUDGMENT_FOLDER
    if not folder or not os.path.exists(folder):
        if AUTO_MODE:
            print(f'{R}错误：文件夹不存在 - {folder}{RST}')
            logging.error('流程2 文件夹不存在: %s', folder)
            return
        folder = input('请输入判决书/裁决书PDF所在文件夹路径：').strip().strip('"').strip("'")
    if not os.path.exists(folder):
        print(f'{R}错误：文件夹不存在 - {folder}{RST}')
        logging.error('流程2 文件夹不存在: %s', folder)
        return
    logging.info('流程2 处理文件夹: %s', folder)

    pdfs = sorted(glob.glob(os.path.join(folder, '*.pdf')))
    if not pdfs:
        print(f'{R}文件夹中没有PDF文件：{folder}{RST}')
        return

    print(f'\n扫描到 {len(pdfs)} 个PDF文件：')
    for i, pdf in enumerate(pdfs):
        ftype = identify_judgment_type(os.path.basename(pdf))
        print(f'  {i+1}. [{ftype}] {os.path.basename(pdf)}')

    if not yes_no(f'\n开始批量处理 {len(pdfs)} 个文件？', True):
        return

    receive_date = smart_date_input('\n请输入收文日期（完结时间，所有文件共用）', datetime.now().strftime('%Y-%m-%d'))

    # --- 批量OCR + DeepSeek ---
    entries = []

    for idx, pdf in enumerate(pdfs):
        pdf_name = os.path.basename(pdf)
        doc_type = identify_judgment_type(pdf_name)

        print(f'\n{"━" * 55}')
        print(f'  [{idx+1}/{len(pdfs)}] {BOLD}{pdf_name}{RST}')
        print(f'{"━" * 55}')

        entry = {
            'idx': idx + 1,
            'pdf_path': pdf,
            'pdf_name': pdf_name,
            'doc_type': doc_type,
            'status': 'warning',
            'case_no': '',
            'plaintiff': '',
            'defendant': '',
            'court': '',
            'judgment_date': '',
            'judgment_result': '',
            'need_payment': False,
            'payment_amount': '',
            'receive_date': receive_date,
            'appeal_deadline': '',
            'dup_id': None,
            'dup_row': None,
            'notes': [],
            'info': {},
            'match_seq': '',
            'written': False,
            'archived': False,
        }

        # --- 断点恢复：该文件已有识别缓存（上次中断）则直接复用，跳过重新OCR ---
        md_path = _ocr_md_path(pdf)
        if _ocr_md_try_resume(entry, md_path):
            # 从 info 重建关键字段 + 重新计算审级/上诉截止日
            entry['case_no'] = entry.get('info', {}).get('caseNo', '')
            entry['plaintiff'] = entry.get('info', {}).get('plaintiff', '')
            entry['defendant'] = entry.get('info', {}).get('defendant', '')
            entry['court'] = entry.get('info', {}).get('court', '')
            entry['judgment_date'] = entry.get('info', {}).get('judgmentDate', '')
            entry['judgment_result'] = entry.get('info', {}).get('judgmentResult', '')
            entry['need_payment'] = to_bool(entry.get('info', {}).get('needPayment', False))
            entry['payment_amount'] = parse_amount(entry.get('info', {}).get('paymentAmount', '')) or ''
            if not entry.get('doc_type'):
                entry['doc_type'] = entry.get('info', {}).get('docType', '') or identify_judgment_type(pdf_name)
            # 匹配：优先缓存 match_seq（人工确认/上次匹配），其次 dup_id，否则重新查库
            if entry.get('match_seq'):
                db_id, row = _find_case_by_seq(entry['match_seq'])
                if db_id:
                    entry['dup_id'], entry['dup_row'] = db_id, row
                    entry['status'] = 'ok'
            if not entry.get('dup_row'):
                cn = entry.get('case_no') or ''
                if cn:
                    db_id, row = db_check_duplicate_unclosed(cn)
                    if not db_id:
                        _, db_id, row = db_find_case_in_all_tables(cn)
                    if db_id:
                        entry['dup_id'], entry['dup_row'] = db_id, row
                        entry['status'] = 'ok'
                        entry['notes'] = [n for n in entry.get('notes', []) if '未找到' not in n and '重新匹配' not in n]
                        entry['notes'].append(f'重新匹配: 序号{_row_get(row, "案件序号")}')
                        print(f'  {G}✅ 重新匹配成功: 序号{_row_get(row, "案件序号")}{RST}')
            entry['_db_id'] = entry.get('dup_id')
            inst = _resolve_instance_level(entry.get('case_no', ''), entry.get('doc_type', ''))
            entry['instance_level'] = inst
            entry['appeal_deadline'] = _calc_appeal_deadline(entry.get('judgment_date', ''), inst)
            entries.append(entry)
            continue

        # OCR
        try:
            total_pages = get_pdf_page_count(pdf)
        except Exception as e:
            entry['status'] = 'error'
            entry['notes'].append(f'无法打开PDF: {e}')
            print(f'  {R}无法打开PDF: {e}{RST}')
            entries.append(entry)
            continue

        ocr_mode = 'all' if total_pages <= 4 else 'smart'
        text = ocr_pdf_safe(pdf, mode=ocr_mode)
        if not text:
            entry['status'] = 'error'
            entry['notes'].append('OCR失败')
            entries.append(entry)
            continue

        # DeepSeek
        info = {}
        try:
            info = call_deepseek(PROMPT_JUDGMENT, text)
            print(f'  DeepSeek提取成功')
        except Exception as e:
            entry['notes'].append(f'DeepSeek失败: {e}')
            print(f'  {R}DeepSeek失败: {e}{RST}')

        entry['info'] = info
        entry['case_no'] = info.get('caseNo', '')
        entry['plaintiff'] = info.get('plaintiff', '')
        entry['defendant'] = info.get('defendant', '')
        entry['court'] = info.get('court', '')
        entry['judgment_date'] = info.get('judgmentDate', '')
        entry['judgment_result'] = info.get('judgmentResult', '')
        entry['need_payment'] = to_bool(info.get('needPayment', False))
        entry['payment_amount'] = parse_amount(info.get('paymentAmount', '')) or ''
        entry['doc_type'] = info.get('docType', doc_type)

        # 数据库匹配（流程2：判决书/裁决书，匹配所有状态的案件）
        if not entry['case_no']:
            entry['status'] = 'error'
            entry['notes'].append('案号未识别')
            print(f'  {R}❌ 案号未识别{RST}')
            entries.append(entry)
            continue

        # 先在未结案/排期中找
        dup_id, dup_row = db_check_duplicate_unclosed(entry['case_no'])
        if not dup_id:
            # 在所有表中找（已结案等其他状态也能匹配）
            table_name, found_id, found = db_find_case_in_all_tables(entry['case_no'])
            if found:
                dup_id = found_id
                dup_row = found
                entry['notes'].append(f'匹配到(状态:{_row_get(found, "案件状态")})')
                print(f'  {Y}⚠ 案号在{_row_get(found, "案件状态")}表中: 序号{_row_get(found, "案件序号")}，继续处理{RST}')
            else:
                entry['notes'].append('数据库中未找到')
                print(f'  {Y}⚠ 数据库中未找到此案号{RST}')
                entry['status'] = 'warning'
                entries.append(entry)
                continue

        entry['dup_id'] = dup_id
        entry['dup_row'] = dup_row

        # 通过案号+文书类型判断程序类型 + 计算上诉截止日
        instance_level = _resolve_instance_level(entry.get('case_no', ''), entry.get('doc_type', ''))
        entry['instance_level'] = instance_level
        entry['appeal_deadline'] = _calc_appeal_deadline(entry.get('judgment_date', ''), instance_level)
        if instance_level == '商事仲裁':
            entry['notes'].append('商事仲裁：上诉截止日留空，需人工按6个月撤销期处理')

        entry['status'] = 'ok'
        seq = _row_get(dup_row, '案件序号')
        pl = _row_get(dup_row, '原告')
        df = _row_get(dup_row, '被告')
        print(f'  {G}✅ 匹配成功: 序号{seq} {pl} vs {df}{RST}')

        entry['match_seq'] = _row_get(entry.get('dup_row'), '案件序号') if entry.get('dup_row') else ''
        entry['written'] = False
        entry['archived'] = False
        # 识别完成 → 写 md 缓存（断点续跑：中断后下次直接复用）
        _ocr_md_save(entry, md_path)
        entries.append(entry)

    # --- 汇总表 ---
    columns = [
        ('idx', '序号', 4),
        ('case_no', '案号', 28),
        ('dup_id', '匹配序号', 8),
        ('plaintiff', '原告', 16),
        ('defendant', '被告', 14),
        ('judgment_date', '判决日期', 12),
        ('judgment_result', '判决结果', 30),
    ]
    # 调整dup_id显示（用案件序号覆盖，但保留原始dup_id到_db_id）
    for e in entries:
        if e.get('dup_row'):
            e['_db_id'] = e.get('dup_id')  # 保留数据库行ID
            e['dup_id'] = _row_get(e['dup_row'], '案件序号')
        else:
            e['dup_id'] = '未找到'

    print_summary_table(entries, columns, '流程2 批量处理结果汇总', notes_width=28)

    # --- 纠错 ---
    print(f'\n{BOLD}--- 人工纠错 ---{RST}')
    print(f'  输入序号可修改对应判决书的字段')
    print(f'  {DIM}（回车跳过=确认无误，直接写入）{RST}')

    fields_config = [
        ('dup_id', '匹配序号', False),
        ('case_no', '案号', False),
        ('instance_level', '审级(一审/二审)', False),
        ('judgment_date', '判决日期', False),
        ('judgment_result', '判决结果', False),
        ('receive_date', '收文日期', False),
        ('need_payment', '需付款', False),
        ('payment_amount', '付款金额', False),
    ]
    entries = interact_correct(entries, fields_config)

    # --- 纠错后：更新 md 缓存（保留用户修改的匹配序号/字段，中断后可续） ---
    for e in entries:
        if e.get('pdf_path'):
            e['match_seq'] = str(e.get('dup_id', '')).strip() if e.get('dup_id') and str(e.get('dup_id', '')).strip() != '未找到' else ''
            _ocr_md_save(e, _ocr_md_path(e['pdf_path']))

    # --- 纠错后：重新解析用户手动修改的 dup_id（案件序号）→ 补全 _db_id 和 dup_row ---
    conn_tmp = None  # 远程 API 模式无需本地连接
    for e in entries:
        user_dup_id = str(e.get('dup_id', '')).strip()
        if not user_dup_id or user_dup_id == '未找到':
            continue
        # 如果 _db_id 已经存在且 dup_row 中的案件序号与当前 dup_id 一致，则无需重查
        existing_seq = _row_get(e.get('dup_row') or {}, '案件序号')
        if str(existing_seq) == user_dup_id and e.get('_db_id'):
            continue
        # 用户修改了 dup_id，需要重新查库
        print(f'  {B}[重查] 按案件序号 {user_dup_id} 查找数据库...{RST}')
        try:
            db_id, row = _find_case_by_seq(user_dup_id, conn_tmp)
            if db_id:
                e['_db_id'] = db_id
                e['dup_row'] = row
                e['status'] = 'ok'
                e['notes'] = [n for n in e.get('notes', []) if '未找到' not in n and '匹配' not in n]
                e['notes'].append(f'手动匹配序号{user_dup_id}')
                # 重新计算上诉截止日（用案号+文书类型判断程序类型）
                inst = e.get('instance_level', '') or _resolve_instance_level(e.get('case_no', ''), e.get('doc_type', ''))
                e['appeal_deadline'] = _calc_appeal_deadline(e.get('judgment_date', ''), inst)
                print(f'  {G}  ✓ 找到：序号{user_dup_id} → 行ID {db_id}{RST}')
            else:
                print(f'  {R}  ✗ 数据库中不存在案件序号 {user_dup_id}{RST}')
        except Exception as _ex:
            print(f'  {R}  查库异常: {_ex}{RST}')

    # --- 去重：同一案件序号只保留一个（用户可能上传了同一案件的多个PDF） ---
    seen_dbd_ids = set()
    deduped_entries = []
    dup_removed = 0
    for e in entries:
        db_id = e.get('_db_id')
        if db_id and e.get('status') == 'ok':
            if db_id in seen_dbd_ids:
                dup_removed += 1
                continue
            seen_dbd_ids.add(db_id)
        deduped_entries.append(e)
    if dup_removed > 0:
        print(f'  {Y}⚠ 已删除重复匹配结果 {dup_removed} 条（同一案件多个PDF）{RST}')
    entries = deduped_entries

    # --- 写入 ---
    print(f'\n{BOLD}--- 确认写入 ---{RST}')
    ok_count = sum(1 for e in entries if e['status'] == 'ok')
    warn_count = sum(1 for e in entries if e['status'] == 'warning')
    err_count = sum(1 for e in entries if e['status'] == 'error')

    print(f'  将更新 {G}{ok_count}{RST} 条到已结案')
    if warn_count:
        print(f'  需确认 {Y}{warn_count}{RST} 条（案号匹配失败，仅归档文件）')
    if err_count:
        print(f'  失败跳过 {R}{err_count}{RST} 条')

    if not yes_no('\n确认更新数据库并归档文件？', True):
        print('已取消')
        return

    for entry in entries:
        pdf = entry['pdf_path']
        if not os.path.exists(pdf):
            continue
        # 上次已归档（中断恢复）→ 跳过，避免重复移动文件
        if entry.get('archived'):
            print(f'  {DIM}⏭ 已归档（上次成功，跳过）: {entry["pdf_name"]}{RST}')
            continue

        # 优先取 entry 内用户可能在纠错中修改过的收文日期
        entry_receive_date = entry.get('receive_date') or receive_date

        if entry['status'] == 'error':
            print(f'  {R}❌ 跳过: {entry["pdf_name"]} (处理失败){RST}')
            # 归档到未分类
            archive_file(pdf, '', entry_receive_date, entry['doc_type'])
            entry['archived'] = True
            _ocr_md_save(entry, _ocr_md_path(pdf))
            continue

        if entry['status'] == 'warning' and not entry.get('_db_id'):
            print(f'  {Y}⚠ 归档: {entry["pdf_name"]} (案号未匹配){RST}')
            archive_file(pdf, '', entry_receive_date, entry['doc_type'])
            entry['archived'] = True
            _ocr_md_save(entry, _ocr_md_path(pdf))
            continue

        if entry['status'] == 'ok' and entry.get('_db_id'):
            dup_row = entry.get('dup_row', {})
            case_no = entry.get('case_no', '')
            judgment_result = entry.get('judgment_result', '')
            judgment_date = entry.get('judgment_date', '')
            appeal_deadline = entry.get('appeal_deadline', '')

            # 通过案号判断审级，优先用纠错阶段用户确认的 instance_level
            instance_level = entry.get('instance_level', '') or _detect_instance_level(case_no)

            is_second_instance = (instance_level == '二审')

            updates = {
                '案件状态': '已结案',
                '收到判决时间': entry_receive_date,
                '上诉时间截止日': appeal_deadline,
                '案件进展': f'【{entry_receive_date} 结案】{judgment_result}',
            }
            if is_second_instance:
                updates['二审案号'] = case_no
                updates['二审判决内容'] = judgment_result
            else:
                updates['判决内容（一审、二审）'] = judgment_result

            db_update_case(entry['_db_id'], updates)
            entry['written'] = True
            print(f'  {G}✅ 已结案: 序号{_row_get(dup_row, "案件序号")} {case_no}{" (二审)" if is_second_instance else ""}{RST}')

            archive_file(pdf, _row_get(dup_row, '案件序号'), entry_receive_date, entry['doc_type'])
            entry['archived'] = True
            _ocr_md_save(entry, _ocr_md_path(pdf))

            # 付款登记提示
            if entry.get('need_payment') and entry.get('payment_amount'):
                if yes_no(f'    判决需付款 {entry["payment_amount"]}元，是否登记未付款计划？', False):
                    register_unpaid_batch(case_no, dup_row, entry)

        elif entry['status'] == 'warning':
            # 用户可能在纠错中补了dup信息
            print(f'  {Y}⚠ 归档: {entry["pdf_name"]} (匹配未解决){RST}')
            archive_file(pdf, '', receive_date, entry['doc_type'])
            entry['archived'] = True
            _ocr_md_save(entry, _ocr_md_path(pdf))

    # 全部完成：删除 md 缓存（防止下次误恢复）
    for _pdf in pdfs:
        _ocr_md_remove(_ocr_md_path(_pdf))

    # 清理空文件夹
    try:
        remaining = glob.glob(os.path.join(folder, '*'))
        if not any(True for r in remaining if '_ocr_tmp' not in os.path.basename(r)):
            pass  # 不删判决书文件夹
    except Exception:
        pass

    print(f'\n{G}{"═" * 50}{RST}')
    print(f'{G}  批量处理完成！{RST}')
    print(f'{G}{"═" * 50}{RST}')
    logging.info('流程2 完成')


def register_unpaid_batch(case_no, case_row, entry):
    """批量模式下的付款登记"""
    amount = confirm_input('    付款金额', str(entry.get('payment_amount', '')))
    if not amount:
        return
    pay_deadline = confirm_input('    最迟付款日期', '')
    # 用 _row_get 兼容本地dict和远程API两种格式
    related_unit = confirm_input('    涉及单位（收款方）', entry.get('plaintiff', '') or _row_get(case_row, '原告'))
    project = confirm_input('    涉及项目', _row_get(case_row, '涉及项目'))
    project_manager = confirm_input('    项目负责人', _row_get(case_row, '项目负责人'))

    new_unpaid = {
        '登记日期': datetime.now().strftime('%Y-%m-%d'),
        '案件序号': _row_get(case_row, '案件序号'),
        '案号': case_no,
        '最迟付款日期': pay_deadline,
        '金额': amount,
        '涉及单位': related_unit,
        '涉及项目': project,
        '项目负责人': project_manager,
    }
    db_insert_unpaid(new_unpaid)
    print(f'    {G}✅ 已登记未付款计划，金额：{amount}元{RST}')
