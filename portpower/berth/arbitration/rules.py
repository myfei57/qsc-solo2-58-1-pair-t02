"""The numbered rule set every arbitration decision cites.

Each rule is a single, explicit statement. A ``Decision`` always records the
``RuleId`` under which it became effective, so any pre-emption can be traced
back to the exact clause and effective timestamp.
"""

from __future__ import annotations

from enum import Enum


class RuleId(str, Enum):
    # --- booking management -------------------------------------------------
    BOOKING_ACCEPTED = "R1"
    BOOKING_WINDOW_CONFLICT = "R2"
    BOOKING_CANCELLED = "R3"
    BOOKING_REBOOKED = "R4"

    # --- reservations taking effect ----------------------------------------
    RESERVATION_HONORED = "R10"
    RESERVATION_ACTIVE_LOCK = "R11"
    RESERVATION_GRACE_HELD = "R12"

    # --- walk-in traffic ----------------------------------------------------
    WALKIN_FREE_BERTH = "R20"
    WALKIN_FIT_GAP = "R21"
    WALKIN_PREEMPTS = "R22"
    WALKIN_QUEUED = "R23"
    WALKIN_DENIED = "R24"
    WALKIN_WITHDRAWN = "R25"

    # --- timeout / vacation -------------------------------------------------
    RESERVATION_NO_SHOW = "R30"
    QUEUE_PROMOTED = "R31"

    # --- physical departure -------------------------------------------------
    BERTH_VACATED = "R40"


# Human-readable wording, exposed on the audit/decision read models.
RULE_DESCRIPTIONS: dict[RuleId, str] = {
    RuleId.BOOKING_ACCEPTED: "预约窗口无冲突，预约成立并在 [start, end) 内持有泊位。",
    RuleId.BOOKING_WINDOW_CONFLICT: "预约窗口与同泊位已生效的预约窗口重叠，拒绝建立。",
    RuleId.BOOKING_CANCELLED: "预约被显式取消，持有的泊位约束立即解除。",
    RuleId.BOOKING_REBOOKED: "被挤掉或超时让位的预约在新窗口内重新安排。",
    RuleId.RESERVATION_HONORED: "预约船舶在生效窗口内到港，预约兑现为实际靠泊。",
    RuleId.RESERVATION_ACTIVE_LOCK: "预约已生效，窗口内泊位锁定给预约方，临时靠泊不得占用。",
    RuleId.RESERVATION_GRACE_HELD: "预约已到点但宽限期未满，泊位继续保留给预约方。",
    RuleId.WALKIN_FREE_BERTH: "临时靠泊直接排入当前空闲泊位。",
    RuleId.WALKIN_FIT_GAP: "临时靠泊可以完全落入两条预约之间的空窗，按空窗安排。",
    RuleId.WALKIN_PREEMPTS: (
        "无空窗且泊位上仅有未生效的未来预约；临时靠泊明确挤掉该预约，"
        "被挤方收到通知并进入可重排池。"
    ),
    RuleId.WALKIN_QUEUED: (
        "无空窗且现存预约均不可挤（已生效或宽限期内）；临时靠泊进入等待队列。"
    ),
    RuleId.WALKIN_DENIED: "所有泊位均被实际占用且无队列策略适用，临时靠泊被拒绝。",
    RuleId.WALKIN_WITHDRAWN: "临时靠泊船方主动撤销等待，退出队列。",
    RuleId.RESERVATION_NO_SHOW: (
        "预约超过到点宽限期仍未兑现，自动让位：泊位释放给队列下一位，"
        "原预约进入可重排池，不删除。"
    ),
    RuleId.QUEUE_PROMOTED: "泊位空出或预约让位时，等待队列首位被自动提升到该泊位。",
    RuleId.BERTH_VACATED: "实际靠泊船舶离港，泊位释放并触发队列提升。",
}
