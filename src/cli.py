"""两个 CLI 入口共用的骨架。

为什么单独一个模块
==================
`run.py`（注册主链）与 `tools/run_downstream.py`（下游链路）是**同级入口**，
两边的 argparse 接线与台账落盘骨架曾逐字重复（见
`docs/audit-2026-09-20.md` §3.1）。重复的代价不是"多打几行字"，而是**漂移**：
改一边忘一边，两个入口对同一个开关给出不同行为 —— 而这类漂移**不会报错**。

⚠ 本模块**只放两边经核对真正等价的部分**。
  审计里被列为"重复"的另外几处，核对后判定**不等价**，刻意**不**抽进来：

  | 看似重复 | 为什么不能合并 |
  |---|---|
  | `--out` 的**落盘分支判据** | `run.py` 判 `out is None`（显式给路径 ⇒ 纯导出、不落快照）；`run_downstream.py` 判 `ledger.is_ledger_path(dest)`（路径在台账目录里 ⇒ 落快照）。**"显式 `--out` 指向 ledger/ 内部"这一种输入下两者行为不同**，且各自的 `--help` 都把自己那套写成了承诺。 |
  | `--out` 的 help 文本 | 两边的默认值语义不同（`None` vs 台账读源路径），文本必须各自表述。 |
  | 落盘后的提示行 | `run.py` 印 `relative_to(ROOT)` 相对路径 + 本批条数；`run_downstream.py` 印绝对路径。 |
  | `--key-name` | help 文本不同（"API Key 名称" vs "复用/新建的 key 名"）。 |

  ⇒ 规矩同 `deep-codebase-audit-landing` §5d：**"重复"不等于"等价"，
    先证明再合并**。没证明就合并 = 悄悄改行为。
"""

import argparse

# 🔴 help 文本提成常量，**不是**为了少打几个字 —— 是为了能被断言。
#    `tests/test_cli_skeleton.py` 会跑两个入口的 `--help`，断言这两句逐字出现，
#    这样"某个入口又把 help 抄成自己的一份"会当场变红。
#    （文本统一时改这里一处即可；改前两边分别是"无头模式"/"无头浏览器"
#      和"需要肉眼看"/"要肉眼看"—— 见 docs/audit-2026-09-20.md §3.1。）
HEADLESS_HELP = "无头浏览器（**默认**，不弹窗口）"
HEADFUL_HELP = "有头浏览器（弹窗口；只在需要肉眼看流程时用）"


def add_headless_args(ap: argparse.ArgumentParser) -> None:
    """加 `--headless` / `--headful` 一对互斥开关。

    🔴 关键在 `dest="headless"` + `store_false`：让两个开关写**同一个目标**。
       手写时漏掉 `dest` 是常见错误 —— 于是 `args.headful` 与 `args.headless`
       变成两个字段，而代码只读其中一个 ⇒ **`--headful` 静默无效**
       （argparse 不报错，用户以为切了有头，实际还是无头）。
       抽成函数就是为了不再手写这一对。

    `--headless` 是 `store_true` 且 `default=True` ⇒ **默认就是无头**。
    `--headless` 仍接受是为了让旧脚本 / 文档里的写法继续有效，是幂等空操作。
    """
    ap.add_argument("--headless", action="store_true", default=True,
                    help=HEADLESS_HELP)
    ap.add_argument("--headful", dest="headless", action="store_false",
                    help=HEADFUL_HELP)


def merge_summary_line(kept: int, added: int, upgraded: int, total: int) -> str:
    """「结果合并：原有 N 条 + 本次新增 M 条（K 条已更新：升级或补全字段）= T 条」。

    🔴 返回的字符串**带前导 `\\n`** —— 它就是 `print()` 出去的那一行，
       两个调用方原先都写成 `print(f"\\n结果合并：…")`。保持逐字节相同是抽取的
       前提，所以换行跟着文本一起走。

    ⚠ 只返回**文本**，不打印、不带 `if kept:` 判断 —— 两个调用方在那里**不同**：
      `run.py` 用 `if kept:` 守着（`kept == 0` 时不印），
      `run_downstream.py` 无条件印。把这个差异留在函数之外，抽取才是零行为变化。
    """
    extra = f"（{upgraded} 条已更新：升级或补全字段）" if upgraded else ""
    return f"\n结果合并：原有 {kept} 条 + 本次新增 {added} 条{extra} = {total} 条"