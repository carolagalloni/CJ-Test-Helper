"""Compact journey graph: one line per outcome with the condition as a formula; inventory of constructs."""
import json, re, sys, os, collections
import xml.etree.ElementTree as ET

def items(v):
    if isinstance(v, dict): return list(v.values())
    return v or []

def build_index(d):
    index = {}
    for kind, coll in d['entities'].items():
        if isinstance(coll, dict):
            for k, e in coll.items():
                index[(kind, k)] = e
                if e.get('id'): index[(kind, e.get('id'))] = e
                if isinstance(e.get('data'), dict) and e['data'].get('id'): index[(kind, e['data']['id'])] = e
        else:
            for e in items(coll):
                index[(kind, e.get('id'))] = e
    return index

def make_resolver(index):
    pat = re.compile(r'\{\{mcpm#/entities/([^/]+)/([^/]+)/data/([^}]+)\}\}')
    def resolve(s):
        if not isinstance(s, str): return s
        def rep(m):
            kind, eid, p = m.group(1), m.group(2), m.group(3)
            e = index.get((kind, eid))
            if e is None: return f'<UNRESOLVED {kind}/{eid}/{p}>'
            data = e.get('data', {})
            if p == 'name': return data.get('name', e.get('name', eid))
            if p == 'id': return data.get('id', eid)
            if p.startswith('fields/name:'): return p.split(':',1)[1].split('/')[0]
            if p == 'namePrefix': return data.get('namePrefix', '') or ''
            cur = data
            for seg in p.split('/'):
                cur = cur.get(seg) if isinstance(cur, dict) else None
            return str(cur) if cur is not None else f'<MISSING {kind}/{eid}/{p}>'
        prev = None
        while prev != s:
            prev = s; s = pat.sub(rep, s)
        return s
    return resolve

def formula(el, inv):
    if el.tag == 'FilterDefinition':
        kids = list(el)
        return ' ; '.join(formula(k, inv) for k in kids)
    if el.tag == 'ConditionSet':
        op = el.get('Operator')
        parts = [formula(k, inv) for k in el]
        return '(' + f' {op} '.join(parts) + ')' if len(parts) > 1 else (parts[0] if parts else '()')
    if el.tag == 'Condition':
        key = el.get('Key'); op = el.get('Operator')
        eph = el.get('IsEphemeralAttribute')
        vpn = el.get('ValueParameterName'); vir = el.get('ValueIsReference')
        vals = [ (v.text or '') for v in el.iter('Value')]
        paths = [ (ap.get('RelationshipID')) for ap in el.iter('AttributePath')]
        extra = ''
        if eph: extra += ' [JourneyData]'
        if vpn: extra += f' [ValueParameterName={vpn}]'
        if vir: extra += f' [ValueIsReference={vir}]'
        if paths: extra += f' [AttributePath x{len(paths)}]'
        other = {k: v for k, v in el.attrib.items() if k not in ('Key','Operator','UiMetaData','IsEphemeralAttribute','ValueParameterName','ValueIsReference')}
        if other: extra += f' {other}'
        inv['operators'][op] += 1
        v = vals[0] if vals else None
        inv['values'][(op, re.sub(r'\d+', 'N', v) if v is not None else None)] += 1
        inv['keys'][key.split('.')[0] if key else key] += 1
        return f"{key} {op} {v!r}{extra}"
    return f'<{el.tag}>'

def main(path):
    d = json.load(open(path, encoding='utf-8'))
    index = build_index(d)
    resolve = make_resolver(index)
    inv = {'operators': collections.Counter(), 'values': collections.Counter(), 'keys': collections.Counter(), 'types': collections.Counter(), 'waits': collections.Counter()}
    jid = d.get('selectedEntities', {}).get('journeys', [None])[0]
    data = index[('journeys', jid)]['data']
    acts = data['activities']
    names = {a['key']: a.get('name') for a in acts}
    targets = {o.get('next') for a in acts for o in a.get('outcomes', [])}
    start = [a['key'] for a in acts if a['key'] not in targets]
    print(f"# {os.path.basename(path)} :: {data['name']} v{data.get('version')} entryMode={data.get('entryMode')} activities={len(acts)} start={start}")
    # exit criteria
    for ex in data.get('exits', []):
        crit = ex.get('configurationArguments', {}).get('criteria')
        if crit:
            try:
                f = formula(ET.fromstring(resolve(crit)), inv)
            except Exception as e:
                f = f'<xml error {e}> {crit[:200]}'
            print(f"EXIT {ex.get('key')} {ex.get('name')!r}: {f}")
            print(f"   exit metaData: {json.dumps(ex.get('metaData'))[:300]}")
    for g in data.get('goals', []):
        print(f"GOAL: {json.dumps(g)[:400]}")
    # walk in BFS order from start for readability
    order, seen, queue = [], set(), list(start)
    bykey = {a['key']: a for a in acts}
    while queue:
        k = queue.pop(0)
        if k in seen or k not in bykey: continue
        seen.add(k); order.append(k)
        for o in bykey[k].get('outcomes', []):
            if o.get('next'): queue.append(o['next'])
    for k in bykey:
        if k not in seen: order.append(k)
    for k in order:
        a = bykey[k]
        ca = a.get('configurationArguments', {}) or {}
        md = a.get('metaData', {}) or {}
        args = a.get('arguments', {}) or {}
        t = a['type']; inv['types'][t] += 1
        head = f"{k} [{t}] {a.get('name')!r}"
        if t == 'WAIT':
            ui = md.get('uiType'); inv['waits'][ui] += 1
            if ui == 'WAITBYATTRIBUTE':
                head += f" attr={resolve(ca.get('waitEndDateAttributeExpression'))} +{ca.get('waitDuration')} {ca.get('waitUnit')} at {ca.get('specifiedTime')} tz={ca.get('timeZone')}"
            else:
                head += f" {ca.get('waitDuration')} {ca.get('waitUnit')} tz={ca.get('timeZone')} specifiedTime={ca.get('specifiedTime')} waitType={md.get('waitType')}"
        elif t == 'EMAILV2':
            ts = ca.get('triggeredSend', {})
            head += f" emailId={resolve(str(ts.get('emailId')))} subject={ts.get('emailSubject')!r}"
        elif t in ('SMSSYNC',):
            head += f" configArgs={resolve(json.dumps(ca))[:400]} args={resolve(json.dumps(args))[:200]}"
        elif t == 'UPDATECONTACTDATA':
            ups = args.get('activityData', {}).get('updateContactFields', [])
            head += ' writes ' + ', '.join(f"{resolve('{{mcpm#/entities/dataExtensions/'+u.get('dataExtensionId','?')+'/data/name}}')}.{u.get('field')}={u.get('value')!r}" for u in ups)
        elif t == 'ENGAGEMENTDECISION':
            head += f" ref={ca.get('refActivityCustomerKey')} statsTypeId={ca.get('statsTypeId')} urls={ca.get('engagementUrls',{}).get('urls')}"
        print(head)
        for o in a.get('outcomes', []):
            nxt = o.get('next'); lab = (o.get('metaData') or {}).get('label')
            line = f"   -> {o.get('key')} label={lab!r} next={nxt} ({names.get(nxt)!r})"
            if o.get('arguments'): line += f" args={o.get('arguments')}"
            if t == 'MULTICRITERIADECISION':
                crit = (ca.get('criteria') or {}).get(o.get('key'))
                if crit:
                    try:
                        line += '\n        IF ' + formula(ET.fromstring(resolve(crit)), inv)
                    except Exception as e:
                        line += f'\n        IF <xml error {e}> {resolve(crit)[:300]}'
                else:
                    line += '\n        REMAINDER'
            print(line)
    print('## inventory')
    for k, v in inv.items():
        print(k, dict(v))

if __name__ == '__main__':
    main(sys.argv[1])
