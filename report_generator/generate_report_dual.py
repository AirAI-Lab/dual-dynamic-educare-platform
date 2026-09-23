# -*- coding: utf-8 -*-
"""
即时-平均 双动督学报表生成器（已更新）

主要功能（最新逻辑）：
- 从 Excel/CSV（默认查找 data/inputs，下同）读取原始实时成绩，删除导出表的第一行并把第二行作为列名；支持 .xlsx/.xls/.csv 格式。
- 自动识别信息列（学号、所属学院、所属行政班、姓名）和观测指标（第5-7列），并从列名中提取分母（满分/共发放页数/共开课次数）。
- 计算即时指标：课程百分得分（course_percent）、课件阅读系数（view_coeff）、公告阅读系数（sign_coeff）、动态学习成绩（dynamic_score）。
- 滚动平均计算流程：先读取历史滚动表（data/inputs/average 下最新文件）；再读取本次 realtime（中文列名 CSV）；用本次即时列覆盖历史的最后一列并重命名为“M月D日即时成绩”；从第4列起对各即时成绩列（历史列+本次）逐行求平均并追加“M月D日平均动态成绩”。
- 输出文件（按日期命名，示例 11-26）：
    - data/reports/real-time/{MM-DD}_即时成绩.csv/.png（中文列名的即时详表与图片）
    - data/reports/real-time/{MM-DD}_即时成绩_匿名.csv/.png（匿名版，首列为“编号”，不包含学号/姓名）
    - data/reports/average/{MM-DD}_平均动态成绩.csv/.png（按日保存的平均表与图片）
    - data/reports/average/{MM-DD}_平均动态成绩_匿名.csv/.png（匿名版，首列为“编号”）
    - data/reports/mappings/{MM-DD}_mapping.csv（当天使用的匿名映射；若存在则读取以加速生成）

隐私与映射规则：
- 匿名码为固定随机种子（123456）下生成的 6 位字母+数字码，保证同一输入在同一环境下可重现；映射以当日 `{MM-DD}_mapping.csv` 为准，若存在则直接读取，不会重生新的码。

着色规则（图片）：
- 即时报表：按动态学习成绩分段着色（>=80 绿，>=60 蓝，否则红）；即时表仍保留对课件/公告阅读系数单元格的特殊标红逻辑（小于 1 时单元格标显著红色）。
- 平均报表（含匿名版）：按最后一列（优先使用当天生成的平均列名，如“M月D日平均动态成绩”，否则取表格最后一列）分段着色（>=80 绿，>=60 蓝，否则红），不再对阅读/签到系数做单元格级别的强制红色处理。

其它要点：
- 支持同时读取/保存 CSV 与 Excel 格式；数值取整规则已统一：历史与计算后的平均均取整并以整数保存；平均保存时按平均值降序。
- 匿名 CSV/PNG 仅包含一列编号作为唯一标识（列名：`编号`），不包含 `学号` 或 `姓名`，以满足隐私要求。

用法示例：
python generate_report_dual.py --input data/inputs/realtime.xlsx --prev data/inputs/previous_average.csv --outdir data/reports
"""
# ---------- 最近更新（便于维护 / 快速参考） ----------
# 2025-12-18:
# - 将若干运行时控制从环境变量迁移为命令行参数，以便更明确、可脚本化地控制本次运行：
#     --test-date YYYY-MM-DD   （可选，默认为系统当前日期）
#     --keep-prefix PREFIXES   （可选，逗号分隔，例如 "25" 或 "23,24"；默认为保留所有学生）
#     --remove-bottom N        （可选，指定从报表底部剔除 N 名；默认为不剔除）
# - 原先文档中提到的环境变量 TEST_DATE / REPORT_KEEP_PREFIX / REPORT_REMOVE_BOTTOM
#   已不再作为首选控制方式；为保持向后兼容，脚本在某些早期分支仍会回退到环境变量，但
#   推荐使用上面的命令行参数以获得确定性行为。
# - 读取原始 realtime 表头时会对列名做轻量规范化：如果列名中包含子串 "签到次数",
#   会把该子串替换为 "阅读公告数"（保留列名中其它字符，例如括号与数字），以统一后续列名处理。
# - 匿名映射：匿名码使用固定随机种子 123456，生成 6 位大写字母+数字码；当日映射文件优先读取以保证稳定性。
# 使用示例：
# python generate_report_dual.py --test-date 2025-12-09 --keep-prefix 25 --remove-bottom 2
# -----------------------------------------------

from __future__ import annotations
import argparse
from pathlib import Path
import re
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
import random
import string
import os
from datetime import datetime


def _get_today_from_env():
    """返回当前日期的 datetime 对象（以前可能支持 TEST_DATE 环境变量，现默认使用系统时间）。"""
    return datetime.now()


# 中文字体检测
def choose_chinese_font(preferred=('Microsoft YaHei', 'SimHei', 'Arial Unicode MS')):
    try:
        sys_fonts = {font_manager.FontProperties(fname=f).get_name(): f for f in font_manager.findSystemFonts()}
    except Exception:
        return None
    for name in preferred:
        if name in sys_fonts:
            return name
    for name in sys_fonts:
        nl = name.lower()
        if 'yahei' in nl or 'simhei' in nl or 'unicode' in nl:
            return name
    return None


ch_font = choose_chinese_font()
if ch_font:
    plt.rcParams['font.sans-serif'] = [ch_font]
plt.rcParams['axes.unicode_minus'] = False


def create_anonymized_mapping(ids, names=None, prefix='ANON'):
    """为一组学号（ids）和可选姓名(names)生成匿名码映射表。
    返回 mapping_df (columns: student_id, name (if provided), 匿名码) 和 dict id->code
    映射按 ids 出现顺序生成稳定但可预测的代码（ANON0001...）。"""
    ids = [str(x) for x in ids]
    # 保持原顺序且去重
    seen = {}
    uniq = []
    for x in ids:
        if x not in seen:
            seen[x] = True
            uniq.append(x)
    # generate 6-char alphanumeric codes using a fixed seed for reproducibility
    rng = random.Random(123456)
    mapping = []
    id_to_code = {}
    existing = set()
    for sid in uniq:
        # generate deterministic 6-char code
        for _ in range(10000):
            code = ''.join(rng.choices(string.ascii_uppercase + string.digits, k=6))
            if code not in existing:
                break
        existing.add(code)
        id_to_code[sid] = code
        mapping.append({'student_id': sid, 'code': code})
    if names is not None:
        # attach names in original order (may be shorter/longer)
        for sid, name in zip(ids, names):
            for row in mapping:
                if row['student_id'] == sid:
                    row['姓名'] = name
                    break
    mapping_df = pd.DataFrame(mapping)
    # attach names if provided
    if names is not None:
        name_list = list(names)
        for i, sid in enumerate(uniq):
            if i < len(name_list):
                mapping_df.loc[mapping_df['student_id'] == sid, 'name'] = name_list[i]
            else:
                mapping_df.loc[mapping_df['student_id'] == sid, 'name'] = ''
    else:
        mapping_df['name'] = [''] * len(mapping_df)
    return mapping_df[['student_id','code','name']], id_to_code


def load_or_create_perdate_mapping(mapping_dir: Path, today, ids, names=None, prefix: str = ''):
    """Load per-date mapping if exists, otherwise create and save it.
    Returns (mapping_df, code_map) where mapping_df has columns ['student_id','code','name'] and code_map maps id->code.
    """
    mapping_dir.mkdir(parents=True, exist_ok=True)
    prefix_part = f"{prefix}_" if prefix else ''
    map_name = f"{prefix_part}{today.month:02d}-{today.day:02d}_mapping.csv"
    map_path = mapping_dir / map_name
    if map_path.exists():
        try:
            m = pd.read_csv(str(map_path), dtype=str, encoding='utf-8-sig')
        except Exception:
            m = pd.read_csv(str(map_path), dtype=str)
        m.columns = [str(c) for c in m.columns]
        # try to normalize columns to student_id, code, name
        if 'code' not in m.columns and '匿名码' in m.columns:
            m = m.rename(columns={'匿名码': 'code'})
        if 'name' not in m.columns and '姓名' in m.columns:
            m = m.rename(columns={'姓名': 'name'})
        if 'student_id' not in m.columns and '学号' in m.columns:
            m = m.rename(columns={'学号': 'student_id'})
        # ensure columns
        if 'student_id' not in m.columns:
            # fallback: take first col as id
            m = m.rename(columns={m.columns[0]: 'student_id'})
        if 'code' not in m.columns:
            # create placeholder codes if missing
            m['code'] = [''] * len(m)
        if 'name' not in m.columns:
            m['name'] = [''] * len(m)
        code_map = m.set_index('student_id')['code'].to_dict()
        return m[['student_id','code','name']], code_map
    else:
        # create mapping and save as standardized file
        mapping_df, id_to_code = create_anonymized_mapping(ids, names)
        # mapping_df may have columns student_id, 匿名码, 姓名
        code_col = mapping_df.columns[mapping_df.columns.str.contains('匿名|code', regex=True)][0] if any(mapping_df.columns.str.contains('匿名|code', regex=True)) else None
        name_col = mapping_df.columns[mapping_df.columns.str.contains('姓名|name', regex=True)][0] if any(mapping_df.columns.str.contains('姓名|name', regex=True)) else None
        std = pd.DataFrame()
        std['student_id'] = mapping_df['student_id'].astype(str)
        if code_col is not None:
            std['code'] = mapping_df[code_col].astype(str)
        else:
            std['code'] = [id_to_code.get(s,'') for s in std['student_id']]
        if name_col is not None:
            std['name'] = mapping_df[name_col].astype(str)
        else:
            std['name'] = [''] * len(std)
        try:
            std.to_csv(str(map_path), index=False, encoding='utf-8-sig')
        except Exception:
            std.to_csv(str(map_path), index=False)
        return std, std.set_index('student_id')['code'].to_dict()
            

def find_col(cols, pattern_list):
    """在列名中按包含任意模式字符串来查找列名，返回第一个匹配列名或None"""
    for p in pattern_list:
        for c in cols:
            if p in c:
                return c
    return None


def extract_number(s):
    if not isinstance(s, str):
        s = str(s)
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


def safe_divide(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    with np.errstate(divide='ignore', invalid='ignore'):
        out = np.where((b == 0) | np.isnan(b), 0.0, a / b)
    return out


def _gen_code(existing_codes: set, length: int = 6) -> str:
    """Generate a unique alphanumeric code of given length not in existing_codes."""
    alphabet = string.ascii_uppercase + string.digits
    # use global random as fallback
    for _ in range(10000):
        code = ''.join(random.choices(alphabet, k=length))
        if code not in existing_codes:
            return code
    raise RuntimeError('无法生成唯一匿名码')


def create_or_update_mapping(mapping_dir: Path, id_name_df: pd.DataFrame) -> pd.DataFrame:
    """Ensure mapping master exists and update with any new student_id/name pairs.

    id_name_df: DataFrame with columns ['student_id','name'] (both as strings)
    Returns the full mapping DataFrame with columns ['student_id','name','code']
    """
    mapping_dir.mkdir(parents=True, exist_ok=True)
    master_path = mapping_dir / 'mapping_master.csv'
    # normalize incoming
    id_name_df = id_name_df.copy()
    id_name_df['student_id'] = id_name_df['student_id'].astype(str)
    if 'name' not in id_name_df.columns:
        id_name_df['name'] = ''

    if master_path.exists():
        try:
            master = pd.read_csv(str(master_path), dtype=str, encoding='utf-8-sig')
        except Exception:
            master = pd.read_csv(str(master_path), dtype=str)
        master.columns = [str(c) for c in master.columns]
    else:
        master = pd.DataFrame(columns=['student_id', 'name', 'code'])

    # Ensure types
    master['student_id'] = master['student_id'].astype(str)
    master['name'] = master['name'].astype(str)
    existing_codes = set(master['code'].dropna().astype(str).tolist())

    # For each id,name pair, if not exist in master (by student_id), add
    to_add = []
    existing_ids = set(master['student_id'].tolist())
    for _, row in id_name_df[['student_id','name']].drop_duplicates().iterrows():
        sid = str(row['student_id'])
        nm = str(row['name'])
        if sid not in existing_ids:
            code = _gen_code(existing_codes)
            existing_codes.add(code)
            to_add.append({'student_id': sid, 'name': nm, 'code': code})
            existing_ids.add(sid)

    if to_add:
        master = pd.concat([master, pd.DataFrame(to_add)], ignore_index=True)
        try:
            master.to_csv(str(master_path), index=False, encoding='utf-8-sig')
        except Exception:
            master.to_csv(str(master_path), index=False)

    # Return mapping for all ids in master (as DataFrame)
    return master


def process_realtime_excel(path: Path):
    # 读取 sheet1 或 CSV：先不把任一行当作 header，删除第一行（meta），
    # 然后将原文件的第二行作为列名（按用户要求）
    # 支持 .xlsx/.xls/.csv
    suffix = str(path.suffix).lower()
    if suffix == '.csv':
        # CSV: 以无 header 的方式读取，保持与 Excel 读取行为一致
        try:
            x = pd.read_csv(str(path), header=None, encoding='utf-8')
        except Exception:
            x = pd.read_csv(str(path), header=None)
    else:
        x = pd.read_excel(str(path), sheet_name=0, header=None)
    if len(x) <= 2:
        raise ValueError('输入文件行数不足')

    # 删除原始第一行（通常是导出时的说明行）
    x2 = x.iloc[1:].reset_index(drop=True)
    # 现在 x2 的第一行应为列名行，后续为数据
    # 读取原始第2行作为列名（保留原始文本，不做将“签到次数”替换为“阅读公告数”的破坏性修改）
    header_row_raw = x2.iloc[0].astype(str).tolist()
    header_row = header_row_raw[:]  # 保持原样，用于后续的 DataFrame 列名设置
    data = x2.iloc[1:].reset_index(drop=True)
    data.columns = header_row

    cols = list(data.columns)
    # 按列位置匹配：前4列为信息列（学号、所属学院、所属行政班、姓名），5-7列为观测指标
    student_col = cols[0] if len(cols) >= 1 else None
    college_col = cols[1] if len(cols) >= 2 else None
    class_col = cols[2] if len(cols) >= 3 else None
    name_col = cols[3] if len(cols) >= 4 else None

    total_score_col = cols[4] if len(cols) >= 5 else None
    viewed_pages_col = cols[5] if len(cols) >= 6 else None

    # 不再使用固定位置 cols[6]。严格查找原始列名中包含子串“阅读公告数”的列并使用它作为 sign_col；
    # 注意：我们使用 header_row_raw（未做替换的原始列名）来匹配，这样不会把原来的“签到次数”误识别为“阅读公告数”。
    sign_col = None
    for idx, raw_name in enumerate(header_row_raw):
        if '阅读公告数' in str(raw_name):
            # 使用对应位置的 header_row 中的列名（保持与 DataFrame 列名一致）
            sign_col = header_row[idx]
            break
    # 如果未找到 '阅读公告数'，严格报错（不再回退到 '签到' 或更宽泛的匹配）
    if sign_col is None:
        raise ValueError('ERR_NO_SIGN_COL: 在输入表的第2行列名中未找到包含 "阅读公告数" 的列，请检查导出模板或先将列名标准化为包含该子串。')

    # 从这三列的列名中提取满分/总页数/开课次数（作为各指标的分母）
    full_score = extract_number(total_score_col) if total_score_col is not None else None
    total_pages = extract_number(viewed_pages_col) if viewed_pages_col is not None else None
    total_signs = extract_number(sign_col) if sign_col is not None else None

    # 打印解析到的三个分母，便于运行时即时确认（DEBUG 信息）
    try:
        print(f"解析分母: full_score={full_score}, total_pages={total_pages}, total_signs={total_signs}")
    except Exception:
        # 防止打印过程抛出异常影响主流程
        pass

    # 若未能从列名提取，抛出错误并带错误码，便于上层处理
    if full_score is None or total_pages is None or total_signs is None:
        raise ValueError('ERR_NO_DENOMINATOR: 无法从第5-7列列名中提取分母（满分/共发放页数/开课次数），请检查原始表的第2行列名是否包含数字。')

    # 重命名相关列为标准名
    mapping = {}
    if student_col:
        mapping[student_col] = 'student_id'
    if name_col:
        mapping[name_col] = 'name'
    if college_col:
        mapping[college_col] = 'college'
    if class_col:
        mapping[class_col] = 'class'
    if total_score_col:
        mapping[total_score_col] = 'total_score'
    if viewed_pages_col:
        mapping[viewed_pages_col] = 'viewed_pages'
    if sign_col:
        mapping[sign_col] = 'sign_count'

    df = data.rename(columns=mapping)

    # 去除重复列名（保留第一次出现的列）以避免后续按列名索引返回 DataFrame 而非 Series
    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated()]

    # Ensure required columns present
    required = ['student_id','name','total_score','viewed_pages','sign_count']
    for c in required:
        if c not in df.columns:
            df[c] = 0

    # Convert numeric: 如果列名重复导致返回 DataFrame，取第一列再转换
    def _to_numeric_first(col):
        val = df[col]
        if isinstance(val, pd.DataFrame):
            val = val.iloc[:, 0]
        return pd.to_numeric(val, errors='coerce').fillna(0).astype(float)

    df['total_score'] = _to_numeric_first('total_score')
    df['viewed_pages'] = _to_numeric_first('viewed_pages')
    df['sign_count'] = _to_numeric_first('sign_count')

    # Add constants columns
    df['full_score'] = full_score
    df['total_pages'] = total_pages
    df['total_signs'] = total_signs

    # Keep info columns order
    info_cols = [c for c in ['student_id','college','class','name'] if c in df.columns]
    # 原始第5-7列的列名（未重命名，用于保存 selected CSV 时保留原列名）
    orig_metric_cols = []
    if total_score_col:
        orig_metric_cols.append(total_score_col)
    else:
        orig_metric_cols.append('total_score')
    if viewed_pages_col:
        orig_metric_cols.append(viewed_pages_col)
    else:
        orig_metric_cols.append('viewed_pages')
    if sign_col:
        orig_metric_cols.append(sign_col)
    else:
        orig_metric_cols.append('sign_count')

    return df, info_cols, orig_metric_cols


def compute_immediate(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['course_percent'] = safe_divide(df['total_score'], df['full_score']) * 100.0
    df['view_coeff'] = safe_divide(df['viewed_pages'], df['total_pages'])
    df['sign_coeff'] = safe_divide(df['sign_count'], df['total_signs'])
    df['dynamic_score'] = df['course_percent'] * df['view_coeff'] * df['sign_coeff']
    df['dynamic_score'] = df['dynamic_score'].replace([np.inf, -np.inf], 0).fillna(0)
    return df


def merge_previous_average(df: pd.DataFrame, prev_path: Path) -> pd.DataFrame:
    # prev CSV expected columns: student_id, prev_average
    if prev_path is None or not getattr(prev_path, 'exists', lambda: False)():
        df['prev_average'] = np.nan
        df['current_average'] = df['dynamic_score']
        return df
    # 支持 CSV 或 Excel 格式的上一轮平均表
    try:
        if prev_path.suffix.lower() in ['.xlsx', '.xls']:
            prev = pd.read_excel(str(prev_path))
        else:
            try:
                prev = pd.read_csv(str(prev_path), dtype={})
            except Exception:
                prev = pd.read_csv(str(prev_path), encoding='utf-8-sig')
    except Exception:
        # 如果读取失败，抛出以便上层处理
        raise
    # find column with avg
    cols = prev.columns
    avg_col = None
    for c in cols:
        if 'avg' in c.lower() or '平均' in c or 'average' in c.lower():
            avg_col = c
            break
    if avg_col is None and len(cols) >= 2:
        avg_col = cols[1]
    prev = prev.rename(columns={avg_col: 'prev_average'}) if avg_col else prev
    if 'student_id' not in prev.columns:
        # try first column as id
        prev = prev.rename(columns={prev.columns[0]: 'student_id'})
    # Ensure student_id columns have the same dtype to avoid merge errors (string preferable)
    # 强制将 id 列转换为字符串（处理缺失值），避免因类型不一致导致 merge 报错
    try:
        df = df.copy()
        df['student_id'] = df['student_id'].map(lambda x: '' if pd.isna(x) else str(x))
    except Exception:
        pass
    try:
        prev['student_id'] = prev['student_id'].map(lambda x: '' if pd.isna(x) else str(x))
    except Exception:
        pass
    merged = pd.merge(df, prev[['student_id','prev_average']], on='student_id', how='left')
    merged['prev_average'] = pd.to_numeric(merged['prev_average'], errors='coerce')
    # 对读取后的历史平均成绩进行取整（保留缺失值）
    merged['prev_average'] = merged['prev_average'].round(0)
    # current average as simple mean of prev_average and dynamic_score when prev exists, otherwise dynamic_score
    merged['current_average'] = np.where(merged['prev_average'].isna(), merged['dynamic_score'], (merged['prev_average'] + merged['dynamic_score'])/2.0)
    # 对计算出的平均值也进行取整并使用 nullable Int64 保存
    merged['current_average'] = pd.to_numeric(merged['current_average'], errors='coerce').round(0)
    try:
        merged['prev_average'] = merged['prev_average'].astype('Int64')
    except Exception:
        pass
    try:
        merged['current_average'] = merged['current_average'].astype('Int64')
    except Exception:
        pass
    return merged


def color_table_and_save(df: pd.DataFrame, info_cols, out_png: Path, max_rows=1000, display_columns: list | None = None, orig_metric_cols: list | None = None):
    """生成彩色表格图片。display_columns 可以传入最终展示的列名（支持中文或英文列名）。
    着色规则：按动态成绩分段着色（>=80 绿，>=60 蓝，否则红），若课件阅读系数或公告阅读系数<1，则对应单元格标红。
    """
    # 如果未指定展示列，使用默认英文列组合
    if display_columns is None:
        observed = ['total_score','full_score','viewed_pages','total_pages','sign_count','total_signs']
        computed = ['course_percent','view_coeff','sign_coeff','dynamic_score']
        display_columns = info_cols + observed + computed

    # 保留存在的列
    display_columns = [c for c in display_columns if c in df.columns]
    use_df = df[display_columns].copy()

    # 查找动态/阅读/签到列（兼容中文）
    dyn_candidates = ['dynamic_score', '动态学习成绩']
    view_candidates = ['view_coeff', '课件阅读系数']
    sign_candidates = ['sign_coeff', '公告阅读系数']

    def _find(cands):
        for c in cands:
            if c in use_df.columns:
                return c
        return None

    dyn_col = _find(dyn_candidates)
    view_col = _find(view_candidates)
    sign_col = _find(sign_candidates)

    # 按动态成绩降序
    if dyn_col:
        use_df = use_df.sort_values(dyn_col, ascending=False).reset_index(drop=True)

    disp = use_df.copy()
    # 通用格式化函数，兼容不存在的列
    def _fmt(col, fn):
        if col and col in disp.columns:
            try:
                disp[col] = disp[col].map(lambda x: fn(x))
            except Exception:
                pass

    # dynamic score display as integer
    _fmt(dyn_col, lambda x: ("" if pd.isna(x) else f"{int(round(float(x)))}"))
    # 课程百分得分保留两位小数（兼容中文/英文列名）
    _fmt('course_percent', lambda x: ("" if pd.isna(x) else f"{float(x):.2f}"))
    _fmt('课程百分得分', lambda x: ("" if pd.isna(x) else f"{float(x):.2f}"))
    # 课件/公告阅读系数保留两位小数
    _fmt(view_col, lambda x: ("" if pd.isna(x) else f"{float(x):.2f}"))
    _fmt(sign_col, lambda x: ("" if pd.isna(x) else f"{float(x):.2f}"))
    # total_score keep one decimal as before
    _fmt('total_score', lambda x: ("" if pd.isna(x) else f"{float(x):.1f}"))
    # 如果传入了原始观测列名，则把观看总页数与签到次数在图片中显示为整数
    if orig_metric_cols:
        try:
            # orig_metric_cols: [orig_total_score_name, orig_viewed_pages_name, orig_sign_name]
            if len(orig_metric_cols) >= 2 and orig_metric_cols[1] in disp.columns:
                _fmt(orig_metric_cols[1], lambda x: ("" if pd.isna(x) else f"{int(round(float(x)))}"))
            if len(orig_metric_cols) >= 3 and orig_metric_cols[2] in disp.columns:
                _fmt(orig_metric_cols[2], lambda x: ("" if pd.isna(x) else f"{int(round(float(x)))}"))
        except Exception:
            pass

    rows = min(len(disp), max_rows)
    table_vals = disp.head(rows).values.tolist()
    col_labels = disp.columns.tolist()

    # prepare colors
    nrows = len(table_vals)
    ncols = len(col_labels)
    cell_colors = [['white' for _ in range(ncols)] for _ in range(nrows)]

    # helpers to find index in col_labels
    def _idx(cands):
        for c in cands:
            if c in col_labels:
                return col_labels.index(c)
        return None

    idx_dyn = _idx(dyn_candidates)
    idx_view = _idx(view_candidates)
    idx_sign = _idx(sign_candidates)

    for i in range(nrows):
        dyn = float(use_df.iloc[i][dyn_col]) if dyn_col and dyn_col in use_df.columns else 0.0
        view_c = float(use_df.iloc[i][view_col]) if view_col and view_col in use_df.columns else 0.0
        sign_c = float(use_df.iloc[i][sign_col]) if sign_col and sign_col in use_df.columns else 0.0
        if dyn >= 80:
            row_color = '#b7e4c7'
        elif dyn >= 60:
            row_color = '#a0c4ff'
        else:
            row_color = '#ffadad'
        for j in range(ncols):
            cell_colors[i][j] = row_color
        if view_c < 1 and idx_view is not None:
            cell_colors[i][idx_view] = '#ff6b6b'
        if sign_c < 1 and idx_sign is not None:
            cell_colors[i][idx_sign] = '#ff6b6b'
        # 课程百分得分低于60的单元格标红（无论行色如何覆盖）
        idx_course = _idx(['course_percent', '课程百分得分'])
        if idx_course is not None:
            try:
                cp_val = float(use_df.iloc[i][_find(['course_percent','课程百分得分'])]) if _find(['course_percent','课程百分得分']) in use_df.columns else None
            except Exception:
                cp_val = None
            if cp_val is not None and not pd.isna(cp_val) and float(cp_val) < 60:
                cell_colors[i][idx_course] = '#ff6b6b'

    # dynamic figure sizing
    fig_w = min(max(8, ncols * 1.1), 28)
    # height should scale with rows but remain reasonable
    fig_h = min(max(2.5, 0.5 * (nrows + 3)), 40)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.axis('off')

    # dynamic font size and scaling
    # base font decreases with number of columns and rows
    try:
        base_font = int(max(8, min(14, 180.0 / max(1, (ncols * (nrows ** 0.5))))))
    except Exception:
        base_font = 10
    # column and row scale
    col_scale = max(1.0, min(2.2, ncols / 8.0))
    row_scale = max(0.8, min(2.0, 30.0 / max(5, nrows)))

    table = ax.table(cellText=table_vals, colLabels=col_labels, cellColours=cell_colors, loc='center', cellLoc='center')
    table.auto_set_font_size(False)
    table.set_fontsize(base_font)
    table.scale(col_scale, row_scale)
    # 自动调整列宽，减少左右和上下空白
    try:
        table.auto_set_column_width(col=list(range(len(col_labels))))
    except Exception:
        pass

    # style header row (-1 in matplotlib table coordinates)
    header_bg = '#2f4f4f'
    header_fg = 'white'
    for j, label in enumerate(col_labels):
        # header cell may be at row -1 or 0 depending on matplotlib; search for it
        header_cell = None
        for (r, c), cell in table.get_celld().items():
            if c == j and (r == -1 or r == 0):
                header_cell = cell
                break
        if header_cell is not None:
            header_cell.set_facecolor(header_bg)
            # set header text properties
            try:
                header_cell.get_text().set_weight('bold')
                header_cell.get_text().set_color(header_fg)
            except Exception:
                pass

    # add light borders and alignment
    for (r, c), cell in table.get_celld().items():
        # 网格线（cell 边框）——确保所有单元格都有浅灰色边框
        cell.set_edgecolor('#999999')
        # 稍微加粗以便在图片中清晰可见
        cell.set_linewidth(0.8)
        # left-align info columns for readability
        if c < len(info_cols):
            try:
                cell.get_text().set_ha('left')
            except Exception:
                pass

    # reduce margins
    plt.subplots_adjust(left=0.01, right=0.99, top=0.98, bottom=0.02)
    # draw and compute table bbox to tightly crop
    fig.canvas.draw()
    try:
        renderer = fig.canvas.get_renderer()
        tbbox = table.get_window_extent(renderer)
        # convert window bbox (display coords) to inches for savefig
        bbox_inches = tbbox.transformed(fig.dpi_scale_trans.inverted())
        # expand slightly (2%) in inches
        bbox_inches = bbox_inches.expanded(1.02, 1.02)
        fig.savefig(str(out_png), dpi=200, bbox_inches=bbox_inches, pad_inches=0.02)
    except Exception:
        # fallback to tight
        fig.savefig(str(out_png), dpi=200, bbox_inches='tight', pad_inches=0.02)
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(description='生成即时-平均双动督学报表')
    p.add_argument('--input', '-i', default=None, help='原始实时成绩 Excel 文件（可省略，脚本会在 data/inputs 中自动查找匹配的 Excel 文件）')
    p.add_argument('--prev', '-p', default=None, help='上一次平均成绩（CSV，含 student_id 和 平均列；可省略，脚本会在 data/inputs 中自动查找）')
    p.add_argument('--outdir', '-o', default='data/reports', help='输出目录')
    p.add_argument('--remove-bottom', type=int, default=None, help='从报表底部剔除 N 名（若不提供则不剔除）')
    p.add_argument('--test-date', default=None, help='测试用的日期，格式 YYYY-MM-DD（默认为当前系统日期）')
    p.add_argument('--keep-prefix', default=None, help='按学号前缀保留记录，逗号分隔，例如 "25" 或 "23,24"（默认保留所有学生）')
    args = p.parse_args()

    inputs_dir = Path('data/inputs')
    inp = Path(args.input) if args.input else None
    prev = Path(args.prev) if args.prev else None

    # 自动在 data/inputs 中查找 realtime Excel（若用户未提供或指定文件不存在）
    if inp is None or not inp.exists():
        # 优先在 data/inputs/real-time 下查找最新文件（只在该子目录中搜索），以避免误选到其它历史/平均表
        rt_dir = inputs_dir / 'real-time'
        sel = None
        if rt_dir.exists():
            rt_cand = list(rt_dir.glob('*.xlsx')) + list(rt_dir.glob('*.xls')) + list(rt_dir.glob('*.csv'))
            sel = max(rt_cand, key=lambda p: p.stat().st_mtime) if rt_cand else None
        # 如果 real-time 目录没有文件，再回退到 data/inputs 根目录及其子目录的最新文件
        if sel is None and inputs_dir.exists():
            cand = list(inputs_dir.rglob('*.xlsx')) + list(inputs_dir.rglob('*.xls')) + list(inputs_dir.rglob('*.csv'))
            sel = max(cand, key=lambda p: p.stat().st_mtime) if cand else None
        if sel:
            inp = sel
    # 从输入文件名提取报表前缀（示例：文件名开头为 "25药生_..." 则前缀为 "25药生"），用于输出文件名
    report_prefix = ''
    try:
        if inp is not None:
            stem = inp.stem
            # split on common separators and take first token
            report_prefix = str(stem).split('_')[0].split(' ')[0].split('-')[0]
    except Exception:
        report_prefix = ''
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if inp is None or not inp.exists():
        raise FileNotFoundError(f"未找到原始实时成绩 Excel 文件，请将文件放到 data/inputs 或用 --input 指定。")

    # 1) 处理原始 realtime Excel，保存筛选后的原始表到 inputs/real-time/real_time_selected.csv
    df, info_cols, orig_metric_cols = process_realtime_excel(inp)
    real_time_inputs_dir = inputs_dir / 'real-time'
    real_time_inputs_dir.mkdir(parents=True, exist_ok=True)

    # 保存筛选后的表（信息列 + 3 个观测指标 + 常数列）
    # 先计算即时指标，再保存一个供人工查看的中文列名文件（用户要求）
    df_immediate = compute_immediate(df)

    # 可选：按学号前缀过滤（临时用以只保留特定年级，例如只留 '25' 级）
    # 优先使用命令行参数 --keep-prefix（逗号分隔）；若未提供则保留所有学生（不使用环境变量）
    keep_prefix = args.keep_prefix if getattr(args, 'keep_prefix', None) else None
    if keep_prefix:
        prefixes = [p.strip() for p in keep_prefix.split(',') if p.strip()]
        if prefixes:
            before_n = len(df_immediate)
            df_immediate = df_immediate[df_immediate['student_id'].astype(str).str.startswith(tuple(prefixes))].copy()
            after_n = len(df_immediate)
            print(f"已按学号前缀过滤：保留前缀 {prefixes} 的记录，{before_n}->{after_n} 行。")

    # 准备中文列名映射
    cn_map = {
        'student_id': '学号',
        'college': '所属学院',
        'class': '所属行政班',
        'name': '姓名',
        'total_score': '总分',
        'viewed_pages': '观看总页数',
        'sign_count': '阅读公告数',
        'full_score': '满分',
        'total_pages': '共发放页数',
        'total_signs': '共开课次数',
        'course_percent': '课程百分得分',
        'view_coeff': '课件阅读系数',
        'sign_coeff': '公告阅读系数'
    }

    # 按用户要求：直接将即时表以中文列名保存为 real_time_report，并用于生成彩色表格图片
    # info columns in Chinese
    info_cn = [cn_map.get(c, c) for c in info_cols]

    report_cn_df = pd.DataFrame()
    for en_col, cn_col in zip(info_cols, info_cn):
        report_cn_df[cn_col] = df_immediate[en_col]

    # append the three observed columns using original metric column names (保留原列名与数据)
    obs_map = {
        orig_metric_cols[0]: 'total_score',
        orig_metric_cols[1]: 'viewed_pages',
        orig_metric_cols[2]: 'sign_count'
    }
    for orig_name, std_col in obs_map.items():
        report_cn_df[orig_name] = df_immediate[std_col]

    # 计算指标（中文列名）
    report_cn_df['课程百分得分'] = safe_divide(df_immediate['total_score'], df_immediate['full_score']) * 100.0
    report_cn_df['课件阅读系数'] = safe_divide(df_immediate['viewed_pages'], df_immediate['total_pages'])
    report_cn_df['公告阅读系数'] = safe_divide(df_immediate['sign_count'], df_immediate['total_signs'])
    report_cn_df['动态学习成绩'] = df_immediate['dynamic_score']

    # 列顺序并排序
    ordered = []
    ordered.extend(info_cn)
    ordered.extend(orig_metric_cols)
    ordered.extend(['课程百分得分','课件阅读系数','公告阅读系数','动态学习成绩'])
    ordered = [c for c in ordered if c in report_cn_df.columns]
    report_cn_df = report_cn_df[ordered]
    report_cn_df = report_cn_df.sort_values('动态学习成绩', ascending=False).reset_index(drop=True)

    # 可选：临时剔除最后若干名次的记录（可通过命令行参数 --remove-bottom 指定；默认不剔除）
    remove_bottom = args.remove_bottom if getattr(args, 'remove_bottom', None) is not None else 0
    if remove_bottom and len(report_cn_df) > remove_bottom:
        before_n = len(report_cn_df)
        report_cn_df = report_cn_df.iloc[:-remove_bottom].reset_index(drop=True)
        # 同步剔除 df_immediate 中对应的学号以保持一致（以学号列为键）
        try:
            ids_kept = set(report_cn_df[cn_map.get('student_id', '学号')].astype(str).tolist()) if cn_map.get('student_id', '学号') in report_cn_df.columns else set()
            if ids_kept:
                df_immediate = df_immediate[df_immediate['student_id'].astype(str).isin(ids_kept)].copy()
        except Exception:
            pass
        after_n = len(report_cn_df)
        print(f"已剔除最后 {remove_bottom} 名（即时表）：{before_n}->{after_n} 行。")

    # 格式化要求：
    # - 课程百分得分保留两位小数
    # - 课件阅读系数、公告阅读系数保留两位小数
    # - 观看总页数与签到次数取整
    # - 动态学习成绩（即时成绩）取整
    try:
        report_cn_df['课程百分得分'] = report_cn_df['课程百分得分'].astype(float).round(2)
    except Exception:
        report_cn_df['课程百分得分'] = report_cn_df['课程百分得分'].map(lambda x: ("" if pd.isna(x) else float(x)))
    try:
        report_cn_df['课件阅读系数'] = report_cn_df['课件阅读系数'].astype(float).round(2)
    except Exception:
        report_cn_df['课件阅读系数'] = report_cn_df['课件阅读系数'].map(lambda x: ("" if pd.isna(x) else float(x)))
    try:
        report_cn_df['公告阅读系数'] = report_cn_df['公告阅读系数'].astype(float).round(2)
    except Exception:
        report_cn_df['公告阅读系数'] = report_cn_df['公告阅读系数'].map(lambda x: ("" if pd.isna(x) else float(x)))

    # 观看总页数与签到次数使用原始列名（orig_metric_cols），将其取整为整数类型（nullable Int64）以保留缺失值
    try:
        if len(orig_metric_cols) >= 2 and orig_metric_cols[1] in report_cn_df.columns:
            report_cn_df[orig_metric_cols[1]] = report_cn_df[orig_metric_cols[1]].round(0).astype('Int64')
        if len(orig_metric_cols) >= 3 and orig_metric_cols[2] in report_cn_df.columns:
            report_cn_df[orig_metric_cols[2]] = report_cn_df[orig_metric_cols[2]].round(0).astype('Int64')
    except Exception:
        # 退回为字符串化整数或空串
        try:
            if len(orig_metric_cols) >= 2 and orig_metric_cols[1] in report_cn_df.columns:
                report_cn_df[orig_metric_cols[1]] = report_cn_df[orig_metric_cols[1]].map(lambda x: ("" if pd.isna(x) else str(int(round(float(x))))) )
            if len(orig_metric_cols) >= 3 and orig_metric_cols[2] in report_cn_df.columns:
                report_cn_df[orig_metric_cols[2]] = report_cn_df[orig_metric_cols[2]].map(lambda x: ("" if pd.isna(x) else str(int(round(float(x))))) )
        except Exception:
            pass

    # 动态学习成绩取整
    try:
        report_cn_df['动态学习成绩'] = report_cn_df['动态学习成绩'].astype(float).round(0).astype('Int64')
    except Exception:
        report_cn_df['动态学习成绩'] = report_cn_df['动态学习成绩'].map(lambda x: ("" if pd.isna(x) else str(int(round(float(x))))) )

    # 作为后续计算的数据源：df_immediate
    df2 = df_immediate.copy()

    # 3) merge previous average (自动在 data/inputs 中查找 CSV)
    if prev is None or not prev.exists():
        prev = None
        if inputs_dir.exists():
            # 支持查找 .csv 和 .xlsx/.xls 作为上一次平均表
            prev_candidates = list(inputs_dir.rglob('*.csv')) + list(inputs_dir.rglob('*.xlsx')) + list(inputs_dir.rglob('*.xls'))
            # 选取最新修改的文件
            prev = max(prev_candidates, key=lambda p: p.stat().st_mtime) if prev_candidates else None

    merged = merge_previous_average(df2, prev)

    # 4) 输出到 reports 对应子目录
    out_real = outdir / 'real-time'
    out_avg = outdir / 'average'
    out_real.mkdir(parents=True, exist_ok=True)
    out_avg.mkdir(parents=True, exist_ok=True)

    # 使用按日期的文件命名：{MM-DD}_即时成绩(.csv/.png)
    # 支持通过命令行参数 --test-date 临时指定日期（格式 YYYY-MM-DD）；若未提供则使用系统当前日期
    if getattr(args, 'test_date', None):
        try:
            today = datetime.strptime(args.test_date, '%Y-%m-%d')
        except Exception:
            print(f"无法解析 --test-date={args.test_date}，将使用系统当前日期。")
            today = datetime.now()
    else:
        today = datetime.now()
    prefix_part = f"{report_prefix}_" if report_prefix else ''
    realtime_csv = out_real / f"{prefix_part}{today.month:02d}-{today.day:02d}_即时成绩.csv"
    img_real = out_real / f"{prefix_part}{today.month:02d}-{today.day:02d}_即时成绩.png"
    # 保存中文列名的即时成绩表（按日期命名）
    report_cn_df.to_csv(str(realtime_csv), index=False, encoding='utf-8-sig')

    # 生成并保存匿名版即时报表（使用按日期 mapping，若存在则读取，否则生成并保存）
    try:
        mapping_dir = Path('data') / 'reports' / 'mappings'
        ids = df_immediate['student_id'].astype(str).tolist()
        names = df_immediate['name'].astype(str).tolist() if 'name' in df_immediate.columns else None
        mapping_df, code_map = load_or_create_perdate_mapping(mapping_dir, today, ids, names, report_prefix)

        # 构建匿名报表：用 mapping 的 code 生成 '编号' 列，并删除学号/姓名列
        report_anon = report_cn_df.copy()
        id_col_cn = cn_map.get('student_id', '学号')
        name_col_cn = cn_map.get('name', '姓名')
        if id_col_cn in report_anon.columns:
            report_anon['编号'] = report_anon[id_col_cn].astype(str).map(lambda x: code_map.get(str(x), ''))
        else:
            if 'student_id' in df_immediate.columns:
                report_anon['编号'] = df_immediate['student_id'].astype(str).map(lambda x: code_map.get(str(x), ''))
            else:
                report_anon['编号'] = [''] * len(report_anon)
        # 删除学号/姓名列
        report_anon = report_anon.drop(columns=[c for c in [id_col_cn, name_col_cn] if c in report_anon.columns])
        # 将编号放首列
        cols = report_anon.columns.tolist()
        if '编号' in cols:
            cols = ['编号'] + [c for c in cols if c != '编号']
            report_anon = report_anon[cols]

        anon_csv = out_real / f"{prefix_part}{today.month:02d}-{today.day:02d}_即时成绩_匿名.csv"
        try:
            report_anon.to_csv(str(anon_csv), index=False, encoding='utf-8-sig')
        except Exception:
            report_anon.to_csv(str(anon_csv), index=False)

        # 保存原表图片
        try:
            color_table_and_save(report_cn_df, info_cn, img_real, display_columns=report_cn_df.columns.tolist(), orig_metric_cols=orig_metric_cols)
        except Exception:
            pass

        # 保存匿名图片
        anon_img = out_real / f"{prefix_part}{today.month:02d}-{today.day:02d}_即时成绩_匿名.png"
        try:
            info_cols_anon = ['编号'] + [c for c in info_cn if c in report_anon.columns and c not in [id_col_cn, name_col_cn]]
            color_table_and_save(report_anon, info_cols_anon, anon_img, display_columns=report_anon.columns.tolist(), orig_metric_cols=orig_metric_cols)
        except Exception:
            pass
    except Exception:
        # mapping creation failure should not stop main flow
        try:
            # fallback: still save original image
            color_table_and_save(report_cn_df, info_cn, img_real, display_columns=report_cn_df.columns.tolist(), orig_metric_cols=orig_metric_cols)
        except Exception:
            pass
    # 为平均计算准备排序后的 merged 表（按取整后的 current_average 倒序排列）
    merged_sorted = merged.copy()
    if 'current_average' in merged_sorted.columns:
        try:
            merged_sorted = merged_sorted.sort_values('current_average', ascending=False, na_position='last').reset_index(drop=True)
        except Exception:
            merged_sorted = merged_sorted.sort_values('dynamic_score', ascending=False).reset_index(drop=True)
    else:
        merged_sorted = merged_sorted.sort_values('dynamic_score', ascending=False).reset_index(drop=True)

    

    # 生成平均成绩表图片（简表）
    def save_avg_image(df_avg, path: Path):
        disp_cols = ['student_id'] + [c for c in info_cols if c!='student_id'] + ['prev_average','dynamic_score','current_average']
        disp = df_avg[disp_cols].copy()
        # 格式化数字
        if 'prev_average' in disp.columns:
            disp['prev_average'] = disp['prev_average'].map(lambda x: f"{float(x):.3f}" if not pd.isna(x) else '')
        disp['dynamic_score'] = disp['dynamic_score'].map(lambda x: f"{float(x):.3f}")
        disp['current_average'] = disp['current_average'].map(lambda x: f"{float(x):.3f}")
        rows = min(len(disp), 40)
        fig, ax = plt.subplots(figsize=(10, 0.5*(rows+3)))
        ax.axis('off')
        table = ax.table(cellText=disp.head(rows).values, colLabels=disp.columns, loc='center')
        table.auto_set_font_size(False)
        table.set_fontsize(10)
        table.scale(1, 1.2)
        plt.tight_layout()
        try:
            fig.savefig(str(path), dpi=120)
        except KeyboardInterrupt:
            print('生成平均成绩图片被中断（超时/过大），已跳过图片保存。')
        except Exception:
            try:
                fig.savefig(str(path))
            except Exception:
                print('保存平均成绩图片失败。')
        plt.close(fig)

    # 5) 生成/更新滚动平均动态成绩：读取 inputs/average 中的历史滚动表（若存在），
    #    将当前即时成绩覆盖历史表的最后一列，并将该列重命名为“xx即时成绩”，
    #    然后计算所有即时成绩列的平均并追加“xx平均动态成绩”，
    #    最后只把按日期命名的 CSV 和对应的 PNG 保存到 reports/average（out_dir）。
    def compute_and_save_rolling_average(inputs_avg_dir: Path, realtime_csv: Path, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)

        # 先读取历史平均表（在 inputs_avg_dir 下查找 .csv/.xlsx/.xls，选取最新文件作为历史基准）
        prev_files = []
        if inputs_avg_dir.exists():
            prev_files = list(inputs_avg_dir.glob('*.csv')) + list(inputs_avg_dir.glob('*.xlsx')) + list(inputs_avg_dir.glob('*.xls'))
            prev_files = sorted(prev_files, key=lambda p: p.stat().st_mtime)
        prev = None
        if prev_files:
            prev_path = prev_files[-1]
            try:
                if prev_path.suffix.lower() in ['.xlsx', '.xls']:
                    prev = pd.read_excel(str(prev_path))
                else:
                    try:
                        prev = pd.read_csv(str(prev_path), encoding='utf-8-sig')
                    except Exception:
                        prev = pd.read_csv(str(prev_path))
            except Exception:
                # 如果读取失败，保留 prev 为 None（后续分支处理）
                prev = None
            if prev is not None:
                prev.columns = [str(c) for c in prev.columns]

        # 读取 realtime 报表（中文列名的 real_time_report）
        # 读取 realtime 报表（支持 CSV 或 XLSX）
        realtime_path = Path(realtime_csv)
        try:
            if realtime_path.suffix.lower() in ['.xlsx', '.xls']:
                rt = pd.read_excel(str(realtime_path), dtype=str)
            else:
                try:
                    rt = pd.read_csv(str(realtime_path), encoding='utf-8-sig', dtype=str)
                except Exception:
                    rt = pd.read_csv(str(realtime_path), dtype=str)
        except Exception:
            # 如果读取 realtime 失败，抛出以便上层处理
            raise
        rt.columns = [str(c) for c in rt.columns]

        # 确定 realtime 的 id 列（优先 学号 / student_id），并取 realtime 表的最后一列作为即时成绩来源
        id_col_rt = None
        for c in ['学号', 'student_id']:
            if c in rt.columns:
                id_col_rt = c
                break
        if id_col_rt is None:
            id_col_rt = rt.columns[0]

        instant_src_col = rt.columns[-1]
        rt_instant = rt[[id_col_rt, instant_src_col]].drop_duplicates(subset=[id_col_rt]).copy()
        rt_instant.columns = ['student_id', 'instant_value']
        rt_instant['student_id'] = rt_instant['student_id'].astype(str)

        # 使用命令行参数 --test-date（优先）或默认当前系统日期作为 today
        if getattr(args, 'test_date', None):
            try:
                today = datetime.strptime(args.test_date, '%Y-%m-%d')
            except Exception:
                print(f"无法解析 --test-date={args.test_date}，将使用系统当前日期。")
                today = datetime.now()
        else:
            today = datetime.now()
        instant_col_name = f"{today.month}月{today.day}日即时成绩"
        avg_col_name = f"{today.month}月{today.day}日平均动态成绩"

        if prev is None:
            # 没有历史表：按要求不使用 realtime 结果，保存一个空的按日平均模板并给出提示图片
            out_dir.mkdir(parents=True, exist_ok=True)
            # 构造一个空的 prev_df 与用于保存的 prev_df_for_save（学号列中文名）
            prev_df = pd.DataFrame(columns=['student_id', avg_col_name])
            prev_df_for_save = prev_df.copy()
            if 'student_id' in prev_df_for_save.columns:
                prev_df_for_save = prev_df_for_save.rename(columns={'student_id': '学号'})
                # 若通过 CLI 指定 --remove-bottom，则对平均表剔除最后若干名次；默认不剔除
                remove_bottom = args.remove_bottom if getattr(args, 'remove_bottom', None) is not None else 0
                if remove_bottom:
                    try:
                        before_n = len(prev_df_for_save)
                        if len(prev_df_for_save) > remove_bottom:
                            prev_df_for_save = prev_df_for_save.iloc[:-remove_bottom].reset_index(drop=True)
                        # 同步对 prev_df（内部数据）也剔除
                        try:
                            if 'student_id' in prev_df.columns:
                                prev_df = prev_df[prev_df['student_id'].astype(str).isin(prev_df_for_save['学号'].astype(str))].copy()
                        except Exception:
                            pass
                        after_n = len(prev_df_for_save)
                        print(f"已剔除最后 {remove_bottom} 名（平均表）：{before_n}->{after_n} 行。")
                    except Exception:
                        pass

            csv_path = out_dir / f"{prefix_part}{today.month:02d}-{today.day:02d}_平均动态成绩.csv"
            png_path = out_dir / f"{prefix_part}{today.month:02d}-{today.day:02d}_平均动态成绩.png"
            try:
                prev_df_for_save.to_csv(str(csv_path), index=False, encoding='utf-8-sig')
            except Exception:
                prev_df_for_save.to_csv(str(csv_path), index=False)

            # 生成一个简单的占位图片，说明没有历史数据
            try:
                fig, ax = plt.subplots(figsize=(8, 2))
                ax.axis('off')
                ax.text(0.5, 0.5, '无历史平均数据，未使用 realtime 结果', ha='center', va='center')
                fig.savefig(str(png_path), dpi=150, bbox_inches='tight')
                plt.close(fig)
            except Exception:
                pass

            # 同时生成空的匿名 CSV/PNG 以及（必要时）映射文件（映射为空）
            try:
                mappings_dir = Path('data') / 'reports' / 'mappings'
                mappings_dir.mkdir(parents=True, exist_ok=True)
                # 无学生 ID 时传入空列表，load_or_create_perdate_mapping 会创建或读取空映射
                mapping_df, code_map = load_or_create_perdate_mapping(mappings_dir, today, [], None, report_prefix)

                anon_df_for_save = prev_df_for_save.copy()
                # 添加 编号 列（为空），并把编号放到首列
                anon_df_for_save['编号'] = [''] * len(anon_df_for_save)
                cols = anon_df_for_save.columns.tolist()
                if '编号' in cols:
                    cols = ['编号'] + [c for c in cols if c != '编号']
                    anon_df_for_save = anon_df_for_save[cols]

                anon_csv_path = out_dir / f"{prefix_part}{today.month:02d}-{today.day:02d}_平均动态成绩_匿名.csv"
                anon_png_path = out_dir / f"{prefix_part}{today.month:02d}-{today.day:02d}_平均动态成绩_匿名.png"
                try:
                    anon_df_for_save.to_csv(str(anon_csv_path), index=False, encoding='utf-8-sig')
                except Exception:
                    anon_df_for_save.to_csv(str(anon_csv_path), index=False)

                # 生成占位匿名图片
                try:
                    fig, ax = plt.subplots(figsize=(8, 2))
                    ax.axis('off')
                    ax.text(0.5, 0.5, '无历史平均数据，未生成匿名成绩', ha='center', va='center')
                    fig.savefig(str(anon_png_path), dpi=150, bbox_inches='tight')
                    plt.close(fig)
                except Exception:
                    pass
            except Exception:
                # 映射或匿名保存失败不影响主流程
                pass

            return csv_path
        else:
            # 以历史表为基础，将 realtime 的最后一列覆盖历史表的最后一列
            prev_cols = list(prev.columns)
            id_col_prev = None
            for c in ['学号', 'student_id']:
                if c in prev.columns:
                    id_col_prev = c
                    break
            if id_col_prev is None:
                id_col_prev = prev_cols[0]
            prev_df = prev.rename(columns={id_col_prev: 'student_id'}).copy()
            prev_df['student_id'] = prev_df['student_id'].astype(str)

            # 学号并集，保证不丢学生
            all_ids = pd.Index(prev_df['student_id'].astype(str)).union(rt_instant['student_id'].astype(str))
            prev_df = prev_df.set_index('student_id').reindex(all_ids).reset_index()

            # 覆盖历史最后一列为本次即时成绩；若历史只有 id 列则追加即时列
            if prev_df.shape[1] >= 2:
                last_col = prev_df.columns[-1]
                inst_map = rt_instant.set_index('student_id')['instant_value']
                # 覆盖：用即时成绩替换历史最后一列中的对应学号值
                prev_df[last_col] = prev_df['student_id'].map(lambda x: inst_map.get(x) if x in inst_map.index else prev_df.loc[prev_df['student_id']==x, last_col].values[0] if x in prev_df['student_id'].values else np.nan)
                # 将该最后一列重命名为即时列名
                prev_df = prev_df.rename(columns={last_col: instant_col_name})
            else:
                prev_df[instant_col_name] = prev_df['student_id'].map(rt_instant.set_index('student_id')['instant_value'])

        # 计算平均：从第4列（index 3）开始作为即时成绩列参与平均；
        # 先对读取到的历史列（第4列及之后）进行取整处理，再基于取整后的值计算平均，平均值也取整。
        if prev_df.shape[1] < 4:
            if instant_col_name in prev_df.columns:
                prev_df[avg_col_name] = pd.to_numeric(prev_df[instant_col_name], errors='coerce').round(0)
            else:
                prev_df[avg_col_name] = np.nan
        else:
            data_cols = prev_df.columns[3:]
            # 将这些列转换为数值、取整（保留 NaN），并写回 prev_df
            num_vals = prev_df[data_cols].apply(pd.to_numeric, errors='coerce')
            num_vals = num_vals.round(0)
            try:
                prev_df[data_cols] = num_vals.astype('Int64')
            except Exception:
                prev_df[data_cols] = num_vals
            # 计算平均并取整
            prev_df[avg_col_name] = num_vals.mean(axis=1, skipna=True).round(0)
            try:
                prev_df[avg_col_name] = prev_df[avg_col_name].astype('Int64')
            except Exception:
                pass

        # 保存按日期命名的 CSV 与对应图片到 out_dir（仅这两个文件）
        csv_name = f"{prefix_part}{today.month:02d}-{today.day:02d}_平均动态成绩.csv"
        png_name = f"{prefix_part}{today.month:02d}-{today.day:02d}_平均动态成绩.png"
        csv_path = out_dir / csv_name
        png_path = out_dir / png_name
        # 按当天计算出的平均列倒序排序，成绩高的排前面，然后保存
        if avg_col_name in prev_df.columns:
            try:
                prev_df = prev_df.sort_values(avg_col_name, ascending=False, na_position='last').reset_index(drop=True)
            except Exception:
                pass
        # 保存时将 student_id 列改为中文列名 学号，避免修改原 prev_df（保留内部 id 列名以便后续处理）
        prev_df_for_save = prev_df.copy()
        if 'student_id' in prev_df_for_save.columns:
            prev_df_for_save = prev_df_for_save.rename(columns={'student_id': '学号'})
        # 若通过 CLI 指定 --keep-prefix，则对平均表执行学号前缀过滤；默认保留所有学生
        keep_prefix = args.keep_prefix if getattr(args, 'keep_prefix', None) else None
        if keep_prefix:
            prefixes = [p.strip() for p in keep_prefix.split(',') if p.strip()]
            if prefixes:
                before_n = len(prev_df_for_save)
                # 过滤 prev_df（内部 id 列）与 prev_df_for_save（用于保存的学号列）
                try:
                    if 'student_id' in prev_df.columns:
                        prev_df = prev_df[prev_df['student_id'].astype(str).str.startswith(tuple(prefixes))].copy()
                except Exception:
                    pass
                try:
                    if '学号' in prev_df_for_save.columns:
                        prev_df_for_save = prev_df_for_save[prev_df_for_save['学号'].astype(str).str.startswith(tuple(prefixes))].copy()
                except Exception:
                    pass
                after_n = len(prev_df_for_save)
                print(f"已按学号前缀过滤平均表：保留前缀 {prefixes} 的记录，{before_n}->{after_n} 行。")
        try:
            prev_df_for_save.to_csv(str(csv_path), index=False, encoding='utf-8-sig')
        except Exception:
            prev_df_for_save.to_csv(str(csv_path), index=False)

        # 同时保存匿名版 CSV/PNG 与映射表（学号->匿名码），放在 reports/mappings 目录
        try:
            mappings_dir = Path('data') / 'reports' / 'mappings'
            mappings_dir.mkdir(parents=True, exist_ok=True)
            ids_for_map = prev_df_for_save['学号'].astype(str).tolist() if '学号' in prev_df_for_save.columns else []
            names_for_map = prev_df_for_save['姓名'].astype(str).tolist() if '姓名' in prev_df_for_save.columns else None
            mapping_df, code_map = load_or_create_perdate_mapping(mappings_dir, today, ids_for_map, names_for_map, report_prefix)

            # 生成匿名显示表（将 学号 与 姓名 替换为 匿名码）
            anon_prev = prev_df_for_save.copy()
            if '学号' in anon_prev.columns:
                anon_prev['编号'] = anon_prev['学号'].astype(str).map(lambda x: code_map.get(x, ''))
            else:
                anon_prev['编号'] = [''] * len(anon_prev)
            # 删除学号/姓名列
            anon_prev = anon_prev.drop(columns=[c for c in ['学号','姓名'] if c in anon_prev.columns])
            # 将编号放到首列
            cols = anon_prev.columns.tolist()
            if '编号' in cols:
                cols = ['编号'] + [c for c in cols if c != '编号']
                anon_prev = anon_prev[cols]

            # 保存匿名 CSV 与图片
            anon_csv_name = f"{prefix_part}{today.month:02d}-{today.day:02d}_平均动态成绩_匿名.csv"
            anon_png_name = f"{prefix_part}{today.month:02d}-{today.day:02d}_平均动态成绩_匿名.png"
            anon_csv_path = out_dir / anon_csv_name
            anon_png_path = out_dir / anon_png_name
            try:
                anon_prev.to_csv(str(anon_csv_path), index=False, encoding='utf-8-sig')
            except Exception:
                anon_prev.to_csv(str(anon_csv_path), index=False)

            try:
                # 生成匿名图片（复用之前的着色逻辑）
                total_rows = len(anon_prev)
                total_cols = len(anon_prev.columns)
                fig_w = min(max(8, total_cols * 1.0), 240)
                fig_h = min(max(2.5, 0.35 * (total_rows + 3)), 300)
                fig, ax = plt.subplots(figsize=(fig_w, fig_h))
                ax.axis('off')
                cell_text = anon_prev.values.tolist()
                col_labels = list(anon_prev.columns)
                nrows = len(cell_text)
                ncols = len(col_labels)
                bean_bg = '#b7e4c7'
                cell_colors = [[bean_bg for _ in range(ncols)] for _ in range(nrows)]
                # 对匿名表格按单元格着色：统一豆绿色底，仅对不及格(<60)标红
                for i in range(nrows):
                    for j in range(ncols):
                        col = col_labels[j]
                        # 尝试从原始用于保存的表 prev_df_for_save 中获取对应列的原始值（若有）
                        try:
                            if col in prev_df_for_save.columns:
                                raw = prev_df_for_save.iloc[i][col]
                            else:
                                raw = anon_prev.iloc[i, j]
                            v = float(raw) if not pd.isna(raw) and raw != '' else None
                        except Exception:
                            v = None
                        # 若解析出数值且 <60 则标红；否则保持豆绿色底
                        if v is not None and v < 60:
                            cell_colors[i][j] = '#ffadad'
                tbl = ax.table(cellText=cell_text, colLabels=col_labels, cellColours=cell_colors, loc='center')
                tbl.auto_set_font_size(False)
                tbl.set_fontsize(9)
                tbl.scale(1, 1.0)
                for (r, c), cell in tbl.get_celld().items():
                    cell.set_edgecolor('#999999')
                    cell.set_linewidth(0.6)
                    try:
                        # center-align all cell text
                        cell.get_text().set_ha('center')
                        cell.get_text().set_va('center')
                    except Exception:
                        pass
                plt.subplots_adjust(left=0.01, right=0.99, top=0.98, bottom=0.02)
                try:
                    fig.canvas.draw()
                    renderer = fig.canvas.get_renderer()
                    tbbox = tbl.get_window_extent(renderer)
                    bbox_inches = tbbox.transformed(fig.dpi_scale_trans.inverted())
                    bbox_inches = bbox_inches.expanded(1.02, 1.02)
                    fig.savefig(str(anon_png_path), dpi=150, bbox_inches=bbox_inches, pad_inches=0.02)
                except Exception:
                    fig.savefig(str(anon_png_path), dpi=150, bbox_inches='tight', pad_inches=0.02)
                plt.close(fig)
            except Exception:
                pass
        except Exception:
            # 映射或匿名保存失败不影响主流程
            pass

        try:
            total_rows = len(prev_df)
            total_cols = len(prev_df.columns)
            fig_w = min(max(8, total_cols * 1.0), 240)
            fig_h = min(max(2.5, 0.35 * (total_rows + 3)), 300)
            fig, ax = plt.subplots(figsize=(fig_w, fig_h))
            ax.axis('off')
            # 为图片表格生成按最后一列（通常为 avg_col_name）分段着色（>=80 绿，>=60 蓝，否则红）
            # 使用用于保存的表（已将 student_id 重命名为 学号）用于图片显示，保持与 CSV 一致
            cell_text = prev_df_for_save.values.tolist()
            col_labels = list(prev_df_for_save.columns)
            nrows = len(cell_text)
            ncols = len(col_labels)
            # 默认豆绿色背景，仅对不及格(<60)单元格标红
            bean_bg = '#b7e4c7'
            cell_colors = [[bean_bg for _ in range(ncols)] for _ in range(nrows)]
            for i in range(nrows):
                for j in range(ncols):
                    col = col_labels[j]
                    try:
                        if col in prev_df_for_save.columns:
                            raw = prev_df_for_save.iloc[i][col]
                        else:
                            raw = prev_df_for_save.iloc[i, j] if j < len(prev_df_for_save.columns) else ''
                        v = float(raw) if not pd.isna(raw) and raw != '' else None
                    except Exception:
                        v = None
                    # 若解析为数值且 <60，则标红
                    if v is not None and v < 60:
                        cell_colors[i][j] = '#ffadad'
            # create table with colors
            tbl = ax.table(cellText=cell_text, colLabels=col_labels, cellColours=cell_colors, loc='center')
            tbl.auto_set_font_size(False)
            tbl.set_fontsize(9)
            tbl.scale(1, 1.0)
            # style header row
            header_bg = '#2f4f4f'
            header_fg = 'white'
            for j, label in enumerate(col_labels):
                header_cell = None
                for (r, c), cell in tbl.get_celld().items():
                    if c == j and (r == -1 or r == 0):
                        header_cell = cell
                        break
                if header_cell is not None:
                    try:
                        header_cell.set_facecolor(header_bg)
                        header_cell.get_text().set_weight('bold')
                        header_cell.get_text().set_color(header_fg)
                    except Exception:
                        pass
            # add light borders
            for (r, c), cell in tbl.get_celld().items():
                cell.set_edgecolor('#999999')
                cell.set_linewidth(0.6)
                try:
                    # center-align all cell text
                    cell.get_text().set_ha('center')
                    cell.get_text().set_va('center')
                except Exception:
                    pass
            plt.subplots_adjust(left=0.01, right=0.99, top=0.98, bottom=0.02)
            try:
                fig.canvas.draw()
                renderer = fig.canvas.get_renderer()
                tbbox = tbl.get_window_extent(renderer)
                bbox_inches = tbbox.transformed(fig.dpi_scale_trans.inverted())
                bbox_inches = bbox_inches.expanded(1.02, 1.02)
                fig.savefig(str(png_path), dpi=150, bbox_inches=bbox_inches, pad_inches=0.02)
            except Exception:
                fig.savefig(str(png_path), dpi=150, bbox_inches='tight', pad_inches=0.02)
            plt.close(fig)
        except Exception:
            pass

        return csv_path

    # 调用 compute_and_save_rolling_average 并输出结果
    inputs_avg_dir = Path('data/inputs/average')
    try:
        date_csv = compute_and_save_rolling_average(inputs_avg_dir, realtime_csv, out_avg)
    except Exception:
        import traceback
        print('滚动平均生成失败：')
        traceback.print_exc()
        date_csv = None

    # 在控制台打印生成的文件路径，便于用户确认
    print('已生成文件（部分可能为空或未生成）：')
    print(' - 即时报表 CSV: ', realtime_csv)
    print(' - 即时报表图片: ', img_real)
    # 已移除顶层简化平均 CSV/图片的保存；参见 reports/average 下的按日文件
    if date_csv is not None:
        png_out = out_avg / (date_csv.name.replace('.csv', '.png'))
        print(' - 日期平均 CSV: ', date_csv)
        print(' - 日期平均图片: ', png_out)


if __name__ == '__main__':
    main()
