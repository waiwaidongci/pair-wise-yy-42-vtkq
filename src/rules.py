from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='山火事件指挥与离线人员调度'; ENTITY='山火事件'; ID_PREFIX='WF'
AGENT_ENTITY='药剂'; REQUISITION_ENTITY='现场领用'; RETURN_ENTITY='药剂交回'
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; TRANSITIONS={'reported': ['active'], 'active': ['contained'], 'contained': ['controlled'], 'controlled': ['closed'], 'closed': []}; TRANSITION_ROLES={'active': ['incident_commander'], 'contained': ['incident_commander'], 'controlled': ['incident_commander'], 'closed': ['incident_commander']}
CREATE_ROLES=set(['field_commander']); RECORD_ROLES=set(['field_commander', 'logistics']); AUDIT_ROLES=set(['incident_commander', 'viewer']); VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
AGENT_CREATE_ROLES=set(['logistics']); REQUISITION_ROLES=set(['field_commander', 'logistics']); RETURN_ROLES=set(['field_commander', 'logistics']); USAGE_VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
ACTIVE_FIRE_STATES=set(['reported', 'active', 'contained', 'controlled']); CLOSED_STATE='closed'
SEVERITY_WEIGHT={'low': 1.0, 'moderate': 3.0, 'high': 6.0, 'extreme': 9.0}; DEADLINE_HOURS={'low': 72, 'moderate': 24, 'high': 8, 'extreme': 4}; TERMINAL_STATES=set(['closed'])
def fmt_qty(value):
    return ("%.3f" % float(value)).rstrip('0').rstrip('.')
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def fire_is_active(status):
    return status in ACTIVE_FIRE_STATES
def requisition_conflict_detail(stock, fire_outstanding, requested, fire_id):
    """余量不足判定。fire_outstanding为{火场id: 活动未归还量}，含申请火场自身。"""
    other = [(fid, qty) for fid, qty in fire_outstanding.items()
             if fid != fire_id and qty > 0]
    other.sort()
    own = fire_outstanding.get(fire_id, 0.0)
    available = stock - sum(qty for fid, qty in fire_outstanding.items()) + own
    return {
        "requested": requested,
        "available": max(0.0, available),
        "stock": stock,
        "own_outstanding": own,
        "conflict_fires": [{"fire_id": fid, "outstanding": qty} for fid, qty in other],
    }
def stock_shortage_message(agent_name, detail, unit=""):
    suffix = unit or ""
    parts = [f"药剂「{agent_name}」余量不足：申请{fmt_qty(detail['requested'])}{suffix}，"
             f"可用余量仅{fmt_qty(detail['available'])}{suffix}"]
    if detail["conflict_fires"]:
        clash = "、".join(f"火场#{c['fire_id']}占用{fmt_qty(c['outstanding'])}{suffix}"
                          for c in detail["conflict_fires"])
        parts.append(f"冲突火场：{clash}")
    if detail["own_outstanding"] > 0:
        parts.append(f"本火场已占用{fmt_qty(detail['own_outstanding'])}{suffix}尚未归还")
    return "；".join(parts)
def fire_close_blockers(unreturned):
    """unreturned为[{name, unit, outstanding, requisition_ids}]，未归还则阻断关闭。"""
    blockers = []
    for item in unreturned:
        suffix = item.get("unit") or ""
        ids = ",".join(str(i) for i in item["requisition_ids"])
        blockers.append(
            f"火场仍有未归还药剂：{item['name']}缺口{fmt_qty(item['outstanding'])}"
            f"{suffix}（领用单#{ids}）"
        )
    return blockers
