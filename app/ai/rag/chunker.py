"""SmartDocumentChunker — 标题感知 + 段落优先文档分块（P2.1 Step 3）。

设计目标（task.md P2.1 / P2.2 / P7.x 段落感知优化）：
- 标题感知：按"第X章 / X.X / 一、 / （一）"等标题行优先切分，标题作为该章
  首个 chunk 内容前缀，同时写入 chapter_title 字段（Milvus 检索溯源用）。
- 段落优先：段内超长正文优先按段落边界切分（空行 \\n\\n，无空行退化为单换行
  \\n），段落为原子单元不切半，只在段落边界断开——避免 token 硬切在句/词中间，
  保 chunk 语义完整（BGE 编码质量、引用溯源）。单段超长（长表格/连续文本无段落
  信号）才退回 token 滑窗兜底。
- 分块尺寸：单 chunk 落在 [min_tokens, max_tokens]，相邻 chunk 保留段落级
  overlap（新 chunk 开头带上一 chunk 末尾段落尾部）。
- 幂等/纯函数：chunk() 无副作用，同一输入输出完全一致（验收/单测可复现）。

P8.2 元数据升级（参考 good-question）：页码协议 `@@PAGE:n@@` 行标记——
  document_ingest 逐页提取 PDF 时插入标记，本分块器解析标记（更新当前页号、
  剥离标记行不进正文），按标题 section 记录覆盖页码范围 page_range=[start,end]
  （跨页 section 下所有 chunk 共享，无标记恒 [0,0]）。新增 heading_level /
  source_type / token_count 溯源元数据。切分顺序与段落原子性不受影响。

P2.2 单测覆盖：标题感知切分、段落边界断开、overlap 保留、超长截断、空文档。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.config import settings

# 标题行识别：中文章节/数字小节/序号列表。行首（允许缩进）命中即视为标题。
# 例："第一章  公司概况"、"3.2 系统架构"、"一、总体目标"、"（一）建设内容"
_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"第[一二三四五六七八九十百千零〇]+[章节篇]"  # 第一章 / 第2节 / 第三篇
    r"|附录[^\n]*"  # 附录A / 附录
    r"|[0-9]+(?:\.[0-9]+){0,2}[、.\s]"  # 1、 / 1.1 / 3.2.1
    r"|[一二三四五六七八九十]{1,3}、"
    r"|（[一二三四五六七八九十]{1,3}）"
    r")\s*[^\n]{0,40}?"
)

# 页码协议（good-question）：PDF 逐页提取时页首插 `@@PAGE:n@@` 行，n 从 1 起。
# 分块器识别并剥离该行（不进正文），section 据此记录覆盖的页码范围。
_PAGE_MARKER_RE = re.compile(r"^@@PAGE:(\d+)@@\s*$")


def _page_range(pages: set[int]) -> list[int]:
    """页码集合 → [start, end]；空集（无标记）→ [0, 0]。"""
    return [min(pages), max(pages)] if pages else [0, 0]


def page_range_to_str(r: list[int]) -> str:
    """page_range → Milvus VARCHAR：单页 "1"、跨页 "1-2"、无页码 "0"。"""
    if not r or len(r) < 2 or r[1] <= 0:
        return "0"
    return str(r[0]) if r[0] == r[1] else f"{r[0]}-{r[1]}"


def page_range_from_str(s: str) -> list[int]:
    """Milvus VARCHAR → page_range：[1,1] 单页 / [1,2] 跨页 / [0,0] 无页码。"""
    if not s or s == "0":
        return [0, 0]
    if "-" in s:
        a, b = s.split("-", 1)
        return [int(a), int(b)]
    n = int(s)
    return [n, n]


def _heading_level(title: str) -> int:
    """标题层级（0=无标题）。映射 _HEADING_RE 各格式：章/篇/附录=1，数字小节
    按点分段数 2/3/4，一、=2，（一）=3。近似反映格式层级，非文档实际层级树。"""
    t = (title or "").strip()
    if not t:
        return 0
    if t.startswith(("第", "附录")):
        return 1
    m = re.match(r"^[0-9]+(?:\.[0-9]+){0,2}", t)
    if m:
        return 2 + m.group(0).count(".")
    if t.startswith("（"):
        return 3
    return 2  # 一、等中文序号及兜底


def _infer_source_type(content: str) -> str:
    """内容类型推断（参考 good-question）：table/list/code/paragraph。"""
    lines = [l for l in content.splitlines() if l.strip()]
    if not lines:
        return "paragraph"
    n = len(lines)
    if sum(1 for l in lines if "|" in l or "\t" in l) / n >= 0.4:
        return "table"
    if sum(1 for l in lines if re.match(r"^\s*[-*•·]\s|\d+[.、]\s", l)) / n >= 0.5:
        return "list"
    if sum(1 for l in lines if re.match(r"^\s{2,}", l) and any(c in l for c in "{}[]();=<>#")) / n >= 0.5:
        return "code"
    return "paragraph"


@dataclass
class DocumentChunk:
    """单个分块。字段对齐 Milvus `bid_documents` schema（scripts/init_milvus.py）。

    P8.2 元数据升级（参考 good-question）：page_range 页码范围（原 page_no 单值
    表达不了跨页 chunk）、heading_level/source_type/token_count 溯源元数据。
    """

    chunk_id: str  # f"{bid_id}-{seq:04d}"
    bid_id: str
    lot_id: str
    content: str
    chapter_title: str
    page_range: list[int]  # [start,end] 页码范围（good-question @@PAGE:n@@ 协议；无标记 [0,0]）
    heading_level: int  # 标题层级（0=无标题，1=章/篇/附录，2=节/一、，3=（一））
    source_type: str  # table/list/code/paragraph（参考 good-question）
    token_count: int  # tiktoken cl100k 统计
    chunk_index: int
    source_file: str
    embedding: list[float] = field(default_factory=list)  # P2.1 Step 4 填充


class SmartDocumentChunker:
    """标题感知分块器。token 用 tiktoken cl100k_base 估算（与 embedding 无关，仅控长度）。"""

    def __init__(
        self,
        min_tokens: int | None = None,
        max_tokens: int | None = None,
        overlap_tokens: int | None = None,
    ) -> None:
        self.min_tokens = min_tokens if min_tokens is not None else settings.doc_chunk_min_tokens
        self.max_tokens = max_tokens if max_tokens is not None else settings.doc_chunk_max_tokens
        self.overlap_tokens = (
            overlap_tokens if overlap_tokens is not None else settings.doc_chunk_overlap_tokens
        )
        if not (self.max_tokens > self.min_tokens > 0 and self.overlap_tokens < self.max_tokens):
            raise ValueError(f"分块参数非法: min={self.min_tokens} max={self.max_tokens} overlap={self.overlap_tokens}")
        # 延迟加载编码器（tiktoken 首次调用会下载词表，本地缓存后复用）
        self._encoder = None

    @property
    def encoder(self):
        """lazily 初始化 tiktoken 编码器（线程安全由 to_thread 调用方保证）。"""
        if self._encoder is None:
            import tiktoken

            self._encoder = tiktoken.get_encoding("cl100k_base")
        return self._encoder

    # ==================== 公开接口 ====================

    def chunk(
        self,
        text: str,
        *,
        bid_id: str,
        lot_id: str,
        source_file: str = "",
    ) -> list[DocumentChunk]:
        """把全文切成 DocumentChunk 列表（标题感知 + 段落优先 + 段落级 overlap）。

        返回空列表：空文档/全空白。chunk_index 全局递增，chunk_id 取
        `{bid_id}-{seq:04d}`，保证 Milvus 主键唯一且可复现。
        页码：文本内 `@@PAGE:n@@` 标记（good-question 协议）由 _split_by_headings
        解析，section 级 page_range 复制给该 section 下所有 chunk。
        """
        if not text or not text.strip():
            return []

        sections = self._split_by_headings(text)
        chunks: list[DocumentChunk] = []
        seq = 0
        for title, body, page_range in sections:
            full = f"{title}\n{body}" if title else body
            # 短文档（含标题 ≤ max）整体保留：标题并入正文首行，chunk 自包含。
            # 超长正文：段落切分纯 body，标题只作为该章首个 chunk 内容前缀——
            # 不让标题参与段落/滑窗切分，避免标题行被拆成近空 chunk（设计不变量）。
            if len(self.encoder.encode(full)) <= self.max_tokens:
                pieces = [full]
            else:
                pieces = self._split_body(body)
                if title and pieces:
                    pieces[0] = f"{title}\n{pieces[0]}"
            heading_level = _heading_level(title)
            for piece in pieces:
                chunks.append(
                    DocumentChunk(
                        chunk_id=f"{bid_id}-{seq:04d}",
                        bid_id=bid_id,
                        lot_id=lot_id,
                        content=piece,
                        chapter_title=title or "无标题",
                        page_range=page_range,
                        heading_level=heading_level,
                        source_type=_infer_source_type(piece),
                        token_count=len(self.encoder.encode(piece)),
                        chunk_index=seq,
                        source_file=source_file,
                    )
                )
                seq += 1
        return chunks

    # ==================== 内部实现 ====================

    @staticmethod
    def _split_by_headings(text: str) -> list[tuple[str, str, list[int]]]:
        """按标题行把全文分成 (标题, 正文, 页码范围) 列表。

        无标题的正文归入"无标题"段（标题为空字符串，调用方映射为"无标题"）。
        标题行本身并入该段正文（见 chunk() 的 full 拼接），故此处只返回标题名。
        页码协议（P8.2，good-question）：`@@PAGE:n@@` 行标记更新当前页号、
        不进入正文；section 记录覆盖的页码范围（无标记恒 [0,0]），跨页 section
        的正文页随标记扩展。
        """
        sections: list[tuple[str, list[str], list[int]]] = []
        cur_title = ""
        cur_lines: list[str] = []
        cur_pages: set[int] = set()  # 当前 section 覆盖的页
        current_page = 0
        for line in text.splitlines():
            pm = _PAGE_MARKER_RE.match(line.strip())
            if pm:  # 页标记行：仅更新当前页号，不进入任何 section 正文
                current_page = int(pm.group(1))
                continue
            if _HEADING_RE.match(line):
                if cur_lines or cur_title:
                    sections.append((cur_title, "\n".join(cur_lines), _page_range(cur_pages)))
                cur_title = line.strip()
                cur_lines = []
                cur_pages = {current_page} if current_page else set()
            else:
                cur_lines.append(line)
                if current_page:
                    cur_pages.add(current_page)
        if cur_lines or cur_title:
            sections.append((cur_title, "\n".join(cur_lines), _page_range(cur_pages)))
        return [(t, b, p) for t, b, p in sections if b.strip()]

    def _split_body(self, body: str) -> list[str]:
        """把一段超长正文切成 [min, max] 的 chunk 列表（段落优先，P7.x）。

        入参为纯正文（不含标题，标题前缀由 chunk() 负责）。段落为原子单元
        不切半，按段落边界断开（语义完整），相邻 chunk 保留上一 chunk 末尾
        段落的 overlap 尾部；单段超 max（长表格/连续文本无段落信号）才退回
        token 滑窗兜底（步长 = max - overlap，原逻辑）。
        """
        tokens = self.encoder.encode(body)
        if len(tokens) <= self.max_tokens:
            return [body]

        paragraphs = self._split_paragraphs(body)
        pieces: list[str] = []
        cur: list[str] = []  # 当前 chunk 的段落（段落为原子单元）
        cur_tokens = 0
        for para in paragraphs:
            p_tokens = len(self.encoder.encode(para))
            if p_tokens > self.max_tokens:
                # 单段超长：先 flush 当前累积，再对该段内部 token 滑窗（无段落信号兜底）
                if cur:
                    pieces.append("\n\n".join(cur))
                    cur, cur_tokens = [], 0
                pieces.extend(self._window_long_paragraph(para))
                continue
            if cur_tokens + p_tokens > self.max_tokens:
                # 段落边界断开：flush 当前 chunk，新 chunk 带上一 chunk 末尾段落尾部
                pieces.append("\n\n".join(cur))
                cur, cur_tokens = self._paragraph_tail(cur)
            cur.append(para)
            cur_tokens += p_tokens
        if cur:
            pieces.append("\n\n".join(cur))
        return pieces

    @staticmethod
    def _split_paragraphs(text: str) -> list[str]:
        """按段落切分：空行（\\n\\n）为段落分隔；无空行退化为单换行（\\n）。

        PDF 按页提取/ DOCX 逐段提取的文本段落信号各异：空行优先（排版规范的
        文档），无空行时单换行也可作为段落边界（至少不切在句/词中间）。
        """
        paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
        if len(paras) > 1:
            return paras
        return [p for p in text.split("\n") if p.strip()]

    def _paragraph_tail(self, paragraphs: list[str]) -> tuple[list[str], int]:
        """取段落列表末尾凑近 overlap_tokens 的段落后缀（新 chunk 开头 overlap）。

        段落为原子单元，overlap 按整段取（宁可略超 overlap_tokens 也不切半段），
        保证相邻 chunk 边界上下文连续。
        """
        tail: list[str] = []
        tail_tokens = 0
        for para in reversed(paragraphs):
            tail.append(para)
            tail_tokens += len(self.encoder.encode(para))
            if tail_tokens >= self.overlap_tokens:
                break
        return tail[::-1], tail_tokens

    def _window_long_paragraph(self, para: str) -> list[str]:
        """单段超 max_tokens 的 token 滑窗兜底（原 _split_body 逻辑，相邻保留 overlap）。"""
        tokens = self.encoder.encode(para)
        if len(tokens) <= self.max_tokens:
            return [para]
        step = self.max_tokens - self.overlap_tokens
        pieces: list[str] = []
        start = 0
        while start < len(tokens):
            end = min(start + self.max_tokens, len(tokens))
            pieces.append(self.encoder.decode(tokens[start:end]))
            if end == len(tokens):
                break
            start += step
        return pieces
