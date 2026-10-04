#!/usr/bin/env python3
"""证明一次配置改动「只删了注释」——剥掉注释与空行后逐行 diff。

为什么需要它：注释掉的服务/参数与生效内容长得几乎一样，肉眼扫不出来；
而「以为只动了注释、其实连带删掉一个生效参数」不会当场报错，
要到下次重建容器才炸。所以改完必须机器比对，不能靠看。

退出码：0 = 生效配置零改动；1 = 有实质差异（差异已打印）；2 = 用法错误。

用法：
  python3 verify_config_comment_only.py old.bak new.yml

判据：两边的「有效行数」相等、且剥注释后逐行相同。
注意：只把整行 # 开头的去掉、并丢弃行内 # 之后的内容——
如果某个值的字符串里就含 #，本脚本会误删，这类文件手工核。
"""
import difflib
import sys


def strip(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            s = ln.split("#")[0].rstrip()
            if s.strip():
                out.append(s)
    return out


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    old, new = sys.argv[1], sys.argv[2]
    a, b = strip(old), strip(new)
    print("有效行数  %s=%d  →  %s=%d" % (old, len(a), new, len(b)))
    diff = list(difflib.unified_diff(a, b, old, new, lineterm=""))
    if not diff:
        print("\n✅ 剥掉注释后逐行完全一致 —— 生效配置零改动，删的全是注释")
        return 0
    print("\n❌ 有实质差异（这些行不属于注释，是被真改动的）：")
    for line in diff:
        print("  ", line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
