"""检查 .bat 文件是不是纯 ASCII。

⚠️ 这条规则是踩了坑才定下来的，不要放宽：

Windows 的 cmd.exe 按**当前控制台代码页**逐字节读取批处理文件。
如果 .bat 里含有中文（任何多字节字符），而代码页与文件编码不匹配，
命令会被撕碎——实测现象是：

    '运行环境存在' is not recognized as an internal or external command
    'ho' is not recognized as an internal or external command      ← echo 被拆成 ho

**结果是双击脚本什么都不发生**，窗口一闪而过，非技术用户完全无从下手。

试过但行不通的方案：
- 存成 UTF-8 + `chcp 65001`：cmd 是按当前代码页读文件的，
  读到 `chcp` 那一行之后仍然会错位，救不了。
- 存成 GBK：只在控制台代码页正好是 936 时才正常，
  换了环境（比如代码页是 65001）一样崩。

**正确做法：.bat 只用纯英文 ASCII，所有中文提示交给 Python 输出。**
Python 3.6+ 在 Windows 上通过宽字符 API 写控制台，不受代码页影响。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tools\\fix_bat_encoding.py
    .venv\\Scripts\\python.exe tools\\fix_bat_encoding.py --check   # 只检查不修改
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def inspect(path: Path) -> tuple[bool, str]:
    """返回 (是否合规, 说明)。"""
    raw = path.read_bytes()
    bad = [(i, b) for i, b in enumerate(raw) if b > 127]

    if not bad:
        return True, "纯 ASCII，安全"

    # 找出具体是哪些字符，方便定位
    for encoding in ("utf-8", "gbk"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        chars = sorted({ch for ch in text if ord(ch) > 127})
        sample = "".join(chars[:10])
        return False, (
            f"含 {len(bad)} 个非 ASCII 字节（按 {encoding} 解码，"
            f"例如这些字符：{sample}）。请把中文提示移到 Python 里输出"
        )

    return False, f"含 {len(bad)} 个非 ASCII 字节，且无法识别编码"


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 .bat 是否纯 ASCII")
    parser.add_argument("--check", action="store_true", help="只检查（默认就是只检查）")
    args = parser.parse_args()

    bats = sorted(ROOT.glob("*.bat"))
    if not bats:
        print("项目根目录下没有 .bat 文件")
        return 0

    print("=" * 66)
    print("批处理文件检查 · .bat 必须是纯 ASCII（原因见脚本头部注释）")
    print("=" * 66)

    bad = 0
    for path in bats:
        ok, note = inspect(path)
        print(f"{'OK ' if ok else 'XX '}{path.name}")
        print(f"     {note}")
        if not ok:
            bad += 1

    print()
    if bad == 0:
        print("全部正常。")
        return 0
    print(f"发现 {bad} 个文件不合规。")
    print("修法：把里面的中文提示挪到 Python 脚本里，bat 只保留英文。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
