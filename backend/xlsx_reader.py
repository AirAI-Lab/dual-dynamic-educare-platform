"""无第三方依赖的 CSV / XLSX 读取与表头适配工具。

设计目标：
1. 直接读取雨课堂导出的汇总 Sheet（第一行常为“汇总”说明行，第二行才是表头）；
2. 读取本平台提供的标准化 CSV 模板；
3. 不要求本机安装 openpyxl，降低部署和学习成本。
"""
from __future__ import annotations

from dataclasses import dataclass
import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from typing import Any, Iterable


NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


@dataclass
class Sheet:
    name: str
    rows: list[list[str]]


def _column_index(reference: str) -> int:
    match = re.match(r"([A-Z]+)", reference or "A")
    if not match:
        return 0
    number = 0
    for char in match.group(1).upper():
        number = number * 26 + ord(char) - 64
    return number - 1


def _relationship_target(base: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return str(PurePosixPath(base).parent.joinpath(target)).replace("\\", "/")


def read_xlsx(content: bytes) -> list[Sheet]:
    """Read all worksheets of a standard OOXML workbook into string matrices."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in shared_root.findall(f"{{{NS_MAIN}}}si"):
                shared_strings.append(
                    "".join(node.text or "" for node in item.iter(f"{{{NS_MAIN}}}t"))
                )

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relationship_map = {
            node.attrib["Id"]: node.attrib["Target"] for node in relationships
        }

        result: list[Sheet] = []
        for sheet_node in workbook.findall(f".//{{{NS_MAIN}}}sheet"):
            relationship_id = sheet_node.attrib.get(f"{{{NS_OFFICE}}}id", "")
            target = relationship_map.get(relationship_id)
            if not target:
                continue
            worksheet_path = _relationship_target("xl/workbook.xml", target)
            if worksheet_path not in archive.namelist():
                continue

            worksheet = ET.fromstring(archive.read(worksheet_path))
            rows: list[list[str]] = []
            for row_node in worksheet.findall(
                f".//{{{NS_MAIN}}}sheetData/{{{NS_MAIN}}}row"
            ):
                cells: dict[int, str] = {}
                for cell in row_node.findall(f"{{{NS_MAIN}}}c"):
                    index = _column_index(cell.attrib.get("r", "A"))
                    cell_type = cell.attrib.get("t")
                    value_node = cell.find(f"{{{NS_MAIN}}}v")
                    inline_node = cell.find(f"{{{NS_MAIN}}}is")
                    if cell_type == "inlineStr" and inline_node is not None:
                        value = "".join(
                            node.text or ""
                            for node in inline_node.iter(f"{{{NS_MAIN}}}t")
                        )
                    elif value_node is None or value_node.text is None:
                        value = ""
                    elif cell_type == "s":
                        try:
                            value = shared_strings[int(value_node.text)]
                        except (ValueError, IndexError):
                            value = ""
                    elif cell_type == "b":
                        value = "TRUE" if value_node.text == "1" else "FALSE"
                    else:
                        value = value_node.text
                    cells[index] = value
                width = max(cells.keys(), default=-1) + 1
                rows.append([cells.get(index, "") for index in range(width)])
            result.append(Sheet(sheet_node.attrib.get("name", worksheet_path), rows))
        return result


def read_csv(content: bytes) -> list[Sheet]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = content.decode(encoding)
            rows = list(csv.reader(io.StringIO(text)))
            return [Sheet("CSV", [[str(cell) for cell in row] for row in rows])]
        except (UnicodeDecodeError, csv.Error) as error:
            last_error = error
    raise ValueError(f"无法识别 CSV 编码：{last_error}")


def read_table(content: bytes, filename: str) -> list[Sheet]:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix in {"xlsx", "xlsm"}:
        return read_xlsx(content)
    if suffix in {"csv", "txt"}:
        return read_csv(content)
    if content[:2] == b"PK":
        return read_xlsx(content)
    return read_csv(content)


def rows_as_dicts(rows: Iterable[list[str]]) -> list[dict[str, str]]:
    """Find a header row containing a student ID and return row dictionaries."""
    matrix = [row for row in rows if any(str(cell).strip() for cell in row)]
    header_index = -1
    for index, row in enumerate(matrix[:10]):
        labels = [str(cell).strip() for cell in row]
        if any(
            label in {"学号", "student_id", "student_no", "Student ID"}
            for label in labels
        ):
            header_index = index
            break
    if header_index < 0:
        raise ValueError("未找到包含“学号/student_id”的表头行")

    headers = [str(cell).strip() for cell in matrix[header_index]]
    records: list[dict[str, str]] = []
    for row in matrix[header_index + 1 :]:
        record: dict[str, str] = {}
        for index, header in enumerate(headers):
            if header:
                record[header] = str(row[index]).strip() if index < len(row) else ""
        if any(record.get(header, "").strip() for header in headers if header):
            records.append(record)
    return records


def choose_sheet(sheets: list[Sheet], kind: str = "realtime") -> Sheet:
    if kind == "realtime":
        for sheet in sheets:
            if "数据汇总" in sheet.name or "汇总" in sheet.name:
                return sheet
    for sheet in sheets:
        if sheet.rows and any(
            any(cell.strip() in {"学号", "student_id", "student_no"} for cell in row)
            for row in sheet.rows[:10]
        ):
            return sheet
    return sheets[0]


def first_number(text: Any) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", str(text or ""))
    return float(match.group(0)) if match else None


def to_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    text = str(value).replace(",", "").replace("%", "").strip()
    if text in {"-", "--", "- -", "未预习", "未答题", "未开始", "未上课", "N/A", "nan"}:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def to_bool(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "是", "已加载", "完成", "已完成"}


def pick_header(record: dict[str, str], keywords: Iterable[str]) -> str | None:
    for keyword in keywords:
        for header in record:
            if keyword.lower() in header.lower():
                return header
    return None
