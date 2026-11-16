"""Local DICOM metadata catalog (SQLite-backed).

Identity model
--------------
File names and folder layout are *not* identity.  Objects are organised by
StudyInstanceUID / SeriesInstanceUID / SOPInstanceUID.  Every catalogued
object declares its Study, Series and SOP Class; the file-meta
MediaStorageSOPClassUID / MediaStorageSOPInstanceUID must equal the dataset
SOPClassUID / SOPInstanceUID, and a series may belong to exactly one study.
Missing values, malformed UIDs and inconsistencies are recorded per file as
explicit failure reasons - files are never silently skipped.

Deduplication model
-------------------
Same SOPInstanceUID, identical bytes        -> one logical object, more sources
Same SOPInstanceUID, different bytes but
  identical indexed metadata                -> one logical object, more sources
                                             (basis 'metadata-identical')
Same SOPInstanceUID, different indexed
  metadata (Study / Series / SOP Class /    -> identity conflict: the object is
  any indexed attribute incl. references)     marked 'conflict', earlier content
                                             is kept, every variant's source is
                                             recorded, and the object leaves the
                                             usable-object queries.

Reference model
---------------
Each ReferencedSOPSequence item is a typed reference
(ReferencedSOPClassUID, ReferencedSOPInstanceUID).  Statuses are computed at
query time from catalog state, so results do not depend on traversal order:
  resolved  - target object exists, is usable, SOP Class matches
  pending   - target not found while at least one batch is still incomplete
  missing   - target not found and every batch finished scanning its roots
  conflict  - target UID has conflicting content variants, or the stored
              target's SOP Class differs from the referenced class

Batch model
-----------
One scan() call over a set of roots is one batch.  Batch states:
  running     - scan in progress (a crashed process leaves batches here)
  interrupted - found unfinished at startup; can be resumed by re-scanning
                the same roots, or closed explicitly with close_batch()
  complete    - every file under the roots was processed without file errors
  partial     - every file was processed but some files have errors
While any batch is running/interrupted, unresolved references are 'pending',
never 'missing'.  Re-importing the same complete file set adds neither
objects nor sources.  If a path is re-seen with different content, the old
source is kept and the new source is added next to it.
"""

import hashlib
import json
import os
import re
import sqlite3
import time

import dicomio

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY,
    roots_json      TEXT NOT NULL,          -- JSON array of absolute roots (sorted)
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL
                    CHECK (status IN ('running','interrupted','complete','partial')),
    total_files     INTEGER NOT NULL DEFAULT 0,
    processed_files INTEGER NOT NULL DEFAULT 0,
    error_files     INTEGER NOT NULL DEFAULT 0,
    note            TEXT
);
CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    batch_id      INTEGER NOT NULL REFERENCES batches(id),
    path          TEXT NOT NULL,
    size          INTEGER,
    mtime         REAL,
    content_hash  TEXT,                     -- sha256 of the file bytes (NULL if unreadable)
    status        TEXT NOT NULL CHECK (status IN ('ok','error')),
    error_reason  TEXT,                     -- explicit failure reason when status='error'
    object_uid    TEXT,                     -- SOPInstanceUID this file was catalogued as
    processed_at  TEXT NOT NULL,
    UNIQUE (batch_id, path)
);
CREATE TABLE IF NOT EXISTS objects (
    id               INTEGER PRIMARY KEY,
    sop_instance_uid TEXT NOT NULL UNIQUE,  -- logical object identity
    sop_class_uid    TEXT NOT NULL,
    study_uid        TEXT NOT NULL,
    series_uid       TEXT NOT NULL,
    metadata_json    TEXT NOT NULL,         -- canonical indexed metadata
    metadata_hash    TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('ok','conflict')) DEFAULT 'ok',
    conflict_reason  TEXT,
    first_seen_batch INTEGER NOT NULL REFERENCES batches(id)
);
CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY,
    object_id       INTEGER NOT NULL REFERENCES objects(id),
    path            TEXT NOT NULL,
    content_hash    TEXT NOT NULL,          -- sha256 of the file bytes at this location
    size            INTEGER NOT NULL,
    basis           TEXT NOT NULL,          -- original | identical-bytes |
                                            -- metadata-identical | conflicting-content
    first_seen_batch INTEGER NOT NULL REFERENCES batches(id),
    UNIQUE (object_id, path, content_hash)  -- re-importing the same set adds nothing
);
CREATE TABLE IF NOT EXISTS conflicts (
    id           INTEGER PRIMARY KEY,
    object_id    INTEGER NOT NULL REFERENCES objects(id),
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    path         TEXT NOT NULL,             -- file that carried the conflicting content
    content_hash TEXT NOT NULL,
    diff_json    TEXT NOT NULL,             -- {field: [stored_value, incoming_value]}
    detected_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS object_references (
    id                  INTEGER PRIMARY KEY,
    referrer_id         INTEGER NOT NULL REFERENCES objects(id),
    ref_sop_class_uid   TEXT NOT NULL,
    ref_sop_instance_uid TEXT NOT NULL,
    UNIQUE (referrer_id, ref_sop_class_uid, ref_sop_instance_uid)
);
CREATE INDEX IF NOT EXISTS idx_files_batch   ON files(batch_id);
CREATE INDEX IF NOT EXISTS idx_sources_obj   ON sources(object_id);
CREATE INDEX IF NOT EXISTS idx_sources_path  ON sources(path);
CREATE INDEX IF NOT EXISTS idx_objects_series ON objects(series_uid);
CREATE INDEX IF NOT EXISTS idx_refs_referrer ON object_references(referrer_id);
CREATE INDEX IF NOT EXISTS idx_refs_target   ON object_references(ref_sop_instance_uid);
"""

# Indexed DICOM attributes: these (plus the typed reference list) define the
# comparable metadata content of an object.  Anything else (e.g. PatientName)
# may differ between byte-different copies without causing a conflict.
INDEXED_TAGS = [
    'SOPClassUID', 'SOPInstanceUID', 'StudyInstanceUID', 'SeriesInstanceUID',
    'Modality', 'StudyDate', 'StudyTime', 'SeriesNumber', 'InstanceNumber',
    'SeriesDescription', 'PatientID',
]

REQUIRED_UIDS = ['SOPClassUID', 'SOPInstanceUID', 'StudyInstanceUID', 'SeriesInstanceUID']

_UID_RE = re.compile(r'^[0-9]+(\.[0-9]+)*$')


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _valid_uid(value):
    return (isinstance(value, str) and 0 < len(value) <= 64
            and bool(_UID_RE.match(value)))


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _diff_metadata(stored, incoming):
    diff = {}
    for key in sorted(set(stored) | set(incoming)):
        old, new = stored.get(key), incoming.get(key)
        if old != new:
            diff[key] = [old, new]
    return diff


class Catalog:
    """Python API entry point.  One Catalog = one SQLite database file."""

    def __init__(self, db_path):
        self.db_path = os.path.abspath(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')      # readers work during scans
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.executescript(SCHEMA)
        self._recover_interrupted()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ scan

    def _recover_interrupted(self):
        # A batch still marked 'running' means the previous process died
        # mid-scan (or stopped early on purpose).  Mark it 'interrupted' so it
        # is resumable and so references stay 'pending' instead of 'missing'.
        self.conn.execute("UPDATE batches SET status='interrupted' WHERE status='running'")
        self.conn.commit()

    def scan(self, roots, max_files=None, walk_fn=None):
        """Import every file found under *roots* (recursively) as one batch.

        Files are processed in sorted-path order, so the result never depends
        on filesystem traversal order.  *max_files* stops the scan early and
        leaves the batch 'running' (simulated interruption, for tests/demos).
        *walk_fn* replaces os.walk (test hook for traversal-order changes).
        """
        roots = [os.path.abspath(r) for r in roots]
        for r in roots:
            if not os.path.isdir(r):
                raise ValueError('scan root is not a directory: %s' % r)
        walk_fn = walk_fn or os.walk
        paths = []
        for root in sorted(roots):
            for dirpath, dirnames, filenames in walk_fn(root):
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    if os.path.isfile(full) and not os.path.islink(full):
                        paths.append(full)
        paths.sort()
        return self._run_batch(paths, json.dumps(sorted(roots)), max_files)

    def _run_batch(self, paths, roots_json, max_files):
        self._recover_interrupted()
        row = self.conn.execute(
            "SELECT id FROM batches WHERE status='interrupted' AND roots_json=?",
            (roots_json,)).fetchone()
        if row:
            batch_id = row['id']     # resume the unfinished batch for these roots
            self.conn.execute("UPDATE batches SET status='running' WHERE id=?", (batch_id,))
        else:
            cur = self.conn.execute(
                "INSERT INTO batches (roots_json, started_at, status, total_files)"
                " VALUES (?, ?, 'running', ?)", (roots_json, _now(), len(paths)))
            batch_id = cur.lastrowid
        self.conn.commit()

        done = {r['path'] for r in self.conn.execute(
            'SELECT path FROM files WHERE batch_id=?', (batch_id,))}
        processed = 0
        for path in paths:
            if path in done:
                continue             # already checked by this batch: never re-judge
            self._process_file(batch_id, path)
            self.conn.commit()       # per-file commit: crash-safe progress
            processed += 1
            if max_files is not None and processed >= max_files:
                self._refresh_batch_counts(batch_id)
                self.conn.commit()
                return {'batch_id': batch_id, 'status': 'running', 'stopped_early': True,
                        'processed_now': processed}
        errors = self.conn.execute(
            "SELECT COUNT(*) AS c FROM files WHERE batch_id=? AND status='error'",
            (batch_id,)).fetchone()['c']
        final = 'partial' if errors else 'complete'
        self.conn.execute('UPDATE batches SET status=?, finished_at=? WHERE id=?',
                          (final, _now(), batch_id))
        self._refresh_batch_counts(batch_id)
        self.conn.commit()
        return {'batch_id': batch_id, 'status': final, 'stopped_early': False,
                'processed_now': processed}

    def _refresh_batch_counts(self, batch_id):
        self.conn.execute(
            """UPDATE batches SET
                 processed_files = (SELECT COUNT(*) FROM files WHERE batch_id=?),
                 error_files = (SELECT COUNT(*) FROM files
                                WHERE batch_id=? AND status='error')
               WHERE id=?""", (batch_id, batch_id, batch_id))

    def _process_file(self, batch_id, path):
        try:
            st = os.stat(path)
            size, mtime = st.st_size, st.st_mtime
        except OSError as exc:
            self._record_file_error(batch_id, path, None, None,
                                    'cannot stat file: %s' % exc)
            return
        try:
            parsed = dicomio.read_identity(path)
        except (dicomio.DicomError, OSError) as exc:
            self._record_file_error(batch_id, path, size, mtime, 'read error: %s' % exc)
            return
        meta, dataset, refs = parsed['meta'], parsed['dataset'], parsed['references']

        problems = []
        for key in REQUIRED_UIDS:
            value = dataset.get(key, '')
            if not value:
                problems.append('missing required %s' % key)
            elif not _valid_uid(value):
                problems.append('invalid %s %r' % (key, value))
        if not problems:
            ms_class = meta.get('MediaStorageSOPClassUID', '')
            ms_inst = meta.get('MediaStorageSOPInstanceUID', '')
            if ms_class != dataset['SOPClassUID']:
                problems.append(
                    'file meta MediaStorageSOPClassUID %r != dataset SOPClassUID %r'
                    % (ms_class, dataset['SOPClassUID']))
            if ms_inst != dataset['SOPInstanceUID']:
                problems.append(
                    'file meta MediaStorageSOPInstanceUID %r != dataset SOPInstanceUID %r'
                    % (ms_inst, dataset['SOPInstanceUID']))
        if problems:
            self._record_file_error(batch_id, path, size, mtime, '; '.join(problems))
            return

        # A series must sit under exactly one study (checked against usable
        # objects only; conflicted objects no longer define structure).
        for row in self.conn.execute(
                "SELECT DISTINCT study_uid FROM objects WHERE series_uid=? AND status='ok'",
                (dataset['SeriesInstanceUID'],)):
            if row['study_uid'] != dataset['StudyInstanceUID']:
                self._record_file_error(
                    batch_id, path, size, mtime,
                    'series-parent-mismatch: series %s is already recorded under study %s, '
                    'but this file declares study %s'
                    % (dataset['SeriesInstanceUID'], row['study_uid'],
                       dataset['StudyInstanceUID']))
                return

        content_hash = _sha256_file(path)
        metadata = {key: dataset.get(key, '') for key in INDEXED_TAGS}
        metadata['references'] = [list(pair) for pair in refs]
        metadata_json = json.dumps(metadata, sort_keys=True)
        metadata_hash = hashlib.sha256(metadata_json.encode('utf-8')).hexdigest()

        obj = self.conn.execute('SELECT * FROM objects WHERE sop_instance_uid=?',
                                (dataset['SOPInstanceUID'],)).fetchone()
        if obj is None:
            cur = self.conn.execute(
                """INSERT INTO objects
                     (sop_instance_uid, sop_class_uid, study_uid, series_uid,
                      metadata_json, metadata_hash, status, first_seen_batch)
                   VALUES (?,?,?,?,?,?, 'ok', ?)""",
                (dataset['SOPInstanceUID'], dataset['SOPClassUID'],
                 dataset['StudyInstanceUID'], dataset['SeriesInstanceUID'],
                 metadata_json, metadata_hash, batch_id))
            object_id = cur.lastrowid
            basis = 'original'
            for ref_class, ref_inst in refs:
                self.conn.execute(
                    """INSERT OR IGNORE INTO object_references
                         (referrer_id, ref_sop_class_uid, ref_sop_instance_uid)
                       VALUES (?,?,?)""", (object_id, ref_class, ref_inst))
        else:
            object_id = obj['id']
            if obj['metadata_hash'] == metadata_hash:
                seen_hash = self.conn.execute(
                    'SELECT 1 FROM sources WHERE object_id=? AND content_hash=? LIMIT 1',
                    (object_id, content_hash)).fetchone()
                basis = 'identical-bytes' if seen_hash else 'metadata-identical'
            else:
                diff = _diff_metadata(json.loads(obj['metadata_json']), metadata)
                reason = ('identity-conflict: same SOPInstanceUID but indexed fields '
                          'differ: ' + ', '.join(sorted(diff)))
                self.conn.execute(
                    "UPDATE objects SET status='conflict',"
                    " conflict_reason=COALESCE(conflict_reason, ?) WHERE id=?",
                    (reason, object_id))
                self.conn.execute(
                    """INSERT INTO conflicts
                         (object_id, batch_id, path, content_hash, diff_json, detected_at)
                       VALUES (?,?,?,?,?,?)""",
                    (object_id, batch_id, path, content_hash,
                     json.dumps(diff, sort_keys=True), _now()))
                basis = 'conflicting-content'
        self.conn.execute(
            """INSERT OR IGNORE INTO sources
                 (object_id, path, content_hash, size, basis, first_seen_batch)
               VALUES (?,?,?,?,?,?)""",
            (object_id, path, content_hash, size, basis, batch_id))
        self.conn.execute(
            """INSERT INTO files
                 (batch_id, path, size, mtime, content_hash, status, object_uid, processed_at)
               VALUES (?,?,?,?,?, 'ok', ?, ?)""",
            (batch_id, path, size, mtime, content_hash,
             dataset['SOPInstanceUID'], _now()))

    def _record_file_error(self, batch_id, path, size, mtime, reason):
        self.conn.execute(
            """INSERT INTO files
                 (batch_id, path, size, mtime, content_hash, status, error_reason,
                  object_uid, processed_at)
               VALUES (?,?,?,?,NULL, 'error', ?, NULL, ?)""",
            (batch_id, path, size, mtime, reason, _now()))

    def close_batch(self, batch_id):
        """Explicitly close an interrupted batch that will not be resumed.

        The batch becomes 'partial' with a note; its unprocessed files are
        *not* treated as nonexistent - they were simply never checked.
        """
        cur = self.conn.execute(
            """UPDATE batches SET status='partial', finished_at=?,
                 note=COALESCE(note || ' ', '') ||
                      'closed manually before completion; unchecked files remain unknown'
               WHERE id=? AND status IN ('running','interrupted')""",
            (_now(), batch_id))
        self._refresh_batch_counts(batch_id)
        self.conn.commit()
        return cur.rowcount > 0

    # ---------------------------------------------------------------- queries

    def status(self):
        batches = [dict(r) for r in
                   self.conn.execute('SELECT * FROM batches ORDER BY id')]
        if not batches:
            overall = 'empty'
        elif any(b['status'] in ('running', 'interrupted') for b in batches):
            overall = 'incomplete'
        elif any(b['status'] == 'partial' for b in batches):
            overall = 'partial'
        else:
            overall = 'complete'
        counts = {}
        for key, sql in [
                ('objects_ok', "SELECT COUNT(*) c FROM objects WHERE status='ok'"),
                ('objects_conflict', "SELECT COUNT(*) c FROM objects WHERE status='conflict'"),
                ('sources', 'SELECT COUNT(*) c FROM sources'),
                ('references', 'SELECT COUNT(*) c FROM object_references'),
                ('files_ok', "SELECT COUNT(*) c FROM files WHERE status='ok'"),
                ('files_error', "SELECT COUNT(*) c FROM files WHERE status='error'"),
                ('conflict_events', 'SELECT COUNT(*) c FROM conflicts')]:
            counts[key] = self.conn.execute(sql).fetchone()['c']
        ref_status = {}
        for ref in self.list_references(include_conflicted=True):
            ref_status[ref['status']] = ref_status.get(ref['status'], 0) + 1
        return {'overall': overall, 'batches': batches, 'counts': counts,
                'reference_status': ref_status}

    def list_studies(self):
        agg = {}
        for r in self.conn.execute('SELECT study_uid, series_uid, status FROM objects'):
            a = agg.setdefault(r['study_uid'], {'study_uid': r['study_uid'],
                                                'series': set(),
                                                'usable_objects': 0,
                                                'conflicted_objects': 0})
            a['series'].add(r['series_uid'])
            a['usable_objects' if r['status'] == 'ok' else 'conflicted_objects'] += 1
        out = []
        for a in agg.values():
            a['series'] = sorted(a['series'])
            out.append(a)
        return sorted(out, key=lambda a: a['study_uid'])

    def list_series(self):
        """Per-series usable/conflicted object counts (conflicts are not usable)."""
        agg = {}
        for r in self.conn.execute(
                'SELECT study_uid, series_uid, status, metadata_json FROM objects'):
            key = (r['study_uid'], r['series_uid'])
            a = agg.setdefault(key, {'study_uid': key[0], 'series_uid': key[1],
                                     'usable_objects': 0, 'conflicted_objects': 0,
                                     'modalities': set(), 'sop_classes': set()})
            meta = json.loads(r['metadata_json'])
            if r['status'] == 'ok':
                a['usable_objects'] += 1
                if meta.get('Modality'):
                    a['modalities'].add(meta['Modality'])
                a['sop_classes'].add(meta.get('SOPClassUID', ''))
            else:
                a['conflicted_objects'] += 1
        out = []
        for a in agg.values():
            a['modalities'] = sorted(a['modalities'])
            a['sop_classes'] = sorted(a['sop_classes'])
            out.append(a)
        return sorted(out, key=lambda a: (a['study_uid'], a['series_uid']))

    def list_objects(self, usable_only=True):
        sql = ('SELECT sop_instance_uid, sop_class_uid, study_uid, series_uid, status'
               ' FROM objects')
        if usable_only:
            sql += " WHERE status='ok'"
        sql += ' ORDER BY study_uid, series_uid, sop_instance_uid'
        return [dict(r) for r in self.conn.execute(sql)]

    def get_object(self, sop_instance_uid):
        """Query entry point: identity, all sources, outgoing typed references.

        Returns None for an unknown UID.  Conflicted objects are returned too
        (they are excluded from usable queries, never from review)."""
        obj = self.conn.execute('SELECT * FROM objects WHERE sop_instance_uid=?',
                                (sop_instance_uid,)).fetchone()
        if obj is None:
            return None
        object_id = obj['id']
        sources = [dict(r) for r in self.conn.execute(
            """SELECT path, content_hash, size, basis, first_seen_batch
               FROM sources WHERE object_id=? ORDER BY path, content_hash""",
            (object_id,))]
        conflicts = [dict(r) for r in self.conn.execute(
            'SELECT batch_id, path, content_hash, diff_json, detected_at'
            ' FROM conflicts WHERE object_id=? ORDER BY id', (object_id,))]
        for c in conflicts:
            c['diff'] = json.loads(c.pop('diff_json'))
        references = []
        for r in self.conn.execute(
                """SELECT ref_sop_class_uid, ref_sop_instance_uid FROM object_references
                   WHERE referrer_id=? ORDER BY ref_sop_instance_uid, ref_sop_class_uid""",
                (object_id,)):
            references.append(self._reference_view(r['ref_sop_class_uid'],
                                                   r['ref_sop_instance_uid']))
        return {'sop_instance_uid': obj['sop_instance_uid'],
                'sop_class_uid': obj['sop_class_uid'],
                'study_uid': obj['study_uid'],
                'series_uid': obj['series_uid'],
                'status': obj['status'],
                'conflict_reason': obj['conflict_reason'],
                'metadata': json.loads(obj['metadata_json']),
                'first_seen_batch': obj['first_seen_batch'],
                'sources': sources,
                'references': references,
                'conflicts': conflicts}

    def list_references(self, include_conflicted=False):
        """All typed references with their current status (query-time computed)."""
        sql = """SELECT r.ref_sop_class_uid, r.ref_sop_instance_uid,
                        o.sop_instance_uid AS referrer_uid,
                        o.study_uid, o.series_uid, o.status AS referrer_status
                 FROM object_references r JOIN objects o ON o.id = r.referrer_id"""
        if not include_conflicted:
            sql += " WHERE o.status='ok'"
        sql += ' ORDER BY o.sop_instance_uid, r.ref_sop_instance_uid, r.ref_sop_class_uid'
        out = []
        for r in self.conn.execute(sql):
            view = self._reference_view(r['ref_sop_class_uid'], r['ref_sop_instance_uid'])
            view.update({'referrer_uid': r['referrer_uid'],
                         'referrer_study_uid': r['study_uid'],
                         'referrer_series_uid': r['series_uid'],
                         'referrer_status': r['referrer_status']})
            out.append(view)
        return out

    def _reference_view(self, ref_class, ref_inst):
        status, reason, target = self._resolve_reference(ref_class, ref_inst)
        return {'ref_sop_class_uid': ref_class, 'ref_sop_instance_uid': ref_inst,
                'status': status, 'reason': reason, 'target': target}

    def _resolve_reference(self, ref_class, ref_inst):
        obj = self.conn.execute('SELECT * FROM objects WHERE sop_instance_uid=?',
                                (ref_inst,)).fetchone()
        if obj is None:
            open_batches = [r['id'] for r in self.conn.execute(
                "SELECT id FROM batches WHERE status IN ('running','interrupted')")]
            if open_batches:
                return ('pending',
                        'target not found so far; import batch(es) %s still incomplete, '
                        'the file may arrive later or sit outside the scanned roots'
                        % open_batches, None)
            return ('missing',
                    'no object with this SOPInstanceUID in the catalog and all import '
                    'batches finished scanning their roots', None)
        target = {'sop_instance_uid': obj['sop_instance_uid'],
                  'sop_class_uid': obj['sop_class_uid'],
                  'study_uid': obj['study_uid'],
                  'series_uid': obj['series_uid'],
                  'status': obj['status'],
                  'sources': [r['path'] for r in self.conn.execute(
                      'SELECT path FROM sources WHERE object_id=? ORDER BY path',
                      (obj['id'],))]}
        if obj['status'] == 'conflict':
            return ('conflict',
                    'target SOPInstanceUID has conflicting content variants (%s); '
                    'no single valid target' % (obj['conflict_reason'] or 'see conflicts'),
                    target)
        if obj['sop_class_uid'] != ref_class:
            return ('conflict',
                    'SOP Class mismatch: reference declares %s but the stored object is %s'
                    % (ref_class, obj['sop_class_uid']), target)
        return ('resolved',
                'target object found with matching SOP Class; see target.sources', target)

    def list_conflicts(self):
        out = []
        for obj in self.conn.execute(
                "SELECT * FROM objects WHERE status='conflict' ORDER BY sop_instance_uid"):
            detail = self.get_object(obj['sop_instance_uid'])
            out.append(detail)
        return out

    def list_files(self, batch_id=None, errors_only=False):
        sql = ('SELECT batch_id, path, size, content_hash, status, error_reason,'
               ' object_uid, processed_at FROM files')
        clauses, args = [], []
        if batch_id is not None:
            clauses.append('batch_id=?')
            args.append(batch_id)
        if errors_only:
            clauses.append("status='error'")
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY batch_id, path'
        return [dict(r) for r in self.conn.execute(sql, args)]
