"""导入模板下载 API（P6.2 补：上传模板下载）。

管理员在导入页下载「空模板 + 示例行」作为上传格式参考。
列头唯一来源 app/core/importer.py（与解析严格一致，改解析必同步改此）。
"""

from __future__ import annotations

import csv
from io import BytesIO, StringIO
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from openpyxl import Workbook

from app.api.deps import require_roles
from app.core import importer
from app.models.user import Role

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["import-templates"])


def _fmt_disposition(filename: str) -> str:
    """RFC 5987 中文文件名（filename*），避免 Windows 下载中文名乱码。"""
    return f"attachment; filename*=UTF-8''{quote(filename)}"


# 模板定义：(列头, 示例行, 扩展名, 下载文件名)
# 示例行为空模板的格式参考；列头必须来自 importer（唯一事实源）
_TEMPLATES: dict[str, tuple[list[str], list[str], str, str]] = {
    "expert": (
        importer.EXPERT_EXCEL_HEADERS,
        ["张三", "示例科技有限公司", "华东", "5", "系统集成;软件开发", "110101199001011234", "zhangsan@example.com", "13800000000"],
        "xlsx", "专家导入模板.xlsx",
    ),
    "supplier": (
        importer.SUPPLIER_EXCEL_HEADERS,
        ["示例科技有限公司", "91310000MA1FAKE01X", "张三", "信息技术", "小型"],
        "xlsx", "供应商导入模板.xlsx",
    ),
    "conflict": (
        importer.CONFLICT_CSV_HEADERS,
        ["张三", "示例科技有限公司", "91310000MA1FAKE01X", "任职", "董事", ""],
        "csv", "工商信息冲突导入模板.csv",
    ),
}


def _build_xlsx(headers: list[str], sample: list[str]) -> bytes:
    """生成 Excel（首行列头 + 示例行）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "sheet1"
    ws.append(headers)
    ws.append(sample)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_csv(headers: list[str], sample: list[str]) -> bytes:
    """生成 CSV（utf-8-sig 带 BOM，与解析端 importer.parse_conflict_csv 一致）。"""
    # csv 写 str，需文本流（StringIO 再编码）；BOM 前缀兼容 Excel 打开中文
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerow(sample)
    return ("﻿" + buf.getvalue()).encode("utf-8")


@router.get("/import-templates/{template_type}", summary="下载导入模板（管理端）")
async def download_template(
    template_type: str,
    _admin=Depends(require_roles(Role.ADMIN)),
) -> Response:
    logger.debug("import_template.download", template_type=template_type, operator=_admin.user_id)
    spec = _TEMPLATES.get(template_type)
    if spec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"未知模板类型: {template_type}")
    headers, sample, ext, filename = spec
    if ext == "xlsx":
        content = _build_xlsx(headers, sample)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        content = _build_csv(headers, sample)
        media_type = "text/csv; charset=utf-8"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": _fmt_disposition(filename)},
    )
