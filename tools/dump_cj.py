"""Raw structural dump of a Package Manager export: journey activities, criteria XML, SQL, DEs, relationships."""
import json, re, sys, os, collections
import xml.dom.minidom

def items(v):
    if isinstance(v, dict): return list(v.values())
    return v or []

def main(path):
    d = json.load(open(path, encoding='utf-8'))
    ents = d['entities']
    # index entities by id for mcpm resolution
    index = {}
    for kind, coll in ents.items():
        if isinstance(coll, dict):
            for k, e in coll.items():
                index[(kind, k)] = e
                if e.get('id'): index[(kind, e.get('id'))] = e
                if isinstance(e.get('data'), dict) and e['data'].get('id'): index[(kind, e['data']['id'])] = e
        else:
            for e in items(coll):
                index[(kind, e.get('id'))] = e
    def resolve(s):
        if not isinstance(s, str): return s
        pat = re.compile(r'\{\{mcpm#/entities/([^/]+)/([^/]+)/data/([^}]+)\}\}')
        def rep(m):
            kind, eid, p = m.group(1), m.group(2), m.group(3)
            e = index.get((kind, eid))
            if e is None: return f'<UNRESOLVED {kind}/{eid}/{p}>'
            data = e.get('data', {})
            if p == 'name': return data.get('name', e.get('name', eid))
            if p == 'id': return data.get('id', eid)
            if p.startswith('fields/name:'):
                return p.split(':',1)[1].split('/')[0]
            cur = data
            for seg in p.split('/'):
                if isinstance(cur, dict): cur = cur.get(seg)
                else: cur = None
            return str(cur) if cur is not None else f'<MISSING {kind}/{eid}/{p}>'
        prev = None
        while prev != s:
            prev = s
            s = pat.sub(rep, s)
        return s
    out = []
    P = out.append
    P(f'# {os.path.basename(path)}')
    jid = d.get('selectedEntities', {}).get('journeys', [None])[0]
    j = index[('journeys', jid)]
    data = j['data']
    P(f"## Journey {data.get('name')} v{data.get('version')} entryMode={data.get('entryMode')} status={data.get('status')}")
    P(f"defaults: {resolve(json.dumps(data.get('defaults')))}")
    for t in data.get('triggers', []):
        P(f"trigger: type={t.get('type')} key={t.get('key')} eventDefKey={resolve(t.get('metaData',{}).get('eventDefinitionKey'))}")
        ca = t.get('configurationArguments', {})
        P(f"  trigger configArgs: {resolve(json.dumps(ca))[:1500]}")
        P(f"  trigger arguments: {resolve(json.dumps(t.get('arguments')))[:800]}")
    P(f"goals: {resolve(json.dumps(data.get('goals')))[:800]}")
    P(f"exits: {resolve(json.dumps(data.get('exits')))[:1500]}")
    acts = data.get('activities', [])
    keys = {a['key']: a for a in acts}
    targets = {o.get('next') for a in acts for o in a.get('outcomes', [])}
    start = [a['key'] for a in acts if a['key'] not in targets]
    P(f"start activities: {start}")
    P('## Activities')
    for a in acts:
        ca = a.get('configurationArguments', {}) or {}
        md = a.get('metaData', {}) or {}
        args = a.get('arguments', {}) or {}
        P(f"### {a['key']} [{a['type']}] name={a.get('name')!r} uiType={md.get('uiType')}")
        for o in a.get('outcomes', []):
            P(f"  outcome key={o.get('key')} next={o.get('next')} args={resolve(json.dumps(o.get('arguments')))[:300]} label={o.get('metaData',{}).get('label')!r}")
        if a['type'] in ('MULTICRITERIADECISION', 'MULTICRITERIADECISIONV2'):
            crit = ca.get('criteria', {})
            for k, xml in crit.items():
                xml = resolve(xml)
                try:
                    pretty = xml.dom.minidom.parseString(xml).toprettyxml(indent=' ')
                    pretty = '\n'.join(l for l in pretty.split('\n') if l.strip())
                except Exception as ex:
                    pretty = xml + f'  <!-- parse error {ex} -->'
                P(f"  criteria[{k}]:\n" + '\n'.join('    ' + l for l in pretty.split('\n')))
            other = {k: v for k, v in ca.items() if k != 'criteria'}
            P(f"  other configArgs: {resolve(json.dumps(other))[:600]}")
        elif a['type'] == 'WAIT':
            P(f"  configArgs: {resolve(json.dumps(ca))[:800]}")
            P(f"  metaData: {resolve(json.dumps(md))[:600]}")
        elif a['type'] in ('EMAILV2',):
            ts = ca.get('triggeredSend', {})
            P(f"  emailId={resolve(str(ts.get('emailId')))} name={ts.get('name')} subject={ts.get('emailSubject')!r} keys={list(ts.keys())[:40]}")
        elif a['type'] in ('SMSSYNC', 'SMS', 'PUSHNOTIFICATION'):
            P(f"  configArgs: {resolve(json.dumps(ca))[:1500]}")
            P(f"  arguments: {resolve(json.dumps(args))[:600]}")
        elif a['type'] in ('UPDATECONTACTDATA',):
            ad = args.get('activityData', {})
            P(f"  updates: {resolve(json.dumps(ad))[:1200]}")
        elif a['type'] in ('ENGAGEMENTDECISION',):
            P(f"  configArgs: {resolve(json.dumps(ca))[:800]}")
            P(f"  metaData: {resolve(json.dumps(md))[:400]}")
        else:
            P(f"  configArgs: {resolve(json.dumps(ca))[:800]}")
            P(f"  arguments: {resolve(json.dumps(args))[:800]}")
    P('## Event definitions')
    for e in items(ents.get('eventDefinitions')):
        dd = e['data']
        P(json.dumps({k: resolve(json.dumps(v)) if not isinstance(v, str) else resolve(v) for k, v in dd.items() if k not in ('id',)}, indent=1, ensure_ascii=False)[:3000])
    P('## Automations')
    for a in items(ents.get('automations')):
        dd = a['data']
        P(f"automation {dd.get('name')} type={dd.get('typeId')} status={dd.get('status')} schedule={resolve(json.dumps(dd.get('schedule')))[:400]}")
        for i, s in enumerate(dd.get('steps', [])):
            for act in s.get('activities', []):
                P(f"  step {i}: {resolve(act.get('name'))} objectTypeId={act.get('objectTypeId')}")
    P('## Query activities')
    for q in items(ents.get('queryActivities')):
        dd = q['data']
        P(f"### query {dd.get('name')} target={resolve(dd.get('targetName') or '')} targetKey={resolve(str(dd.get('targetKey')))} targetId={resolve(str(dd.get('targetId')))} updateType={dd.get('targetUpdateTypeName')}")
        P(resolve(dd.get('queryText', '')))
    P('## Data extensions')
    for de in items(ents.get('dataExtensions')) + items(ents.get('sharedDataExtensions')):
        dd = de['data']
        P(f"### DE {dd.get('name')} key={dd.get('key')} sendable={dd.get('isSendable')} sendableField={resolve(json.dumps(dd.get('sendableCustomObjectField') or dd.get('sendableDataExtensionField')))} retention={json.dumps({k:v for k,v in dd.items() if 'etention' in k or 'elete' in k})[:200]}")
        for f in sorted(dd.get('fields', []), key=lambda f: f.get('ordinal', 0)):
            P(f"  - {f.get('name')} {f.get('type')} len={f.get('length')} pk={f.get('isPrimaryKey')} null={f.get('isNullable')} default={f.get('defaultValue')!r} scale={f.get('scale')}")
    P('## Attribute sets')
    for s in items(ents.get('attributeSets')):
        dd = s['data']
        P(f"ASET {dd.get('name')} storageName={resolve(str(dd.get('storageName')))} isRoot={dd.get('isRoot')} storageRef={resolve(str(dd.get('storageReferenceID')))} leftRel={resolve(json.dumps(dd.get('leftRelationships')))[:1500]}")
    P('## Attribute relationships')
    for r in items(ents.get('attributeRelationships')):
        dd = r['data']
        P(resolve(json.dumps(dd, ensure_ascii=False))[:2500])
    P('## Attribute groups')
    for g in items(ents.get('attributeGroups')):
        dd = g['data']
        P(resolve(json.dumps({k: v for k, v in dd.items() if k in ('name','key','attributeSetIds','isSystemDefined','description')}, ensure_ascii=False))[:800])
    P('## Issues')
    P(json.dumps(d.get('issues'))[:1500])
    for kind, coll in ents.items():
        for e in items(coll):
            if e.get('issues'):
                P(f"issue on {kind}/{e.get('data',{}).get('name')}: {json.dumps(e.get('issues'))[:600]}")
    print('\n'.join(out))

if __name__ == '__main__':
    main(sys.argv[1])
