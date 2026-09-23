from pathlib import Path
import pandas as pd
import shutil
from datetime import datetime

SRC_DIR = Path('data') / 'inputs' / 'real-time'
BACKUP_DIR = SRC_DIR / 'backups'
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

modified = []

for p in sorted(SRC_DIR.glob('*')):
    if p.is_dir():
        continue
    if p.suffix.lower() not in ['.csv', '.xlsx', '.xls']:
        continue
    try:
        if p.suffix.lower() == '.csv':
            # try utf-8-sig then fallback
            try:
                df = pd.read_csv(str(p), dtype=str, encoding='utf-8-sig')
            except Exception:
                df = pd.read_csv(str(p), dtype=str)
        else:
            df = pd.read_excel(str(p), sheet_name=0, dtype=str)
    except Exception as e:
        print(f"跳过无法读取的文件: {p} ({e})")
        continue

    cols = list(df.columns)
    # check exact-match columns
    if any(str(c).strip() == '签到次数' for c in cols):
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = BACKUP_DIR / f"{p.name}.bak.{ts}"
        shutil.copy2(str(p), str(backup_path))
        # replace only exact matches
        new_cols = [ ('阅读公告数' if str(c).strip() == '签到次数' else c) for c in cols ]
        df.columns = new_cols
        # write back
        try:
            if p.suffix.lower() == '.csv':
                df.to_csv(str(p), index=False, encoding='utf-8-sig')
            else:
                df.to_excel(str(p), index=False)
        except Exception as e:
            print(f"写回文件失败: {p} ({e})")
            # attempt to restore backup
            try:
                shutil.copy2(str(backup_path), str(p))
                print(f"已恢复原文件: {p}")
            except Exception:
                print(f"恢复失败: {p}")
            continue
        modified.append((p, backup_path))
        print(f"已修改: {p} -> 备份: {backup_path}")

if not modified:
    print("未找到包含 '签到次数' 的文件。")
else:
    print('\n修改完成，受影响文件：')
    for p, b in modified:
        print(f" - {p}  （备份：{b}）")
