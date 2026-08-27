"""chat 意图轻量分类（移植 good-question 二期 function calling，场景本地化：标书评审追问）。

用途两处（见 agent_loop.py）：
- F3 规则否决：LLM 未调工具但规则判该查（query/unknown）→ 强制检索，防 LLM 直接编造
- 检索空三路兜底：query→"未找到"、unknown→"澄清"、smalltalk/非文档问题→LLM 自然答

纯函数、无 IO，便于单元测试。优先级与 good-question 一致：
身份闲聊 > 寒暄整句 > 查询意图 > 一般闲聊 > unknown（回指归队）。
"""

from __future__ import annotations

import re

# 身份类闲聊：最特定（"你是谁/你能做什么"），即便含疑问词也判闲聊——优先于查询意图
_IDENTITY_SMALLTALK = (
    "你是谁", "你叫什么", "你能做什么", "你会什么", "你是什么", "你是干嘛的",
    "自我介绍", "介绍一下你自己", "介绍下你自己",
)
# 查询意图标记：疑问词 / 疑问号 / 查询动词 + 标书评审领域词。命中判查询——
# 防编造的最关键闸门（query 优先于一般闲聊：带问候前缀的查询如"你好，技术方案怎样"
# 必须判查询，绝不能交给 LLM 在空 context 下编造）。领域词让专家追问保守判 query。
_QUERY_MARKERS = (
    "？", "?", "什么", "怎么", "如何", "为什么", "几", "多少", "哪些", "何时",
    "哪里", "是不是", "能否", "可以吗", "有没有", "是否", "查", "找", "帮",
    "告诉", "解释", "总结", "说明", "写", "列出", "推荐",
    # 标书评审领域词（示例场景）：追问几乎都命中，宁可强制检索也不可编造
    "标准", "评分", "细则", "评分项", "多少分", "怎么打分", "报价", "价格", "金额",
    "资质", "认证", "工期", "团队", "人员", "技术方案", "实施", "保障", "服务",
    "条款", "标书", "技术", "方案",
)
# 寒暄整句（正则 fullmatch）：覆盖"最近/今天 + 心情/状态/过得 + 怎么样/咋样"等口语变体，
# 含"怎么/咋"但整句是寒暄则非查询。优先级放在 query 之前——关键约束是 fullmatch 整句：
# "今天心情怎么样，报价多少"（带查询）不命中 → 继续走 query。
_CASUAL_PATTERNS = (
    re.compile(r"^(你|您)?(最近|今天|这两天|这段时间)(心情|状态|过得|身体)?怎么样[啊呀呢嘛!！？?\s]*$"),
    re.compile(r"^(你|您)?(最近|今天|这两天|这段时间)(心情|状态|过得|身体)?咋样[啊呀呢嘛!！？?\s]*$"),
    re.compile(r"^(最近|这两天)?(在)?忙(什么|不忙)[啊呀呢嘛!！？?\s]*$"),
    re.compile(r"^(你|您)(现在)?(在)?(干嘛|干啥|忙什么|做什么|咋了|怎么了)呢?[啊呀?！?\s]*$"),
    re.compile(r"^你呢?[啊呀?！?\s]*$"),
    re.compile(r"^吃了(没|吗)[啊呀?！?\s]*$"),
)
# 明确回指词：unknown 时仅命中这些才回看 history 归队（"就这个/还有呢"延续上一轮意图）。
# 收紧条件避免跨话题短词被归错队——"你呢"已是闲聊反问，由 _CASUAL_PATTERNS 直接识别。
_REFERENTIAL_WORDS = (
    "就这个", "还有呢", "然后呢", "再说说", "再详细点", "这个呢", "那个呢",
)
# 一般闲聊：问候 / 感谢 / 道别。仅当无查询意图时才判闲聊
_SOCIAL_SMALLTALK = (
    "你好", "您好", "嗨", "哈喽", "嗨喽", "hello", "hi", "在吗", "在不在",
    "早上好", "中午好", "下午好", "晚上好",
    "谢谢", "感谢", "辛苦你了", "谢谢你",
    "再见", "拜拜", "回头聊", "下次聊",
)

# 非文档问题（F3 豁免）：明显无需查标书的通用问题（纯计算/当前时间/通用常识）——强制
# 检索只会误伤，如"17×23 等于多少"被否决强制检索空后追加"未找到"，造成割裂体验。
# 命中即视为与标书内容无关：跳过否决，空检索兜底时也交 LLM 自然作答而非"未找到"。
_NON_DOC_QUESTION_PATTERNS = (
    # 纯算术整串："17×23"、"17 乘以 23 等于多少"、"1+1等于几"
    re.compile(r"^\d+\s*(?:乘以|乘|加|加上|减|减去|除|除以|[+\-*/×÷])\s*\d+\s*(?:等于|是|就是)?\s*(?:多少|几|什么)?\s*[？?]?$"),
    # 计算指令 + 数字运算："计算 17*23"、"帮我算一下 5 加 3"
    re.compile(r"^(?:算一算|算一下|计算|帮我算|请计算)[^？?]*[+\-*/×÷乘以加减除]"),
    # 实时信息（标书库不可能有）：今天/现在/明天 + 星期/日期/时间
    re.compile(r"^(?:今天|现在|当前|明天)(?:是)?(?:星期几|星期[一二三四五六日天]|几月几号|几点|几点几分|什么时间|几号)"),
    re.compile(r"^(?:今天|明天|后天)(?:的)?(?:天气|气温|温度|会不会下雨|下雨吗)"),
    # 通用常识白名单（与具体标书内容无关的百科类）
    re.compile(r"^(?:圆周率|光速|地球|太阳系|水的沸点|一公斤等于|一年有|一天有)"),
)


def _is_non_doc_question(text: str) -> bool:
    """F3 豁免判定：明显无需查标书的通用问题（纯计算/当前时间/通用常识）。

    这类问题 LLM 能直接答对，否决强制检索只会造成"先答再补未找到"的割裂体验。
    命中 → 跳过 F3 否决；空检索兜底时同样不走"未找到"话术（交 LLM 自然作答）。
    """
    t = text.strip()
    if not t:
        return False
    return any(p.match(t) for p in _NON_DOC_QUESTION_PATTERNS)


def _classify_intent(text: str, history: list | None = None) -> str:
    """轻量规则意图分类：smalltalk / query / unknown（F3 否决与空检索兜底用）。

    优先级：身份闲聊 > 寒暄整句 > 查询意图 > 一般闲聊 > unknown（回看历史最近一条）。
    - 身份闲聊最特定，即便含疑问词（"你能做什么"）也判闲聊，走 LLM 自然答；
    - 寒暄整句（"最近怎么样/今天心情怎么样"）含"怎么"但整句匹配、非查询，优先于 query；
    - 查询意图优先于一般闲聊：带问候前缀的查询（"你好，报价多少"）必须判查询，
      避免交给 LLM 在空 context 下编造；
    - unknown（既非闲聊也非明确查询）保守判非闲聊 → 走澄清话术（同样不调 LLM，防编造）；
      history 可选（最近 user 消息原文倒序）：unknown 且命中明确回指词（"就这个/还有呢"）
      时回看最近一条 user 消息的意图归队延续上一轮。
    抽成纯函数便于单元测试覆盖边界。
    """
    t = text.lower().strip()
    if not t:
        return "unknown"
    if any(k in t for k in _IDENTITY_SMALLTALK):
        return "smalltalk"
    if any(p.fullmatch(t) for p in _CASUAL_PATTERNS):
        return "smalltalk"
    if any(q in t for q in _QUERY_MARKERS):
        return "query"
    if any(k in t for k in _SOCIAL_SMALLTALK):
        return "smalltalk"
    if history and any(w in t for w in _REFERENTIAL_WORDS):
        for prev in history:
            if prev and prev.strip().lower() != t:
                return _classify_intent(prev)
    return "unknown"
