"""Generate reviewed operator SQL only. Never connects, executes, enables or starts.

DB credentials stay with the authorized operator, never on the Mac sync runtime.
"""
import argparse,re
from uuid import UUID


def statement(action, owner, agent, project, device, release, generation=None, approval=None, permanent=False):
    if type(owner) is not int or owner<=0:raise ValueError('Owner required')
    project=str(UUID(project))
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}',agent):raise ValueError('Agent invalid')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}',device):raise ValueError('Device invalid')
    if not re.fullmatch(r'[a-f0-9]{32}',release):raise ValueError('Reviewed release id required')
    where=f"owner_id={owner} AND agent_id='{agent}' AND workspace_id='{project}' AND project_id='{project}' AND device_id='{device}' AND release_id='{release}'"
    if action=='register':
        body=f"INSERT INTO kadrian_sync_control.gate(owner_id,agent_id,workspace_id,project_id,device_id,release_id) VALUES({owner},'{agent}','{project}','{project}','{device}','{release}');"
    elif action=='stop':
        body=f"UPDATE kadrian_sync_control.gate SET enabled=false,generation=generation+1,approval=NULL,expires_at=NULL,changed_at=clock_timestamp() WHERE {where};\nIF NOT FOUND THEN RAISE EXCEPTION 'Stop target missing: NOT confirmed'; END IF;"
    elif action=='enable':
        if type(generation) is not int or generation<1 or not approval or len(approval)>500 or '$gate$' in approval or any(ord(c)<32 for c in approval):raise ValueError('Explicit generation and owner approval reference required')
        approval=approval.replace("'","''")
        expiry='NULL' if permanent else "clock_timestamp()+interval '15 minutes'"
        body=f"UPDATE kadrian_sync_control.gate SET enabled=true,generation=generation+1,approval='{approval}',expires_at={expiry},changed_at=clock_timestamp() WHERE {where} AND NOT enabled AND generation={generation};\nIF NOT FOUND THEN RAISE EXCEPTION 'Activation CAS failed: stays closed'; END IF;"
    else:raise ValueError('Unknown action')
    return "-- OPERATOR REVIEW REQUIRED. No automatic execution.\nBEGIN;\nSET LOCAL lock_timeout='3s';\nSET LOCAL statement_timeout='5s';\nDO $gate$ BEGIN\n"+body+"\nEND $gate$;\nCOMMIT;\n"+f"SELECT enabled,generation,expires_at FROM kadrian_sync_control.gate WHERE {where};\n"


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['register','enable','stop'])
    p.add_argument('--owner',required=True,type=int);p.add_argument('--agent',default='kadrian')
    p.add_argument('--project',required=True);p.add_argument('--device',required=True);p.add_argument('--release',required=True)
    p.add_argument('--generation',type=int);p.add_argument('--approval');p.add_argument('--permanent',action='store_true')
    print(statement(**vars(p.parse_args())))
