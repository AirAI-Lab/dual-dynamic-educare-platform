"""
列出 data/inputs 下所有支持的文件的第二行（以便检查列名和分母数字）
用法: python tools/list_inputs_second_row.py
"""
from pathlib import Path
import sys
import traceback

root = Path('data/inputs')
if not root.exists():
    print('data/inputs not found')
    sys.exit(1)

files = list(root.rglob('*.csv')) + list(root.rglob('*.xlsx')) + list(root.rglob('*.xls'))
files = sorted(files, key=lambda p: str(p))

import io

for f in files:
    try:
        print('===', f)
        if f.suffix.lower() == '.csv':
            # Read as text, defensive for encoding
            try:
                txt = f.read_text(encoding='utf-8')
            except Exception:
                try:
                    txt = f.read_text(encoding='gbk')
                except Exception:
                    txt = f.read_text(errors='ignore')
            lines = [l.rstrip('\n\r') for l in txt.splitlines()]
            if len(lines) >= 2:
                print('line2:', lines[1])
            else:
                print('line2: <MISSING or empty file>')
        else:
            # Excel: use pandas to read without header and print second row
            try:
                import pandas as pd
                x = pd.read_excel(str(f), sheet_name=0, header=None)
                if x.shape[0] >= 2:
                    row = x.iloc[1].astype(str).tolist()
                    print('line2:', ','.join(row))
                else:
                    print('line2: <LESS THAN 2 ROWS>')
            except Exception as e:
                print('ERROR reading excel:', e)
                traceback.print_exc()
    except Exception as e:
        print('FAILED', f, e)
        traceback.print_exc()
    print()
