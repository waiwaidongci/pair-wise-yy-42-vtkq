from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='山火事件指挥与离线人员调度'; ENTITY='山火事件'; ID_PREFIX='WF'
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; TRANSITIONS={'reported': ['active'], 'active': ['contained'], 'contained': ['controlled'], 'controlled': ['closed'], 'closed': []}; TRANSITION_ROLES={'active': ['incident_commander'], 'contained': ['incident_commander'], 'controlled': ['incident_commander'], 'closed': ['incident_commander']}
CREATE_ROLES=set(['field_commander']); RECORD_ROLES=set(['field_commander', 'logistics']); AUDIT_ROLES=set(['incident_commander', 'viewer']); VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
CHEMICAL_ENTITY='药剂'; CHEMICAL_ROLES=set(['logistics']); REQUISITION_ROLES=set(['field_commander', 'logistics']); RETURN_ROLES=set(['logistics'])
SEVERITY_WEIGHT={'low': 1.0, 'moderate': 3.0, 'high': 6.0, 'extreme': 9.0}; DEADLINE_HOURS={'low': 72, 'moderate': 24, 'high': 8, 'extreme': 4}; TERMINAL_STATES=set(['closed'])
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
def completion_blockers(target,open_records,outstanding=()):
    if target not in TERMINAL_STATES: return []
    blockers=["仍有未关闭事项"] if open_records>0 else []
    for entry in outstanding:
        blockers.append(f"药剂「{entry['name']}」未归还{_num(entry['outstanding'])}{entry['unit']}")
    return blockers
def _num(value):
    return f"{float(value):g}"
def requisition_verdict(total_stock,holdings,requesting_item_id,quantity):
    active_total=sum(h["outstanding"] for h in holdings)
    available=total_stock-active_total
    if quantity<=available: return None
    conflicts=[h for h in holdings if h["outstanding"]>0 and h["item_id"]!=requesting_item_id]
    return {"available":available,"requested":quantity,"shortfall":quantity-available,"conflicts":conflicts}
def requisition_conflict_message(name,unit,verdict):
    parts=[f"药剂「{name}」余量不足：余量{_num(verdict['available'])}{unit}，申请{_num(verdict['requested'])}{unit}，缺口{_num(verdict['shortfall'])}{unit}"]
    if verdict["conflicts"]:
        scenes="、".join(f"{c.get('item_ref') or '#'+str(c['item_id'])}（占用{_num(c['outstanding'])}{unit}）" for c in verdict["conflicts"])
        parts.append(f"冲突火场：{scenes}")
    return "；".join(parts)
def return_verdict(outstanding,quantity):
    if quantity<=outstanding: return None
    return {"outstanding":outstanding,"returned":quantity,"excess":quantity-outstanding}
def return_conflict_message(name,unit,verdict):
    return f"药剂「{name}」退回{_num(verdict['returned'])}{unit}超过未归还{_num(verdict['outstanding'])}{unit}"
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
