"""
simhash.py — SimHash 近似去重算法（乐高积木：纯算法层）

═══════════════════════════════════════════════════════════════════════
功能作用
═══════════════════════════════════════════════════════════════════════
提供一个不依赖任何外部库、支持中英文混合文本的 64 位 SimHash 实现，
用于检测"内容相似但 URL 不同"的重复条目（例如同一篇报道被多家媒体
转载，或 Google News 对同一文章生成不同的跳转 URL）。

本模块是纯算法工具，不感知业务上下文（不知道什么是 RawItem，不连接
数据库），可以直接抽出用于任何需要短文本近似去重/相似度比较的场景
（如日志去重、标题聚类、垃圾评论检测）。

═══════════════════════════════════════════════════════════════════════
输入 / 输出
═══════════════════════════════════════════════════════════════════════
simhash(text) -> int
  输入：任意字符串（中英文混合均可）
  输出：64 位有符号整数（兼容 SQLite INTEGER 列存储）

hamming_distance(a, b) -> int
  输入：两个 simhash() 产生的整数
  输出：汉明距离（0 表示完全相同，数值越大差异越大）

═══════════════════════════════════════════════════════════════════════
限制与边界
═══════════════════════════════════════════════════════════════════════
- 分词策略是简化版：英文按空格切分单词，中文按单字切分（不做真正的
  中文分词，如 jieba）。这在标题级别的相似度检测中效果足够，但不适合
  长文本或需要精确语义分词的场景。
- 对极短文本（<4 个 token）SimHash 的区分度会下降，可能出现误判。
  实践中标题去重问题不大，但若复用到长句/段落比较，建议先验证效果。
- 阈值判断（多少汉明距离算重复）不在本模块内决定，由调用方
  （database.py 中的 dedup_threshold 配置）决定，保持算法与业务阈值
  解耦。
- 不做语言检测，中英文字符会被同一套逻辑分别切分后一起参与哈希计算。
"""
import hashlib

# CJK 统一表意文字的 Unicode 范围（基本区 + 扩展 A 区），用于逐字符切分中文。
_CJK_RANGES = (
    ("一", "鿿"),   # CJK Unified Ideographs (U+4E00–U+9FFF)
    ("㐀", "䶿"),   # CJK Unified Ideographs Extension A (U+3400–U+4DBF)
)


def _is_cjk(ch: str) -> bool:
    return any(lo <= ch <= hi for lo, hi in _CJK_RANGES)


def _tokenize(text: str) -> list:
    """
    将文本切分为 token 列表：
      - 空格分隔的词（覆盖英文/拼音/数字等）
      - 单独切出的每个 CJK 字符（覆盖中文，无需真正分词）

    两类 token 会被合并在一起参与 SimHash 计算，因此中英文混合标题
    也能得到合理的相似度表示。
    """
    tokens = list(text.lower().split())
    tokens.extend(ch for ch in text if _is_cjk(ch))
    return tokens


def simhash(text: str) -> int:
    """
    计算文本的 64 位 SimHash 值。

    算法：对每个 token 计算 MD5 哈希，按位对特征向量做加权投票
    （命中该 bit 为 1 则 +1，否则 -1），最终每个 bit 位取投票结果的
    符号得到指纹。空文本（无 token）返回 0。

    返回值转换为有符号 64 位整数，便于直接存入 SQLite 的 INTEGER 列
    （SQLite 整数是有符号的，无符号大数会溢出报错）。
    """
    v = [0] * 64
    for token in _tokenize(text):
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        for i in range(64):
            v[i] += 1 if (h >> i) & 1 else -1
    result = 0
    for i in range(64):
        if v[i] > 0:
            result |= (1 << i)
    if result >= (1 << 63):
        result -= (1 << 64)
    return result


def hamming_distance(a: int, b: int) -> int:
    """计算两个 SimHash 指纹之间的汉明距离（不同 bit 位的个数）。"""
    return bin(a ^ b).count("1")
