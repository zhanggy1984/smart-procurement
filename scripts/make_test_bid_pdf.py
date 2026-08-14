"""生成供应商上传实测用测试标书 PDF（P6.5 上传页实测）。

reportlab 生成 8 章简单标书，覆盖 技术方案/报价/售后 等维度关键词。
"""

import sys

sys.stdout.reconfigure(encoding="utf-8")

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

CHAPTERS = [
    ("第一章 公司概况", "华东合创信息有限公司，专注政务信息化，注册资本 5000 万元，通过 ISO9001 认证。"),
    ("第二章 技术方案", "采用微服务架构，Kubernetes 部署，含高可用设计、容灾备份、性能优化。"),
    ("第三章 项目团队", "项目经理 8 年政务项目经验，团队含架构师 2 人、开发 12 人、测试 4 人。"),
    ("第四章 售后服务", "提供 3 年质保，7×24 响应，2 小时到场，季度巡检与年度培训。"),
    ("第五章 实施计划", "30 天内完成部署上线，分三阶段实施，含验收标准与里程碑。"),
    ("第六章 质量管理", "遵循 CMMI3 流程，代码评审、自动化测试覆盖率 80% 以上。"),
    ("第七章 安全保障", "等保三级合规，数据加密存储，操作日志留存，应急演练。"),
    ("第八章 报价明细", "投标总价 980 万元，分项明细：硬件 320 万、软件 450 万、服务 210 万。"),
]

OUT = "scripts/_test_bid_huadong.pdf"


def main() -> None:
    c = canvas.Canvas(OUT, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 16)
    c.drawString(60, h - 60, "投标文件")
    c.setFont("Helvetica", 11)
    y = h - 100
    for title, body in CHAPTERS:
        c.setFont("Helvetica-Bold", 12)
        c.drawString(60, y, title)
        y -= 20
        c.setFont("Helvetica", 10)
        # 简单换行（每行 80 字符）
        for i in range(0, len(body), 80):
            c.drawString(60, y, body[i:i + 80])
            y -= 16
        y -= 10
    c.save()
    print(f"生成测试 PDF: {OUT}")


main()
