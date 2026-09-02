"""决策日志（docs/trading_system_tasks.md P1-T5）。

每笔交易计划落 ``~/.easy_tdx/decisions.db``：标的/理由/止损止盈/可信度/
当时特征快照 JSON。写入口：``easy-tdx plan --save``、``scripts/log_decision.py``、
``scripts/trading_plan.py --save``；为 P2-T1 事后回归管道提供数据基础。
"""

from .store import add_decision, db_path, get_decision, list_decisions, open_db

__all__ = ["add_decision", "db_path", "get_decision", "list_decisions", "open_db"]
