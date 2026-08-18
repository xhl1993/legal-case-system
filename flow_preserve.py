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
# 流程6：保全裁定收文归档
# ====================================================================


def _get_all_cases():
    """获取全部案件行（远程 API 版），返回 [dict,...]"""
    return _api_get_all('案件情况表') or []


def _norm_party(name):
    """单个当事人名称归一化：去括号/去常见前缀后缀/深装别名，用于模糊匹配。
    例：'深圳市建筑装饰（集团）有限公司' → '深装集团'
        '亿尔莱（天津）新材料科技有限公司' → '亿尔莱新材料科技'"""
    s = re.sub(r'[（(][^）)]*[）)]', '', name or '')
    s = s.replace(' ', '').replace('\u3000', '')
    # 深装集团别名归一（全称/简称统一）
    if '建筑装饰' in s or '深装' in s:
        return '深装集团'
    # 去掉身份前缀（上诉/被上诉等）
    s = re.sub(r'^(被上诉人|上诉人|被告|原告|申请人|被申请人|第三人)[:：]?', '', s)
    # 去掉常见尾部后缀
    s = re.sub(r'(有限公司|有限责任公司|股份有限公司|集团|公司|经营部|服务部|经销部|建材店|事务所|合作社)$', '', s)
    return s[:8]


def _norm_parties(name):
    """把当事人字段拆分为多个当事人，各自归一化，返回归一化集合。
    支持'、'、'，'、','、';'、空格分隔的多被告，如'深装集团、臻悦公司'。"""
    parts = re.split(r'[、，,;；\s]+', name or '')
    out = set()
    for p in parts:
        p = p.strip()
        if p:
            out.add(_norm_party(p))
    return out


def _case_no_core(case_no):
    """提取案号核心片段（如 深国仲受5628 / 民初21942），用于包含匹配。
    兼容数据库案号带 -N 后缀或拼接多个案号的情况。"""
    s = normalize_case_no(case_no).replace('(', '').replace(')', '')
    m = re.search(r'[\u4e00-\u9fff]{2,}?\d+(?:-\d+)?号?', s)
    return m.group(0) if m else s


def _match_case_by_preserve(info):
    """三级匹配案件：
    1) 本案案号（caseNo）：精确匹配（未结案优先→全表）→ 失败回退案号核心片段包含匹配
    2) 保全号（财保/执保号）在案号字段中包含匹配
    3) 当事人交叉匹配：申请人必须命中，被申请人加分，仅唯一最高分才自动命中
    返回 (db_id, row, 匹配方式)，失败返回 (None, None, '')"""
    # --- 一级：本案案号 ---
    case_no = normalize_case_no(info.get('caseNo', '') or '')
    if case_no:
        db_id, row = db_check_duplicate_unclosed(case_no)
        if not db_id:
            _, db_id, row = db_find_case_in_all_tables(case_no)
        if not db_id:
            # 一级补充：案号核心片段包含匹配（兼容 5628号-3 这类带后缀记录）
            core = _case_no_core(case_no)
            if core:
                hits = []
                for r in _get_all_cases():
                    stored = str(_row_get(r, '案号') or '').replace(' ', '').replace('\u3000', '')
                    if core in stored:
                        hits.append(r)
                if len(hits) == 1:
                    db_id, row = _row_id(hits[0]), hits[0]
        if db_id:
            return db_id, row, '案号'

    # --- 二级：保全号包含匹配（数据库案号字段常拼接多个案号） ---
    pno = normalize_case_no(info.get('preserveNo', '') or '')
    m = re.search(r'\d+', pno)
    num = m.group(0) if m else ''
    if num:
        rows = _get_all_cases()
        hits = []
        for r in rows:
            stored = str(_row_get(r, '案号') or '').replace(' ', '').replace('\u3000', '')
            if num in stored and ('执保' in stored or '财保' in stored or '保' in stored):
                hits.append(r)
        if len(hits) == 1:
            return _row_id(hits[0]), hits[0], '保全号'
        if not hits:
            # 放宽：纯数字包含匹配，唯一才命中
            hits2 = [r for r in rows if num in str(_row_get(r, '案号') or '').replace(' ', '').replace('\u3000', '')]
            if len(hits2) == 1:
                return _row_id(hits2[0]), hits2[0], '保全号'

    # --- 三级：当事人交叉匹配（多被告集合交集，区分共同被告案件） ---
    pl_set = _norm_parties(info.get('plaintiff', '') or '')
    df_set = _norm_parties(info.get('defendant', '') or '')
    if pl_set or df_set:
        scored = []
        for r in _get_all_cases():
            rpl = _norm_parties(_row_get(r, '原告'))
            rdf = _norm_parties(_row_get(r, '被告'))
            score = 0
            if pl_set & rpl:            # 申请人必须命中
                score += 2
            inter = df_set & rdf         # 被申请人交集元素数（深装是共同被告，其他被告可区分）
            if inter:
                score += min(len(inter), 2)
            if score >= 2:
                scored.append((score, r))
        if scored:
            scored.sort(key=lambda x: -x[0])
            top_score = scored[0][0]
            tops = [r for s, r in scored if s == top_score]
            if len(tops) == 1:  # 仅唯一最高分才自动命中，多个候选交给人工
                return _row_id(tops[0]), tops[0], '当事人'
    return None, None, ''


def _build_progress_note(info, receive_date):
    """生成保全裁定进展记录文字"""
    parts = [f'【{receive_date} 保全裁定】']
    raw_pno = str(info.get('preserveNo', '') or '').strip()
    raw_pno = re.sub(r'\s+', '、', raw_pno)  # 多个保全号用顿号分隔
    pno = normalize_case_no(raw_pno)
    cno = normalize_case_no(info.get('caseNo', '') or '')
    if pno:
        parts.append(f'保全号{pno}')
    if cno:
        parts.append(f'本案案号{cno}')
    amt = str(info.get('preserveAmount', '') or '').strip()
    if amt:
        parts.append(f'保全金额{amt}元')
    judge = str(info.get('judge', '') or '').strip()
    contact = str(info.get('judgeContact', '') or '').strip()
    if judge:
        parts.append(f'{judge}')
    if contact:
        parts.append(f'联系方式{contact}')
    deadline = str(info.get('preserveDeadline', '') or '').strip()
    if deadline:
        parts.append(f'保全期限至{deadline}')
    if len(parts) == 1:
        return parts[0]
    return parts[0] + '，' + '，'.join(parts[1:])


def _preserve_report_path(folder, receive_date):
    """Markdown 结果报告路径（脚本同目录）"""
    tag = hashlib.md5(folder.encode('utf-8')).hexdigest()[:8]
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        f'保全裁定_结果_{receive_date.replace("-", "")}.md')


def _preserve_export_report(entries, folder, receive_date):
    """把识别/处理结果导出为 Markdown 报告，方便直接查看或存档"""
    path = _preserve_report_path(folder, receive_date)
    ok = sum(1 for e in entries if e.get('status') == 'ok')
    warn = sum(1 for e in entries if e.get('status') == 'warning')
    err = sum(1 for e in entries if e.get('status') == 'error')
    done = sum(1 for e in entries if e.get('written') or e.get('archived'))
    lines = [
        f'# 保全裁定处理结果汇总（{receive_date}）',
        '',
        f'- 来源文件夹：`{folder}`',
        f'- 生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        f'- 共 {len(entries)} 项：✅{ok}  ⚠️{warn}  ❌{err}  ｜  已完成写入/归档 {done}/{len(entries)}',
        '',
        '| 序号 | 状态 | 文书类型 | 保全号 | 本案案号 | 匹配序号 | 申请人 | 保全金额 | 备注 |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- |',
    ]
    for e in entries:
        icon = '✅' if e.get('status') == 'ok' else ('⚠️' if e.get('status') == 'warning' else '❌')
        note = '；'.join(e.get('notes', []) or [])
        if e.get('written'):
            note = (note + '；' if note else '') + '已写入进展'
        if e.get('archived'):
            note = (note + '；' if note else '') + '已归档'
        lines.append(f"| {e.get('idx', '')} | {icon} | {e.get('doc_type', '')} | {e.get('preserve_no', '')} | "
                     f"{e.get('case_no', '')} | {e.get('match_seq', '')} | {e.get('plaintiff', '')} | "
                     f"{e.get('amount', '')} | {note} |")
    lines.append('')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        print(f'  {C}📄 结果报告: {path}{RST}')
        return path
    except Exception as ex:
        print(f'  {Y}⚠ 报告写入失败: {ex}{RST}')
        return None


def _preserve_identify_loop(pdfs):
    """流程6识别阶段：逐个文件 OCR + DeepSeek 提取 + 三级匹配。返回 entries（未持久化）。
    单独成函数以便断点恢复时直接跳过本阶段。"""
    entries = []
    for idx, pdf in enumerate(pdfs):
        pdf_name = os.path.basename(pdf)
        print(f'\n{"━" * 55}')
        print(f'  [{idx+1}/{len(pdfs)}] {BOLD}{pdf_name}{RST}')
        print(f'{"━" * 55}')

        entry = {
            'idx': idx + 1,
            'pdf_path': pdf,
            'pdf_name': pdf_name,
            'status': 'warning',
            'dup_id': None,
            'dup_row': None,
            'match_level': '',
            'notes': [],
            'info': {},
            'match_seq': '',
            'written': False,
            'archived': False,
        }

        # --- 断点恢复：该文件已有识别缓存（上次中断）则直接复用，跳过重新OCR ---
        md_path = _ocr_md_path(pdf)
        if _ocr_md_try_resume(entry, md_path):
            # 优先沿用缓存中的匹配（match_seq=人工确认/上次匹配的序号，dup_id 次之）
            if entry.get('match_seq'):
                db_id, row = _find_case_by_seq(entry['match_seq'])
                if db_id:
                    entry['dup_id'], entry['dup_row'] = db_id, row
                    entry['status'] = 'ok'
            if not entry.get('dup_row'):
                db_id, row, level = _match_case_by_preserve(entry.get('info') or {})
                if db_id:
                    entry['dup_id'], entry['dup_row'], entry['match_level'] = db_id, row, level
                    entry['status'] = 'ok'
                    entry['match_seq'] = _row_get(row, '案件序号')
                    print(f'  {G}✅ 重新匹配成功(方式:{level}): 序号{entry["match_seq"]}{RST}')
                elif not entry.get('notes'):
                    entry['status'] = 'warning'
                    entry['notes'].append('未匹配到案件')
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
            print(f'  {R}OCR失败{RST}')
            entries.append(entry)
            continue

        # DeepSeek 提取
        info = {}
        try:
            info = call_deepseek(PROMPT_PRESERVE, text)
            print('  DeepSeek提取成功')
        except Exception as e:
            entry['notes'].append(f'DeepSeek失败: {e}')
            print(f'  {R}DeepSeek失败: {e}{RST}')
        entry['info'] = info

        preserve_no = normalize_case_no(info.get('preserveNo', '') or '')
        case_no = normalize_case_no(info.get('caseNo', '') or '')
        doc_type = info.get('docType', '') or ''
        if not preserve_no and not case_no:
            entry['status'] = 'error'
            entry['notes'].append('未识别到保全号/案号')
            print(f'  {R}❌ 未识别到保全号/案号{RST}')
            entries.append(entry)
            continue

        # 三级匹配
        db_id, row, level = _match_case_by_preserve(info)
        if db_id:
            entry['dup_id'] = db_id
            entry['dup_row'] = row
            entry['match_level'] = level
            entry['status'] = 'ok'
            seq = _row_get(row, '案件序号')
            print(f'  {G}✅ 匹配成功(方式:{level}): 序号{seq}{RST}')
        else:
            entry['status'] = 'warning'
            entry['notes'].append('未匹配到案件')
            print(f'  {Y}⚠ 未匹配到案件，将归档到未分类{RST}')

        entry['match_seq'] = _row_get(entry.get('dup_row'), '案件序号') if entry.get('dup_row') else ''
        # 识别完成 → 写 md 缓存（断点续跑：中断后下次直接复用，无需重新OCR）
        _ocr_md_save(entry, md_path)
        entries.append(entry)
    return entries


def process_preserve_docs():
    """流程6：保全裁定收文归档（执保/财保统一处理）"""
    print('\n' + '═' * 60)
    print('  流程6：保全裁定收文归档（执保/财保统一处理）')
    print('  模式：OCR → 提取 → 三级匹配 → 写案件进展 + 归档')
    print('═' * 60)
    logging.info('流程6 开始（保全裁定收文归档）')

    folder = CLI_FOLDER or PRESERVE_FOLDER
    if not folder or not os.path.exists(folder):
        if AUTO_MODE:
            print(f'{R}错误：文件夹不存在 - {folder}{RST}')
            logging.error('流程6 文件夹不存在: %s', folder)
            return
        folder = input('请输入保全裁定PDF所在文件夹路径：').strip().strip('"').strip("'")
    if not os.path.exists(folder):
        print(f'{R}错误：文件夹不存在 - {folder}{RST}')
        logging.error('流程6 文件夹不存在: %s', folder)
        return
    logging.info('流程6 处理文件夹: %s', folder)

    pdfs = sorted(glob.glob(os.path.join(folder, '*.pdf')))
    if not pdfs:
        print(f'{R}文件夹中没有PDF文件：{folder}{RST}')
        return

    print(f'\n扫描到 {len(pdfs)} 个PDF文件：')
    for i, pdf in enumerate(pdfs):
        print(f'  {i+1}. {os.path.basename(pdf)}')

    if not yes_no(f'\n开始批量处理 {len(pdfs)} 个文件？', True):
        return

    receive_date = smart_date_input('\n请输入收文日期（所有文件共用）', datetime.now().strftime('%Y-%m-%d'))

    # --- 识别：有 md 缓存（上次中断）的文件自动复用，跳过重复 OCR ---
    entries = _preserve_identify_loop(pdfs)

    # --- 汇总表 ---
    for e in entries:
        e['preserve_no'] = normalize_case_no(e.get('info', {}).get('preserveNo', '') or '')
        e['case_no'] = normalize_case_no(e.get('info', {}).get('caseNo', '') or '')
        e['doc_type'] = e.get('info', {}).get('docType', '') or '保全裁定'
        e['match_seq'] = _row_get(e['dup_row'], '案件序号') if e.get('dup_row') else '未找到'
        e['plaintiff'] = e.get('info', {}).get('plaintiff', '')
        e['amount'] = e.get('info', {}).get('preserveAmount', '')

    # --- Markdown 汇总报告（识别结果，处理完成后会更新最终状态） ---
    _preserve_export_report(entries, folder, receive_date)

    columns = [
        ('idx', '序号', 4),
        ('doc_type', '文书类型', 10),
        ('preserve_no', '保全号', 22),
        ('case_no', '本案案号', 24),
        ('match_seq', '匹配序号', 10),
        ('plaintiff', '申请人', 16),
        ('amount', '保全金额', 12),
    ]
    print_summary_table(entries, columns, '流程6 保全裁定处理结果汇总', notes_width=30)

    # --- 人工确认：允许按案件序号手动补匹配 ---
    if not AUTO_MODE:
        print(f'\n{BOLD}--- 人工确认 ---{RST}')
        print(f'  对未匹配的记录可输入案件序号手动匹配')
        print(f'  {DIM}（回车跳过=确认无误）{RST}')
        for e in entries:
            if e.get('status') == 'error':
                continue
            if e.get('dup_row'):
                val = input(f'  [{e["idx"]}] {e["pdf_name"]} → 匹配序号{_row_get(e["dup_row"], "案件序号")}（回车确认，或输入新序号）：').strip()
            else:
                val = input(f'  [{e["idx"]}] {e["pdf_name"]} → 未匹配（输入案件序号手动匹配，回车跳过）：').strip()
            if val:
                db_id, row = _find_case_by_seq(val)
                if db_id:
                    e['dup_id'] = db_id
                    e['dup_row'] = row
                    e['status'] = 'ok'
                    e['notes'] = [n for n in e.get('notes', []) if '未匹配' not in n]
                    e['notes'].append(f'手动匹配序号{val}')
                    print(f'  {G}  ✓ 已匹配序号{val}{RST}')
                else:
                    print(f'  {R}  ✗ 不存在案件序号 {val}，保持原状{RST}')
            # 每条确认后立即更新 md 缓存，防止后续中断丢失人工匹配结果
            e['match_seq'] = _row_get(e['dup_row'], '案件序号') if e.get('dup_row') else ''
            _ocr_md_save(e, _ocr_md_path(e['pdf_path']))

    # --- 写入：案件进展(追加) + 归档 ---
    print(f'\n{BOLD}--- 写入案件进展 + 归档 ---{RST}')
    for e in entries:
        # 已完全处理（已归档且进展已写/本就无需写）→ 跳过，避免重复追加进展、重复移动文件
        if e.get('archived') and (e.get('written') or e.get('status') == 'error' or not e.get('dup_row')):
            print(f'  {DIM}⏭ 已完成（上次处理成功，跳过）: {e["pdf_name"]}{RST}')
            continue

        # 分支A：处理失败 / 未匹配 → 仅归档（单条失败不中断整个批次）
        if e.get('status') == 'error' or not e.get('dup_row'):
            label = '处理失败' if e.get('status') == 'error' else '未匹配'
            if e.get('archived'):
                print(f'  {DIM}⏭ 已完成（上次已归档，跳过）: {e["pdf_name"]}{RST}')
                continue
            print(f'  {Y}⚠ 归档: {e["pdf_name"]} ({label}){RST}')
            try:
                archive_file(e['pdf_path'], '', receive_date, '保全裁定')
                e['archived'] = True
                print(f'  {G}  ✓ 已归档{RST}')
            except Exception as ex:
                print(f'  {R}✗ 归档失败: {e["pdf_name"]}: {ex}{RST}')
                print(f'    {Y}文件可能被占用，请关闭占用它的程序后重新运行本脚本继续{RST}')
                logging.exception('流程6 归档失败(%s)', label)
            _ocr_md_save(e, _ocr_md_path(e['pdf_path']))
            continue

        # 分支B：已匹配 → 写案件进展 + 归档
        note = _build_progress_note(e.get('info', {}), receive_date)
        if not e.get('written'):
            old_progress = _row_get(e['dup_row'], '案件进展') or ''
            new_progress = f'{old_progress}；{note}' if old_progress else note
            try:
                db_update_case(e['dup_id'], {'案件进展': new_progress})
            except Exception as ex:
                print(f'  {R}✗ 进展写入失败 序号{_row_get(e["dup_row"], "案件序号")}: {ex}{RST}')
                logging.exception('流程6 进展写入失败')
            else:
                e['written'] = True
                seq = _row_get(e['dup_row'], '案件序号')
                print(f'  {G}✅ 已写入进展: 序号{seq} {note}{RST}')
        if e.get('archived'):
            print(f'  {DIM}⏭ 文件已归档（上次成功）: {e["pdf_name"]}{RST}')
        else:
            seq = _row_get(e['dup_row'], '案件序号')
            try:
                archive_file(e['pdf_path'], seq, receive_date, '保全裁定')
                e['archived'] = True
                print(f'  {G}  ✓ 已归档{RST}')
            except Exception as ex:
                print(f'  {R}✗ 归档失败: {e["pdf_name"]}: {ex}{RST}')
                print(f'    {Y}文件可能被占用，请关闭占用它的程序后重新运行本脚本继续{RST}')
                logging.exception('流程6 归档失败(已匹配)')
        _ocr_md_save(e, _ocr_md_path(e['pdf_path']))

    # --- 全部完成：删除各文件的 md 缓存（防止下次误恢复），导出最终报告 ---
    for _pdf in pdfs:
        _ocr_md_remove(_ocr_md_path(_pdf))
    _preserve_export_report(entries, folder, receive_date)

    print(f'\n{G}  批量处理完成！{RST}')
    print(f'{G}{"═" * 50}{RST}')
    logging.info('流程6 完成')
