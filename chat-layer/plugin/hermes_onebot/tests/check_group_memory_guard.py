"""★ C1 硬前置的**功能级**守门测试：群回合到底会不会写主库。

和 `check_group_wake.py`（判据层）分开的原因：这里要**真的把记忆 provider 加载起来**，
跑一遍「群回合 → 是否触发 retain」，所以它比纯函数测试慢一点、也更重 —— 值得，
因为「群消息零入库」这条承诺在 C1 之前是**免费**的（群消息根本不到 agent），
开了 @ 唤醒之后必须由 `hindsight_guard` 顶住。

跑法（子进程，`HERMES_HOME` 指向聊天门 profile —— 不改本进程环境）：
  * 取证：`--provider hindsight`    → **必须** 看到群回合触发 retain（这就是要堵的洞）
  * 验收：`--provider hindsight_guard` → 群回合 0 次 retain，私聊照旧写主库
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PLUGIN_DIR = _HERE.parent
_EVIDENCE = _HERE / "evidence_group_retain.py"
_CHAT_HOME = Path("/opt/data/profiles/chat")
_PY = os.environ.get("HERMES_PYTHON") or "/opt/hermes/.venv/bin/python"


def _run(provider: str, expect: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "HERMES_HOME": str(_CHAT_HOME), "PYTHONPATH": "/opt/hermes"}
    return subprocess.run(
        [_PY, str(_EVIDENCE), "--provider", provider, "--expect", expect],
        capture_output=True, text=True, timeout=180, env=env, cwd=str(_PLUGIN_DIR))


@unittest.skipUnless(Path(_PY).exists(), "需要 Hermes 自带解释器")
@unittest.skipUnless(_EVIDENCE.is_file(), "取证脚本不在")
@unittest.skipUnless((_CHAT_HOME / "config.yaml").is_file(), "聊天门 profile 不在")
class TestGroupTurnMemoryIsolation(unittest.TestCase):
    """群回合不许碰主记忆库；私聊一模一样的回合必须照写（闸门不能一刀切）。"""

    def test_stock_provider_would_leak_group_turns_into_main_bank(self):
        """取证：原版 hindsight 下群回合**确实会**触发 retain → 所以 C1 必须先装闸。

        这条用例是「洞的证据」。它失败（=原版也不写）反而是好消息，但那时要人工复核
        前提是否变了（例如 Hermes 上游加了隔离），不能默默放宽。
        """
        cp = _run("hindsight", "group:write")
        self.assertEqual(cp.returncode, 0,
                         f"取证脚本失败：\n{cp.stdout[-1500:]}\n{cp.stderr[-800:]}")
        self.assertIn("VERDICT", cp.stdout)

    def test_guard_blocks_group_turns_and_keeps_dms(self):
        """验收：装了闸之后，群回合 0 次 retain、私聊 1 次 retain 到主库。"""
        cp = _run("hindsight_guard", "group:skip dm:write")
        self.assertEqual(cp.returncode, 0,
                         f"闸门行为不符：\n{cp.stdout[-1500:]}\n{cp.stderr[-800:]}")

    def test_guard_plugin_is_installed_and_wired_in_chat_profile(self):
        """闸门在聊天门 profile 里**在位且在位可用**（静态判据，与适配器硬前置同源）。"""
        sys.path.insert(0, os.path.dirname(str(_PLUGIN_DIR)))
        import importlib
        gwk = importlib.import_module(f"{_PLUGIN_DIR.name}.group_wake")
        st = gwk.memory_isolation_status(_CHAT_HOME)
        self.assertTrue(st["plugin_ok"], f"闸门插件不在位：{st}")
        # provider 指向：C1 上线即应为闸门；主人临时回退到 `hindsight` 时适配器会拒绝唤醒，
        # 这里只把状态打出来（不 fail），避免测试代替主人做回退决策。
        print(f"\n[记忆隔离体检] {st}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
