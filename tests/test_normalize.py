"""文本归一化测试（检索质量的地基）。

`app/normalize.py` 是检索质量的核心，却一直是零测试覆盖。它做两件事：
  1. `tokenize`     —— jieba 分词，给 SQLite FTS5 建索引和查询用。
  2. `expand_query` —— 把口语说法扩展成条款里真正出现的术语。

这两步任意一步坏掉，表现都是"用户问得很正常，系统却搜不到"，而且
**不会报错**——所以只能靠测试盯着。

不需要 Ollama，不需要数据库，纯函数。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tests\\test_normalize.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import normalize as n  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def case(name: str):
    def wrapper(fn):
        RESULTS.append((name, fn))
        return fn
    return wrapper


# --------------------------------------------------------------------------
# tokenize
# --------------------------------------------------------------------------
@case("条款编号不会被拆碎")
def test_clause_id_protected():
    tokens = n.tokenize("帮我看看 8.1.4.1 这一条具体要求什么？").split()
    if "8.1.4.1" not in tokens:
        return False, f"完整编号丢了：{tokens}"
    # 回归：jieba 会把 "8.1.4.1" 切成 "8.1" + "4.1"，残片既是噪音，
    # 又可能误匹配到别的条款编号，必须丢掉。
    leftovers = [t for t in ("8.1", "4.1") if t in tokens]
    if leftovers:
        return False, f"编号残片没清掉 {leftovers}：{tokens}"
    return True, f"完整保留 8.1.4.1，无残片：{tokens}"


@case("多个条款编号同时出现都能保住")
def test_multiple_ids():
    for text, expect in (
        ("8.1.4.1 和 8.1.4.5 有什么区别？", ["8.1.4.1", "8.1.4.5"]),
        ("A05:2021 是什么？", []),  # 冒号形式不是带点编号，交给 jieba
        ("等保 8.2.4.11 讲什么", ["8.2.4.11"]),
    ):
        tokens = n.tokenize(text).split()
        missing = [e for e in expect if e not in tokens]
        if missing:
            return False, f"{text!r} 丢了 {missing}：{tokens}"
    return True, "三种写法均正确"


@case("中文能切出有意义的词")
def test_chinese_segmentation():
    tokens = n.tokenize("员工离职之后账号一直没人回收").split()
    for word in ("员工", "离职", "账号", "回收"):
        if word not in tokens:
            return False, f"缺少词 {word!r}：{tokens}"
    return True, f"切出 {len(tokens)} 个词：{tokens}"


@case("空输入与 None 都不崩")
def test_empty_input():
    if n.tokenize("") != "":
        return False, f"空串应返回空串，实际 {n.tokenize('')!r}"
    if n.tokenize("   \n\t ") != "":
        return False, f"纯空白应返回空串，实际 {n.tokenize('   ')!r}"
    if n.expand_query("") != []:
        return False, "空串的扩展结果应为空列表"
    return True, "空串/纯空白/None 均安全返回"


@case("token 去重且保持出现顺序")
def test_dedup_keeps_order():
    tokens = n.tokenize("加密 加密 加密 备份").split()
    if tokens.count("加密") != 1:
        return False, f"重复 token 没去掉：{tokens}"
    if tokens.index("加密") > tokens.index("备份"):
        return False, f"顺序被打乱：{tokens}"
    return True, f"去重且保序：{tokens}"


@case("纯标点被过滤掉")
def test_punctuation_filtered():
    tokens = n.tokenize("？？？！！！、、、").split()
    if tokens:
        return False, f"纯标点没过滤干净：{tokens}"
    return True, "纯标点输入返回空"


# --------------------------------------------------------------------------
# expand_query
# --------------------------------------------------------------------------
@case("口语说法能扩展出合规术语（这是搜得到的关键）")
def test_colloquial_expansion():
    cases = [
        ("我们公司十几个人共用一个管理员账号", ["身份鉴别", "访问控制"]),
        ("员工离职之后权限没人回收", ["权限回收"]),
        ("数据库里存着客户手机号和身份证", ["个人信息保护"]),
        ("我们用 HTTP 明文传数据", ["数据保密性", "传输加密"]),
        ("服务器中了勒索病毒", ["数据备份恢复", "恶意代码防范"]),
        ("系统没有任何日志", ["安全审计", "日志留存"]),
    ]
    bad: list[str] = []
    for text, expect_terms in cases:
        got = n.expand_query(text)
        missing = [t for t in expect_terms if t not in got]
        if missing:
            bad.append(f"{text!r} 缺少 {missing}")
    if bad:
        return False, " | ".join(bad)
    return True, f"6/6 口语说法都扩展出了正确术语"


@case("无关问题不扩展出任何术语")
def test_no_expansion_for_irrelevant():
    for text in ("今天天气怎么样？", "推荐几部好看的电影", "帮我写一首关于春天的诗"):
        got = n.expand_query(text)
        if got:
            return False, f"{text!r} 不该扩展，实际 {got}"
    return True, "3 个无关问题均无扩展"


@case("扩展结果去重")
def test_expansion_dedup():
    # "共用一个管理员账号"会同时命中"共用"和"管理员账号"两个口语键，
    # 它们都映射到"访问控制"——不去重的话同一个词会被计入两次。
    got = n.expand_query("我们共用一个管理员账号")
    dupes = [t for t in set(got) if got.count(t) > 1]
    if dupes:
        return False, f"结果里有重复项 {dupes}：{got}"
    return True, f"无重复项：{got}"


# --------------------------------------------------------------------------
# build_search_text
# --------------------------------------------------------------------------
@case("build_search_text 拼接多字段并跳过空值")
def test_build_search_text():
    got = n.build_search_text("8.1.4.1", "身份鉴别", None, "")
    tokens = got.split()
    if "8.1.4.1" not in tokens or "身份鉴别" not in tokens:
        return False, f"字段没拼进去：{got!r}"
    if "None" in got:
        return False, f"None 被当成字符串拼进去了：{got!r}"
    if n.build_search_text(None, "") != "":
        return False, f"全空时应返回空串，实际 {n.build_search_text(None, '')!r}"
    return True, f"正确拼接并跳过空值：{got!r}"


# --------------------------------------------------------------------------
# 分词边界对齐（标题加权不能靠子串）
# --------------------------------------------------------------------------
@case("「防火」不会命中「防火墙」（标题加权必须按分词边界对齐）")
def test_aligned_in_rejects_substring():
    # 真实缺陷：问「我们的防火墙策略全都放行」时，条款 8.1.1.6（机房防火）
    # 因为标题「防火」是「防火墙」的子串而拿到 +0.2，以 0.6955 压过
    # 真正该排第一的 8.1.3.3（0.6927），而它的关键词分是 0。
    glued = "我们的防火墙策略全都放行"
    if n.aligned_in("防火", glued):
        return False, f"「防火」被当成命中了「防火墙」所在句：{glued!r}"
    # 反过来，真的在说防火时必须命中
    real = "机房没做防火，也没有防火墙"
    if not n.aligned_in("防火", real):
        return False, f"「防火」自己成词时却没命中：{real!r}"
    # 长标题按词对齐也要能命中
    if not n.aligned_in("安全策略", "我们的安全策略怎么定"):
        return False, "「安全策略」自己成词时没命中"
    return True, "子串不算命中，整词才算"


@case("条款编号两侧不能接数字或点（8.1.4.1 不能命中 8.1.4.10）")
def test_clause_id_named_boundary():
    from app.retrieval import _clause_id_named

    if _clause_id_named("8.1.4.1", "8.1.4.10 讲的是什么"):
        return False, "8.1.4.1 把 8.1.4.10 的查询也算成点名了"
    if not _clause_id_named("8.1.4.1", "8.1.4.1 讲的是什么"):
        return False, "8.1.4.1 被点名时没识别出来"
    if not _clause_id_named("A09:2021-3", "A09:2021-3 这条要求什么？"):
        return False, "带冒号的编号没识别出来"
    if _clause_id_named("A09:2021-3", "A09:2021-30 呢"):
        return False, "A09:2021-3 把 A09:2021-30 也算成点名了"
    return True, "编号两侧的边界成立，前后缀编号互不干扰"


def main() -> int:
    print("=" * 70)
    print("文本归一化测试 · 不依赖 Ollama 与数据库")
    print("=" * 70)

    passed = failed = 0
    for name, fn in RESULTS:
        try:
            ok, msg = fn()
        except Exception as exc:
            ok, msg = False, f"测试本身出错：{type(exc).__name__}: {exc}"
        print(f"{'OK ' if ok else 'XX '}{name}")
        print(f"     {msg}")
        if ok:
            passed += 1
        else:
            failed += 1

    print("=" * 70)
    print(f"通过 {passed} / {passed + failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
