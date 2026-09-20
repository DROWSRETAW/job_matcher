# -*- coding: utf-8 -*-
"""
Excel 导出模块
--------------
把打过分、排好序的岗位清单导出为 Excel，方便直接用表格软件筛选投递。
"""
from typing import List
from datetime import datetime

import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from config import OUTPUT_DIR, OUTPUT_COLUMNS, MATCH_LEVELS, EXCEL_SHEET_NAME


# 匹配等级对应的底色（S 最醒目）
LEVEL_FILLS = {
    "S": "FFD9D9",   # 浅红
    "A": "FFE8CC",   # 浅橙
    "B": "FFF9CC",   # 浅黄
    "C": "F2F2F2",   # 浅灰
    "D": "FFFFFF",
}

# ===================================================================
# ★ 英文字段名 -> 中文表头 的映射表
# ===================================================================
# 为什么需要这张表（这是实际调试中发现的 Bug）：
#   storage.query_jobs() 从 SQLite 返回的字典，key 是**英文列名**
#       {'company': '网宿科技', 'title': '算法工程师', 'match_level': 'S', ...}
#   而 config.OUTPUT_COLUMNS 定义的是**中文表头**
#       ["匹配等级", "公司", "岗位", ...]
#   两者对不上 → DataFrame 里一个中文列都没有 → 被填成空字符串
#   → 导出的 Excel 只有表头、数据行全空。
#
# 修复方式：导出前先按这张表把英文 key 重命名为中文（列顺序由
# OUTPUT_COLUMNS 决定），保证「取数」与「表头」两个契约对齐。
FIELD_TO_HEADER = {
    "match_level": "匹配等级",
    "match_score": "匹配分",
    "hit_keywords": "命中关键词",
    "company": "公司",
    "title": "岗位",
    "city": "城市",
    "salary": "薪资",
    "education": "学历要求",
    "major_requirement": "专业要求",
    "apply_method": "投递方式",
    "deadline": "截止时间",
    "source": "来源",
    "crawl_time": "抓取时间",
    "url": "原文链接",
}


class ExcelExporter:
    """岗位清单 Excel 导出器"""

    def __init__(self, output_dir=None):
        self.output_dir = output_dir or OUTPUT_DIR

    def export(self, jobs: List[dict], filename: str = None) -> str:
        """
        导出岗位清单。

        :param jobs: 岗位字典列表（已按匹配分排序）
        :param filename: 输出文件名，不传则用时间戳命名
        :return: 输出文件的绝对路径
        """
        if not jobs:
            raise ValueError("岗位列表为空，无需导出")

        filename = filename or f"厦门岗位匹配清单_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        out_path = self.output_dir / filename

        # ---- 组装 DataFrame ----
        # 关键：先把英文 key 重命名为中文表头，再按 OUTPUT_COLUMNS 取列。
        # 顺序不能反——OUTPUT_COLUMNS 里存的是中文名。
        df = pd.DataFrame(jobs)

        # 只映射存在的列，避免 pandas 因未知列报错
        rename_map = {k: v for k, v in FIELD_TO_HEADER.items() if k in df.columns}
        if not rename_map:
            raise ValueError(
                "岗位数据的字段名无法识别，请检查 storage.query_jobs() 的返回结构。"
                f"实际字段：{list(df.columns)}"
            )
        df = df.rename(columns=rename_map)

        # 补齐缺失列（用空字符串占位），再按配置顺序取列
        for col in OUTPUT_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        df = df[OUTPUT_COLUMNS]

        # 数据完整性自检：如果所有数据列都是空的，说明映射又错位了，
        # 这种情况必须显式报错，而不是导出一个空表让用户以为是没数据。
        data_cols = [c for c in OUTPUT_COLUMNS if c not in ("匹配等级", "匹配分")]
        if df[data_cols].isna().all().all() and (df[data_cols] == "").all().all():
            raise ValueError(
                "导出的全部数据列为空，疑似字段映射错位，已中止导出。"
            )

        with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name=EXCEL_SHEET_NAME)
            self._style_sheet(writer.sheets[EXCEL_SHEET_NAME])

        return str(out_path)

    # ---------------------------------------------------------------
    def _style_sheet(self, ws):
        """设置表头样式、列宽、按等级着色、冻结首行"""
        # --- 表头样式 ---
        header_font = Font(name="微软雅黑", size=10.5, bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="4472C4")
        thin = Side(style="thin", color="BFBFBF")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border

        # --- 列宽 ---
        widths = {
            "匹配等级": 9, "匹配分": 8, "命中关键词": 26,
            "公司": 20, "岗位": 24, "城市": 10, "薪资": 14,
            "学历要求": 14, "专业要求": 32, "投递方式": 30,
            "截止时间": 14, "来源": 16, "抓取时间": 18, "原文链接": 34,
        }
        for idx, col_name in enumerate(ws.iter_cols(min_row=1, max_row=1), start=1):
            name = col_name[0].value
            ws.column_dimensions[get_column_letter(idx)].width = widths.get(name, 15)

        # --- 数据行样式：按匹配等级着色 ---
        header_names = [c.value for c in ws[1]]
        try:
            level_col = header_names.index("匹配等级") + 1
        except ValueError:
            level_col = 1

        for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
            level = row[level_col - 1].value or "D"
            fill_color = LEVEL_FILLS.get(level, "FFFFFF")

            for cell in row:
                cell.alignment = Alignment(vertical="center", wrap_text=True)
                cell.border = border
                cell.font = Font(name="微软雅黑", size=10)
                if cell.column == level_col:
                    cell.fill = PatternFill("solid", fgColor=fill_color)
                    cell.font = Font(name="微软雅黑", size=10, bold=True)
                    cell.alignment = Alignment(horizontal="center", vertical="center")

        # --- 冻结首行 ---
        ws.freeze_panes = "A2"
