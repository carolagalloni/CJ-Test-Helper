"""Run the generator on every client export, write a comparable summary, diff it against the hand-derived targets.

Usage: python run_cohort.py [name ...]   (run from the repository root with PYTHONPATH=. ; defaults to all six)
Writes runs/<name>/ (the generator's files), runs/<name>.summary.json and prints a comparison per journey.
"""
import json, sys, os, traceback
from datetime import datetime
from pathlib import Path

SCRATCH = Path(os.environ.get('CJ_SCRATCH', '/tmp/claude-0/-home-user-CJ-Test-Helper/6010bd0c-246f-58e1-9bfe-230465f90084/scratchpad'))
EXPORTS = SCRATCH / 'newcjs' / 'CJ nuovi'
STEP2 = SCRATCH / 'step2'
RUNS = SCRATCH / os.environ.get('CJ_RUNS', 'runs')
TARGETS = SCRATCH / 'targets'
STEP3_NAMES = ['RET_99_HC', 'RET_99_2W', 'RET_99_3m', 'ret_99_6w', 'RET_99_4W', 'ret_99_5d']
STEP2_NAMES = ['step2_a', 'step2_b', 'step2_c', 'step2_d']
NAMES = STEP3_NAMES + STEP2_NAMES

sys.path.insert(0, os.environ.get('CJ_REPO', '/home/user/CJ-Test-Helper'))
from cjhelper import pipeline
from cjhelper.model import Settings


def summarise(result):
    cov, tm = result.coverage, result.table_model
    branches = []
    for b in cov.branches:
        branches.append({'activity': b.activity_key, 'outcome': b.outcome_key, 'label': b.outcome_label,
                         'status': b.status, 'reason': b.reason, 'covered_by': list(b.covered_by)})
    cases = []
    for s in cov.scenarios:
        for c in s.cases:
            cases.append({'id': c.id, 'scenario': s.id, 'values': {f'{k[0]}.{k[1]}': v for k, v in c.contact_values.items()},
                          'scenario_values': {f'{k[0]}.{k[1]}': v for k, v in s.values.items()},
                          'manual': [f'{m.kind}:{m.activity_key}' for m in c.manual_actions],
                          'path': [f'{st.activity_key}/{st.outcome_key}' if st.outcome_key else st.activity_key for st in c.steps],
                          'end': c.end_activity_key, 'unknown_from': c.unknown_from,
                          'exit_by_criteria': getattr(c, 'exit_by_criteria', None),
                          'sms': list(getattr(c, 'sms_sent', [])), 'emails': list(c.emails_sent),
                          'timeline': [(p.activity_key, p.at.strftime('%Y-%m-%d %H:%M')) for p in getattr(c, 'timeline', [])],
                          'validity_days': getattr(c, 'validity_days', None), 'contacts': c.contact_count})
    return {
        'journey': result.package.journey.name,
        'stats': dict(cov.stats),
        'checks': [(ch.name, ch.passed, ch.details[:6]) for ch in result.checks],
        'tables': [{'name': t.name, 'roles': sorted(t.roles), 'selected': t.selected, 'level': t.level}
                   for t in tm.tables.values() if t.selected],
        'scenario_fields': [f'{a}.{b}' for a, b in cov.scenario_fields],
        'contact_fields': [f'{a}.{b}' for a, b in cov.contact_fields],
        'domains': {f'{a}.{b}': v for (a, b), v in cov.domains.items()},
        'branches': branches,
        'exit_targets': [{'key': b.activity_key, 'status': b.status, 'covered_by': list(b.covered_by), 'reason': b.reason}
                         for b in getattr(cov, 'exit_targets', [])],
        'validity_days': getattr(cov, 'validity_days', None),
        'cases': cases,
        'warnings': list(result.package.warnings) + list(tm.warnings) + list(cov.warnings) + list(result.data.warnings),
    }


def compare(summary, target):
    out = []
    st = summary['stats']
    tmin = target.get('minimum', {})
    out.append(f"scenarios {st.get('scenarios')} vs target {tmin.get('scenarios')} | cases {st.get('cases')} vs {tmin.get('cases')} | "
               f"contacts {st.get('contacts')} vs {tmin.get('contacts')}")
    got = {(b['activity'], b['outcome']): b for b in summary['branches']}
    exp = {(b['activity'], b['outcome']): b for b in target.get('branches', [])}
    norm = lambda s: {'covered': 'covered', 'covered with manual action': 'manual', 'not covered': 'not covered'}.get(s, s)
    missing = [k for k in exp if k not in got]
    extra = [k for k in got if k not in exp]
    diffs = []
    for k, b in exp.items():
        g = got.get(k)
        if g is None:
            continue
        if norm(g['status']) != norm(b['status']):
            diffs.append(f"  {k[0]}/{k[1]} ({b.get('label')}): got {g['status']!r} ({g['reason'][:90]}) expected {b['status']!r} ({b.get('reason_class')}: {b.get('why','')[:90]})")
    out.append(f"branches: {len(got)} got, {len(exp)} expected, {len(missing)} missing, {len(extra)} extra, {len(diffs)} status differences")
    out.extend(diffs)
    if missing: out.append(f"  missing in generator: {missing}")
    if extra: out.append(f"  extra in generator: {extra}")
    te = {t['key']: t for t in target.get('exit_targets', [])}
    tg = {t['key']: t for t in summary['exit_targets']}
    for k, t in te.items():
        g = tg.get(k)
        out.append(f"exit target {k}: got {g['status'] if g else 'MISSING'} expected {t['status']}")
    out.append(f"validity_days: got {summary['validity_days']} expected {target.get('validity_days')}")
    return out


def main(names):
    RUNS.mkdir(exist_ok=True)
    for name in names:
        path = (STEP2 / f'{name}.json') if name in STEP2_NAMES else (EXPORTS / f'{name}.json')
        settings = Settings(real_inbox='tester@example.com', real_phone='+393331234567',
                            generation_time=datetime(2026, 10, 14, 12, 0))
        print(f'===== {name}')
        try:
            result = pipeline.generate(path.read_bytes(), settings)
        except Exception:
            print('CRASH'); traceback.print_exc(); continue
        out = RUNS / name
        out.mkdir(exist_ok=True)
        for fname, content in result.files.items():
            (out / fname).write_bytes(content)
        summary = summarise(result)
        (RUNS / f'{name}.summary.json').write_text(json.dumps(summary, indent=1, ensure_ascii=False, default=str), encoding='utf-8')
        for line in pipeline.summary_lines(result):
            print(line)
        tpath = TARGETS / f'target_{name}.json'
        if tpath.exists():
            try:
                target = json.loads(tpath.read_text(encoding='utf-8'))
                print('\n'.join(compare(summary, target)))
            except Exception as e:
                print('target compare failed:', e)
        else:
            print('(no target yet)')


if __name__ == '__main__':
    main(sys.argv[1:] or NAMES)
