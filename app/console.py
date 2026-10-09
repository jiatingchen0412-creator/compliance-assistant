"""让控制台输出不因为编码问题把程序带崩。

背景：Windows 中文版的默认代码页是 GBK（936）。当标准输出**被重定向**时
（`... | more`、写进文件、被测试脚本捕获），Python 就按 GBK 编码去写；
这时只要打印一个 GBK 里没有的字符——项目里用得不少的 ⚠️ ✅ ❌ ✗——
就会抛 UnicodeEncodeError 直接终结进程。表现是"跑得好好的脚本，一加管道就崩"。

真正的根因是"输出编码跟着系统区域设置走"。这里把 stdout/stderr 都改成 UTF-8，
并且 errors="replace"：宁可显示成一个问号，也不能让一个警告符号弄挂整个程序。

注意：直接连在真实控制台上时（双击 .bat 那种），Python 走的是 Windows 宽字符 API，
本来就不受代码页影响；这里防的是重定向、管道和 CI 的情况。
"""
from __future__ import annotations

import sys


def enable_utf8() -> None:
    """把标准输出/标准错误切成 UTF-8 + 替换非法字符。失败就算了，不能因此报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # 标准流被替换成没有 reconfigure 的对象时（某些测试环境和 IDE 会这么做）忽略
            pass


enable_utf8()
