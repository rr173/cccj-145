#!/usr/bin/env python3
"""Command line interface for the local DICOM metadata catalog.

Usage examples:
    python3 cli.py scan catalog.db /data/delivery1 /data/delivery2
    python3 cli.py status catalog.db
    python3 cli.py series catalog.db
    python3 cli.py object catalog.db 2.25.1234...
    python3 cli.py references catalog.db
    python3 cli.py conflicts catalog.db
    python3 cli.py files catalog.db --errors
    python3 cli.py close-batch catalog.db 2
Add --json to any read command for machine-readable output.
"""

import argparse
import json
import sys

from dicom_catalog import Catalog


def _emit(args, data, text):
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))
    elif text is not None:
        print(text)


def _short(uid, keep=18):
    return uid if len(uid) <= keep else uid[:keep - 1] + '…'


def _status_text(st):
    lines = ['catalog state: %s' % st['overall']]
    c = st['counts']
    lines.append('objects: %d usable, %d conflicted | sources: %d | references: %d '
                 '| files ok/error: %d/%d'
                 % (c['objects_ok'], c['objects_conflict'], c['sources'],
                    c['references'], c['files_ok'], c['files_error']))
    if st['reference_status']:
        lines.append('reference status: '
                     + ', '.join('%s=%d' % kv for kv in sorted(st['reference_status'].items())))
    for b in st['batches']:
        lines.append('batch #%-3d %-11s files %d/%d (errors %d) roots=%s %s'
                     % (b['id'], b['status'], b['processed_files'], b['total_files'],
                        b['error_files'], b['roots_json'],
                        ('note: ' + b['note']) if b['note'] else ''))
    return '\n'.join(lines)


def _series_text(rows):
    lines = ['%-46s %-46s %-8s %6s %10s' % ('STUDY', 'SERIES', 'MODALITY', 'USABLE', 'CONFLICT')]
    for r in rows:
        lines.append('%-46s %-46s %-8s %6d %10d'
                     % (_short(r['study_uid'], 46), _short(r['series_uid'], 46),
                        ','.join(r['modalities']) or '-',
                        r['usable_objects'], r['conflicted_objects']))
    return '\n'.join(lines)


def _studies_text(rows):
    lines = ['%-46s %-6s %6s %10s' % ('STUDY', 'SERIES', 'USABLE', 'CONFLICT')]
    for r in rows:
        lines.append('%-46s %-6d %6d %10d'
                     % (_short(r['study_uid'], 46), len(r['series']),
                        r['usable_objects'], r['conflicted_objects']))
    return '\n'.join(lines)


def _object_text(obj):
    if obj is None:
        return 'object not found in catalog'
    lines = ['SOPInstanceUID : %s' % obj['sop_instance_uid'],
             'SOPClassUID    : %s' % obj['sop_class_uid'],
             'StudyInstanceUID : %s' % obj['study_uid'],
             'SeriesInstanceUID: %s' % obj['series_uid'],
             'status         : %s' % obj['status']]
    if obj['conflict_reason']:
        lines.append('conflict       : %s' % obj['conflict_reason'])
    lines.append('sources        : %d' % len(obj['sources']))
    for s in obj['sources']:
        lines.append('  %-18s %s bytes=%d hash=%s… (batch %d)'
                     % (s['basis'], s['path'], s['size'], s['content_hash'][:12],
                        s['first_seen_batch']))
    lines.append('references     : %d' % len(obj['references']))
    for ref in obj['references']:
        lines.append('  -> [%s] class=%s instance=%s'
                     % (ref['status'].upper(), ref['ref_sop_class_uid'],
                        ref['ref_sop_instance_uid']))
        lines.append('     reason: %s' % ref['reason'])
        if ref['target']:
            for p in ref['target']['sources']:
                lines.append('     target file: %s' % p)
    for c in obj['conflicts']:
        lines.append('conflict event : %s (batch %d)' % (c['path'], c['batch_id']))
        for field, (old, new) in sorted(c['diff'].items()):
            lines.append('     %-20s stored=%r incoming=%r' % (field, old, new))
    return '\n'.join(lines)


def _references_text(refs):
    lines = []
    for r in refs:
        lines.append('[%s] %s -> %s (class %s)'
                     % (r['status'].upper(), r['referrer_uid'],
                        r['ref_sop_instance_uid'], r['ref_sop_class_uid']))
        lines.append('     %s' % r['reason'])
    return '\n'.join(lines) if lines else '(no references)'


def _conflicts_text(objs):
    if not objs:
        return '(no identity conflicts)'
    return '\n\n'.join(_object_text(o) for o in objs)


def _files_text(rows):
    lines = []
    for r in rows:
        if r['status'] == 'error':
            lines.append('[batch %d] ERROR %s\n    reason: %s'
                         % (r['batch_id'], r['path'], r['error_reason']))
        else:
            lines.append('[batch %d] ok    %s -> object %s'
                         % (r['batch_id'], r['path'], r['object_uid']))
    return '\n'.join(lines) if lines else '(no file records)'


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='dicomcat',
        description='Local DICOM metadata catalog: identity, sources and typed '
                    'references. Reads no pixel data, talks to no PACS.')
    parser.add_argument('--json', action='store_true', help='machine-readable output')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('scan', help='import a set of file roots as one batch')
    p.add_argument('db')
    p.add_argument('roots', nargs='+')
    p.add_argument('--max-files', type=int, default=None,
                   help='stop after N files, leaving the batch running (simulated interruption)')

    for name in ('status', 'studies', 'series', 'references', 'conflicts', 'files', 'objects'):
        p = sub.add_parser(name)
        p.add_argument('db')
        p.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                       help='machine-readable output')
        if name == 'references':
            p.add_argument('--all', action='store_true',
                           help='include references of conflicted objects')
        if name == 'files':
            p.add_argument('--errors', action='store_true', help='only files with errors')
            p.add_argument('--batch', type=int, default=None)
        if name == 'objects':
            p.add_argument('--all', action='store_true',
                           help='include conflicted objects')

    p = sub.add_parser('object', help='full detail for one SOPInstanceUID')
    p.add_argument('db')
    p.add_argument('uid')
    p.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                   help='machine-readable output')

    p = sub.add_parser('close-batch', help='mark an interrupted batch as closed (partial)')
    p.add_argument('db')
    p.add_argument('batch_id', type=int)
    p.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                   help='machine-readable output')

    args = parser.parse_args(argv)

    with Catalog(args.db) as cat:
        if args.cmd == 'scan':
            result = cat.scan(args.roots, max_files=args.max_files)
            errors = cat.list_files(batch_id=result['batch_id'], errors_only=True)
            text = ['scan result: %s' % json.dumps(result)]
            if errors:
                text.append('file errors in this batch:')
                for e in errors:
                    text.append('  %s\n      %s' % (e['path'], e['error_reason']))
            _emit(args, {'result': result, 'errors': errors}, '\n'.join(text))
            return 0 if result['status'] != 'running' else 3
        if args.cmd == 'status':
            st = cat.status()
            _emit(args, st, _status_text(st))
        elif args.cmd == 'studies':
            rows = cat.list_studies()
            _emit(args, rows, _studies_text(rows))
        elif args.cmd == 'series':
            rows = cat.list_series()
            _emit(args, rows, _series_text(rows))
        elif args.cmd == 'objects':
            rows = cat.list_objects(usable_only=not args.all)
            _emit(args, rows, '\n'.join('%s %s %s' % (r['status'], r['sop_instance_uid'],
                                                      r['sop_class_uid']) for r in rows))
        elif args.cmd == 'object':
            obj = cat.get_object(args.uid)
            _emit(args, obj, _object_text(obj))
            return 0 if obj else 1
        elif args.cmd == 'references':
            refs = cat.list_references(include_conflicted=args.all)
            _emit(args, refs, _references_text(refs))
        elif args.cmd == 'conflicts':
            objs = cat.list_conflicts()
            _emit(args, objs, _conflicts_text(objs))
        elif args.cmd == 'files':
            rows = cat.list_files(batch_id=args.batch, errors_only=args.errors)
            _emit(args, rows, _files_text(rows))
        elif args.cmd == 'close-batch':
            ok = cat.close_batch(args.batch_id)
            _emit(args, {'closed': ok},
                  'batch %d closed' % args.batch_id if ok
                  else 'batch %d not closable (not running/interrupted?)' % args.batch_id)
            return 0 if ok else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
