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


@dataclass
class DocumentChunk:
    """单个分块。字段对齐 Milvus `bid_documents` schema（scripts/init_milvus.py）。"""

    chunk_id: str  # f"{bid_id}-{seq:04d}"
    bid_id: str
    lot_id: str
    content: str
    chapter_title: str
    page_no: int
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
        page_no: int = 0,
    ) -> list[DocumentChunk]:
        """把全文切成 DocumentChunk 列表（标题感知 + 段落优先 + 段落级 overlap）。

        返回空列表：空文档/全空白。chunk_index 全局递增，chunk_id 取
        `{bid_id}-{seq:04d}`，保证 Milvus 主键唯一且可复现。
        """
        if not text or not text.strip():
            return []

        sections = self._split_by_headings(text)
        chunks: list[DocumentChunk] = []
        seq = 0
        for title, body in sections:
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
            for piece in pieces:
                chunks.append(
                    DocumentChunk(
                        chunk_id=f"{bid_id}-{seq:04d}",
                        bid_id=bid_id,
                        lot_id=lot_id,
                        content=piece,
                        chapter_title=title or "无标题",
                        page_no=page_no,
                        chunk_index=seq,
                        source_file=source_file,
                    )
                )
                seq += 1
        return chunks

    # ==================== 内部实现 ====================

    @staticmethod
    def _split_by_headings(text: str) -> list[tuple[str, str]]:
        """按标题行把全文分成 (标题, 正文) 列表。

        无标题的正文归入"无标题"段（标题为空字符串，调用方映射为"无标题"）。
        标题行本身并入该段正文（见 chunk() 的 full 拼接），故此处只返回标题名。
        """
        sections: list[tuple[str, list[str]]] = []
        cur_title = ""
        cur_lines: list[str] = []
        for line in text.splitlines():
            if _HEADING_RE.match(line):
                if cur_lines or cur_title:
                    sections.append((cur_title, "\n".join(cur_lines)))
                cur_title = line.strip()
                cur_lines = []
            else:
                cur_lines.append(line)
        if cur_lines or cur_title:
            sections.append((cur_title, "\n".join(cur_lines)))
        return [(t, b) for t, b in sections if b.strip()]

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
