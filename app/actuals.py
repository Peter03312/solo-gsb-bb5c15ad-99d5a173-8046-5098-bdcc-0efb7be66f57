"""装船实绩事件重放与校验（纯函数，不触碰数据库）。

事件可晚到：登记时把已接受事件与新事件放在一起，**按实际分钟重放**，
而非按到达顺序处理。重放次序为：

1. ``minute`` 升序；
2. 同分钟 **先完工（finish）后开工（start）**；
3. 再以事件流水号升序（保证次序确定）。

开工须同时满足：该箱尚无开工记录、该桥在该分钟空闲
（该桥此前已开工的箱均已完工）、同堆栈下层箱已完工；
完工须满足：该箱已开工、岸桥与开工桥一致、完工分钟晚于开工分钟；
此外同桥任意两个已完成作业区间 ``[开工, 完工)`` 不得重叠
（允许首尾相接）。

基线冻结排程仅提供：方案箱集合、每箱堆栈/层、合法岸桥编号范围；
实际时序一律以重放结果为准，不与计划时间窗比较。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlannedBox:
    """冻结排程中的单箱计划信息（实绩重放所需部分）。"""

    no: str
    stack: str
    tier: int
    start_minute: int
    finish_minute: int
    crane_no: int


@dataclass(frozen=True)
class ActualEvent:
    """一条装船实绩事件。"""

    event_seq: int
    event_type: str  # 'start' | 'finish'
    container_no: str
    crane_no: int
    minute: int


class InvalidActualEvent(Exception):
    """重放实绩事件链时发现不合法事件。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# 同分钟先完工后开工
_TYPE_ORDER = {"finish": 0, "start": 1}


def replay_order_key(ev: ActualEvent) -> tuple[int, int, int]:
    return (ev.minute, _TYPE_ORDER[ev.event_type], ev.event_seq)


def order_events(events) -> list[ActualEvent]:
    return sorted(events, key=replay_order_key)


@dataclass
class BoxState:
    """重放过程中单箱的实时状态。"""

    stack: str
    tier: int
    start_minute: int | None = None
    finish_minute: int | None = None
    crane_no: int | None = None


@dataclass
class ReplayResult:
    """重放结果：每箱实际起止/岸桥，按箱号升序。"""

    boxes: list[BoxState]


def replay(events, boxes, *, crane_count: int) -> ReplayResult:
    """按实际分钟重放完整事件链；任一事件不合法即抛 :class:`InvalidActualEvent`。

    :param events: 已接受事件 + 待登记事件（全集）
    :param boxes: 冻结排程箱项（方案箱集合与堆栈/层）
    :param crane_count: 冻结排程岸桥数量（合法桥号为 1..crane_count）
    """
    state: dict[str, BoxState] = {
        b.no: BoxState(stack=b.stack, tier=b.tier) for b in boxes
    }
    # 同堆栈层号 -> 箱号（配载方案保证不悬空）
    by_stack: dict[str, dict[int, str]] = {}
    for b in boxes:
        by_stack.setdefault(b.stack, {})[b.tier] = b.no

    ordered_events = order_events(list(events))
    for idx, ev in enumerate(ordered_events):
        if ev.event_type not in ("start", "finish"):
            raise InvalidActualEvent(
                f"事件 {ev.event_seq}：事件类型 {ev.event_type!r} 非法"
                "（仅支持 start/finish）"
            )
        if not (1 <= ev.crane_no <= crane_count):
            raise InvalidActualEvent(
                f"事件 {ev.event_seq}：岸桥编号 {ev.crane_no} 超出冻结排程"
                f"岸桥范围 1..{crane_count}"
            )
        box = state.get(ev.container_no)
        if box is None:
            raise InvalidActualEvent(
                f"事件 {ev.event_seq}：箱 {ev.container_no} 不在该方案冻结排程中"
            )

        if ev.event_type == "start":
            if box.start_minute is not None:
                raise InvalidActualEvent(
                    f"事件 {ev.event_seq}：箱 {ev.container_no} 已于第 "
                    f"{box.start_minute} 分钟开工，不能重复开工"
                )
            # 该桥空闲：该桥此前已开工的箱都必须已完工
            for other in state.values():
                if (
                    other.crane_no == ev.crane_no
                    and other.start_minute is not None
                    and other.finish_minute is None
                ):
                    raise InvalidActualEvent(
                        f"事件 {ev.event_seq}：第 {ev.minute} 分钟岸桥 "
                        f"{ev.crane_no} 正作业箱 {other_no(other, state)}，"
                        "开工时该桥不空闲（同桥作业区间不得重叠）"
                    )
            # 同堆栈下层箱须已完工
            if box.tier > 1:
                lower_no = by_stack.get(box.stack, {}).get(box.tier - 1)
                if lower_no is not None:
                    lower = state[lower_no]
                    if lower.finish_minute is None or lower.finish_minute > ev.minute:
                        raise InvalidActualEvent(
                            f"事件 {ev.event_seq}：同堆栈下层箱 {lower_no} "
                            f"尚未完工，箱 {ev.container_no} 不能在第 "
                            f"{ev.minute} 分钟开工"
                        )
            box.start_minute = ev.minute
            box.crane_no = ev.crane_no
        else:  # finish
            if box.start_minute is None:
                # 同分钟重放次序为“先完工后开工”：若同箱同分钟还有一条开工，
                # 真实原因是完工分钟未严格晚于开工分钟（而非单纯尚未开工）
                later_start = any(
                    e.event_type == "start"
                    and e.container_no == ev.container_no
                    and e.minute == ev.minute
                    for e in ordered_events[idx + 1:]
                )
                if later_start:
                    raise InvalidActualEvent(
                        f"事件 {ev.event_seq}：箱 {ev.container_no} 完工分钟 "
                        f"{ev.minute} 必须严格晚于开工分钟 {ev.minute}"
                        "（同分钟先处理完工再处理开工，开工与完工不得同在该分钟）"
                    )
                raise InvalidActualEvent(
                    f"事件 {ev.event_seq}：箱 {ev.container_no} 尚未开工，"
                    "不能登记完工"
                )
            if box.crane_no != ev.crane_no:
                raise InvalidActualEvent(
                    f"事件 {ev.event_seq}：箱 {ev.container_no} 开工岸桥为 "
                    f"{box.crane_no}，完工岸桥 {ev.crane_no} 与之不一致"
                )
            if box.finish_minute is not None:
                raise InvalidActualEvent(
                    f"事件 {ev.event_seq}：箱 {ev.container_no} 已于第 "
                    f"{box.finish_minute} 分钟完工，不能重复完工"
                )
            if ev.minute <= box.start_minute:
                raise InvalidActualEvent(
                    f"事件 {ev.event_seq}：箱 {ev.container_no} 完工分钟 "
                    f"{ev.minute} 必须严格晚于开工分钟 {box.start_minute}"
                )
            # 同桥区间不重叠：与该桥所有既有完成区间 [s,f) 不得相交
            for other in state.values():
                if other is box or other.crane_no != ev.crane_no:
                    continue
                if other.start_minute is None or other.finish_minute is None:
                    continue
                s, f = box.start_minute, ev.minute
                if s < other.finish_minute and other.start_minute < f:
                    raise InvalidActualEvent(
                        f"事件 {ev.event_seq}：箱 {ev.container_no} 在岸桥 "
                        f"{ev.crane_no} 的作业区间 [{s}, {f}) 与箱 "
                        f"{other_no(other, state)} 的既有作业区间 "
                        f"[{other.start_minute}, {other.finish_minute}) 重叠"
                    )
            box.finish_minute = ev.minute

    return ReplayResult(boxes=[state[no] for no in sorted(state)])


def other_no(box: BoxState, state: dict[str, BoxState]) -> str:
    """反查箱号（仅用于中文报错信息）。"""
    for no, b in state.items():
        if b is box:
            return no
    return "?"
