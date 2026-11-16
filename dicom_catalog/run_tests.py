#!/usr/bin/env python3
"""测试套件：覆盖任务要求的全部场景。

运行: python3 run_tests.py  （或 python3 -m unittest run_tests -v）
"""

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dicomio
import make_samples
from dicom_catalog import Catalog

CT = make_samples.CT_CLASS
MR = make_samples.MR_CLASS
uid_for = make_samples.uid_for

HERE = os.path.dirname(os.path.abspath(__file__))


def rss_bytes():
    with open('/proc/self/status') as f:
        for line in f:
            if line.startswith('VmRSS'):
                return int(line.split()[1]) * 1024
    return 0


class CatalogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='dicat-test-')
        self.data = os.path.join(self.tmp, 'data')
        os.makedirs(self.data)
        self.db = os.path.join(self.tmp, 'catalog.db')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, **kw):
        path = os.path.join(self.data, rel)
        kw.setdefault('sop_class_uid', CT)
        kw.setdefault('sop_instance_uid', uid_for('sop-' + rel))
        kw.setdefault('study_uid', uid_for('study'))
        kw.setdefault('series_uid', uid_for('series'))
        dicomio.write_dicom(path, **kw)
        return path

    def open(self):
        return Catalog(self.db)

    def refs_of(self, cat, uid):
        obj = cat.get_object(uid)
        return {r['ref_sop_instance_uid']: r['status'] for r in obj['references']}

    # ------------------------------------------------------------------ dedup

    def test_byte_identical_duplicates_one_object(self):
        uid = uid_for('obj-dup')
        p1 = self.write('dev1/a.dcm', sop_instance_uid=uid)
        p2 = os.path.join(self.data, 'dev2', 'b.dcm')
        p3 = os.path.join(self.data, 'dev3', 'deep', 'c.dcm')
        os.makedirs(os.path.dirname(p2))
        os.makedirs(os.path.dirname(p3))
        shutil.copyfile(p1, p2)
        shutil.copyfile(p1, p3)
        with self.open() as cat:
            res = cat.scan([self.data])
            self.assertEqual(res['status'], 'complete')
            objs = cat.list_objects()
            self.assertEqual(len(objs), 1)
            obj = cat.get_object(uid)
            self.assertEqual(len(obj['sources']), 3)
            self.assertEqual(sorted(s['basis'] for s in obj['sources']),
                             ['identical-bytes', 'identical-bytes', 'original'])
            self.assertEqual(len({s['content_hash'] for s in obj['sources']}), 1)

    def test_identity_conflict_not_overwritten(self):
        uid = uid_for('obj-conflict')
        self.write('a/first.dcm', sop_instance_uid=uid, instance_number='1',
                   patient_id='PT-1')
        self.write('b/second.dcm', sop_instance_uid=uid, instance_number='2',
                   patient_id='PT-2')
        with self.open() as cat:
            cat.scan([self.data])
            obj = cat.get_object(uid)
            self.assertEqual(obj['status'], 'conflict')
            # 先到的内容保留，不被后读的文件覆盖
            self.assertEqual(obj['metadata']['InstanceNumber'], '1')
            self.assertEqual(obj['metadata']['PatientID'], 'PT-1')
            self.assertEqual(len(obj['sources']), 2)
            self.assertEqual(len(obj['conflicts']), 1)
            diff = obj['conflicts'][0]['diff']
            self.assertEqual(diff['InstanceNumber'], ['1', '2'])
            self.assertEqual(diff['PatientID'], ['PT-1', 'PT-2'])
            # 冲突对象不进入可用查询，但来源与原因可查
            self.assertEqual(cat.list_objects(), [])
            series = cat.list_series()
            self.assertEqual(series[0]['usable_objects'], 0)
            self.assertEqual(series[0]['conflicted_objects'], 1)
            self.assertEqual(len(cat.list_conflicts()), 1)

    def test_bytes_differ_metadata_identical(self):
        uid = uid_for('obj-meta-same')
        self.write('a/x.dcm', sop_instance_uid=uid, patient_name='One')
        self.write('b/y.dcm', sop_instance_uid=uid, patient_name='Two')
        with self.open() as cat:
            cat.scan([self.data])
            obj = cat.get_object(uid)
            self.assertEqual(obj['status'], 'ok')
            self.assertEqual(len(obj['sources']), 2)
            self.assertEqual(sorted(s['basis'] for s in obj['sources']),
                             ['metadata-identical', 'original'])
            self.assertEqual(len({s['content_hash'] for s in obj['sources']}), 2)

    def test_same_uid_different_sop_class(self):
        uid = uid_for('obj-class-flip')
        self.write('a/ct.dcm', sop_instance_uid=uid, sop_class_uid=CT)
        self.write('b/mr.dcm', sop_instance_uid=uid, sop_class_uid=MR, modality='MR')
        with self.open() as cat:
            cat.scan([self.data])
            obj = cat.get_object(uid)
            self.assertEqual(obj['status'], 'conflict')
            self.assertIn('SOPClassUID', obj['conflicts'][0]['diff'])
            self.assertEqual(obj['sop_class_uid'], CT)  # 保留先到的声明

    # ------------------------------------------------------------ file errors

    def test_filemeta_dataset_mismatch(self):
        uid = uid_for('obj-metamismatch')
        self.write('bad1.dcm', sop_instance_uid=uid,
                   meta_sop_instance_uid=uid_for('other'))
        self.write('bad2.dcm', sop_instance_uid=uid_for('obj-metamismatch2'),
                   meta_sop_class_uid=MR)
        with self.open() as cat:
            res = cat.scan([self.data])
            self.assertEqual(res['status'], 'partial')
            errors = cat.list_files(errors_only=True)
            self.assertEqual(len(errors), 2)
            reasons = ' '.join(e['error_reason'] for e in errors)
            self.assertIn('MediaStorageSOPInstanceUID', reasons)
            self.assertIn('MediaStorageSOPClassUID', reasons)
            self.assertEqual(cat.list_objects(), [])  # 错误文件不产生对象

    def test_missing_and_invalid_uid(self):
        self.write('nouid.dcm', omit={'StudyInstanceUID'})
        self.write('baduid.dcm', sop_instance_uid='NOT A UID',
                   meta_sop_instance_uid='NOT A UID')
        with self.open() as cat:
            cat.scan([self.data])
            errors = cat.list_files(errors_only=True)
            self.assertEqual(len(errors), 2)
            reasons = sorted(e['error_reason'] for e in errors)
            self.assertIn('missing required StudyInstanceUID', reasons[1])
            self.assertIn('invalid SOPInstanceUID', reasons[0])

    def test_bad_files_and_partial_batch(self):
        with open(os.path.join(self.data, 'junk.dcm'), 'wb') as f:
            f.write(b'junk' * 64)
        good = self.write('good.dcm')
        with open(good, 'rb') as f:
            head = f.read(180)
        with open(os.path.join(self.data, 'trunc.dcm'), 'wb') as f:
            f.write(head)
        with self.open() as cat:
            res = cat.scan([self.data])
            self.assertEqual(res['status'], 'partial')
            st = cat.status()
            self.assertEqual(st['overall'], 'partial')
            self.assertEqual(st['counts']['files_error'], 2)
            self.assertEqual(st['counts']['files_ok'], 1)
            for e in cat.list_files(errors_only=True):
                self.assertTrue(e['error_reason'])  # 每个坏文件都有明确原因

    def test_series_parent_mismatch(self):
        series = uid_for('series-clash')
        self.write('a/one.dcm', series_uid=series, study_uid=uid_for('study-A'))
        self.write('b/two.dcm', series_uid=series, study_uid=uid_for('study-B'))
        with self.open() as cat:
            res = cat.scan([self.data])
            self.assertEqual(res['status'], 'partial')
            errors = cat.list_files(errors_only=True)
            self.assertEqual(len(errors), 1)
            self.assertIn('series-parent-mismatch', errors[0]['error_reason'])
            self.assertEqual(len(cat.list_objects()), 1)

    # ------------------------------------------------------------- batch life

    def test_interrupted_scan_resumes(self):
        target_uid = uid_for('late-target')
        for i in range(3):
            self.write('r%d.dcm' % i)
        self.write('referrer.dcm', references=[(CT, target_uid)])
        self.write('zz_target.dcm', sop_instance_uid=target_uid)
        cat = self.open()
        res = cat.scan([self.data], max_files=2)   # 模拟中途退出
        self.assertEqual(res['status'], 'running')
        self.assertEqual(cat.status()['overall'], 'incomplete')
        cat.close()                                   # 进程退出
        cat = self.open()                             # 重新执行：恢复并续接
        self.assertEqual(cat.status()['overall'], 'incomplete')
        res = cat.scan([self.data])
        self.assertEqual(res['status'], 'complete')
        self.assertEqual(len(cat.status()['batches']), 1)  # 同一批次续接
        files = cat.list_files(batch_id=res['batch_id'])
        self.assertEqual(len(files), 5)               # 每个文件恰好处理一次
        self.assertEqual(len({f['path'] for f in files}), 5)
        self.assertEqual(self.refs_of(cat, uid_for('sop-referrer.dcm'))[target_uid],
                         'resolved')
        cat.close()

    def test_pending_never_missing_while_incomplete(self):
        missing_uid = uid_for('not-here-yet')
        self.write('ref.dcm', references=[(CT, missing_uid)])
        cat = self.open()
        cat.scan([self.data], max_files=1)
        # 批次 running：引用是 pending 而不是 missing
        self.assertEqual(self.refs_of(cat, uid_for('sop-ref.dcm'))[missing_uid],
                         'pending')
        cat.close()
        # 进程重启后（interrupted）仍然是 pending
        with self.open() as cat:
            self.assertEqual(self.refs_of(cat, uid_for('sop-ref.dcm'))[missing_uid],
                             'pending')
        # 续传完成后才允许判 missing
        with self.open() as cat:
            cat.scan([self.data])
            self.assertEqual(self.refs_of(cat, uid_for('sop-ref.dcm'))[missing_uid],
                             'missing')

    def test_close_batch_makes_pending_missing(self):
        missing_uid = uid_for('gone')
        self.write('a_ref.dcm', references=[(CT, missing_uid)])
        self.write('z_other.dcm')
        with self.open() as cat:
            cat.scan([self.data], max_files=1)
            batch_id = cat.status()['batches'][0]['id']
            self.assertTrue(cat.close_batch(batch_id))
            self.assertEqual(cat.status()['overall'], 'partial')
            self.assertEqual(self.refs_of(cat, uid_for('sop-a_ref.dcm'))[missing_uid],
                             'missing')

    def test_full_retransmission_is_idempotent(self):
        uid = uid_for('obj-re')
        p1 = self.write('a/x.dcm', sop_instance_uid=uid)
        shutil.copyfile(p1, os.path.join(self.data, 'a', 'y.dcm'))
        self.write('b/z.dcm', references=[(CT, uid)])
        with self.open() as cat:
            cat.scan([self.data])
            first = cat.status()['counts'].copy()
            cat.scan([self.data])                     # 完全重传
            second = cat.status()['counts'].copy()
            self.assertEqual(first['objects_ok'], second['objects_ok'])
            self.assertEqual(first['sources'], second['sources'])
            self.assertEqual(first['references'], second['references'])
            self.assertEqual(len(cat.status()['batches']), 2)

    def test_same_path_new_content_keeps_both_sources(self):
        uid = uid_for('obj-swap')
        path = self.write('swap.dcm', sop_instance_uid=uid, instance_number='1')
        with self.open() as cat:
            cat.scan([self.data])
            dicomio.write_dicom(                      # 同一路径换成另一份内容
                path, sop_class_uid=CT, sop_instance_uid=uid,
                study_uid=uid_for('study'), series_uid=uid_for('series'),
                instance_number='2')
            cat.scan([self.data])
            obj = cat.get_object(uid)
            self.assertEqual(obj['status'], 'conflict')
            self.assertEqual(obj['metadata']['InstanceNumber'], '1')  # 不覆盖
            same_path = [s for s in obj['sources'] if s['path'] == path]
            self.assertEqual(len(same_path), 2)       # 新旧来源都保留
            self.assertEqual(len({s['content_hash'] for s in same_path}), 2)

    # -------------------------------------------------------- traversal order

    def test_traversal_order_does_not_change_results(self):
        uid_a, uid_b = uid_for('ord-a'), uid_for('ord-b')
        self.write('m/one.dcm', sop_instance_uid=uid_a, references=[(CT, uid_b)])
        self.write('m/two.dcm', sop_instance_uid=uid_b)
        self.write('m/dup.dcm', sop_instance_uid=uid_a, instance_number='9')
        self.write('n/three.dcm', references=[(MR, uid_for('ord-none'))])

        def reversed_walk(root):
            rows = list(os.walk(root))
            for dirpath, dirnames, filenames in reversed(rows):
                yield dirpath, list(reversed(dirnames)), list(reversed(filenames))

        results = []
        for walk_fn in (os.walk, reversed_walk):
            db = os.path.join(self.tmp, 'order.db')
            if os.path.exists(db):
                os.remove(db)
            with Catalog(db) as cat:
                cat.scan([self.data], walk_fn=walk_fn)
                snap = {
                    'objects': [(o['sop_instance_uid'], o['status']) for o in
                                cat.list_objects(usable_only=False)],
                    'series': [(s['series_uid'], s['usable_objects'],
                                s['conflicted_objects']) for s in cat.list_series()],
                    'refs': [(r['referrer_uid'], r['ref_sop_instance_uid'], r['status'])
                             for r in cat.list_references(include_conflicted=True)],
                    'sources': {o['sop_instance_uid']:
                                sorted(s['path'] for s in o['sources'])
                                for o in (cat.get_object(uid_a), cat.get_object(uid_b))},
                }
                results.append(snap)
        self.assertEqual(results[0], results[1])

    # ------------------------------------------------------------- references

    def test_reference_lifecycle_and_kinds(self):
        # 主例全流程的紧凑版：pending -> missing -> resolved + 两类 conflict
        m = make_samples.build_samples(os.path.join(self.tmp, 'samples'))
        U = m['uids']
        with self.open() as cat:
            cat.scan([m['delivery1']])
            refs = self.refs_of(cat, U['obj-2'])
            self.assertEqual(refs[U['obj-x']], 'missing')
            self.assertEqual(refs[U['obj-never']], 'missing')
            self.assertEqual(refs[U['obj-1']], 'conflict')   # 目标有冲突变体
            refs5 = self.refs_of(cat, U['obj-5'])
            self.assertEqual(refs5[U['obj-6']], 'conflict')  # SOP Class 不符
            cat.scan([m['delivery2']])
            refs = self.refs_of(cat, U['obj-2'])
            self.assertEqual(refs[U['obj-x']], 'resolved')
            self.assertEqual(refs[U['obj-never']], 'missing')
            # resolved 引用能定位到具体文件
            view = cat.get_object(U['obj-2'])
            for ref in view['references']:
                if ref['ref_sop_instance_uid'] == U['obj-x']:
                    self.assertEqual(ref['target']['sources'], [m['paths']['objX']])
                    self.assertIn('matching SOP Class', ref['reason'])

    def test_query_entry_point_structure(self):
        uid = uid_for('entry')
        tgt = uid_for('entry-tgt')
        self.write('a.dcm', sop_instance_uid=uid, references=[(CT, tgt)])
        self.write('b.dcm', sop_instance_uid=tgt)
        with self.open() as cat:
            cat.scan([self.data])
            obj = cat.get_object(uid)
            for key in ('sop_instance_uid', 'sop_class_uid', 'study_uid',
                        'series_uid', 'status', 'sources', 'references',
                        'conflicts', 'metadata'):
                self.assertIn(key, obj)
            ref = obj['references'][0]
            self.assertEqual(ref['status'], 'resolved')
            self.assertTrue(ref['reason'])
            self.assertTrue(ref['target']['sources'][0].endswith('b.dcm'))
            self.assertIsNone(cat.get_object(uid_for('unknown')))

    # ----------------------------------------------------------------- sparse

    def test_sparse_pixel_not_loaded_not_stored(self):
        m = make_samples.build_samples(os.path.join(self.tmp, 'samples'))
        with self.open() as cat:
            before = rss_bytes()
            cat.scan([m['delivery2']])
            after = rss_bytes()
            self.assertLess(after - before, 200 * 2**20,
                            '1 GiB pixel data appears to have been loaded')
            obj = cat.get_object(m['uids']['obj-sparse'])
            self.assertEqual(obj['status'], 'ok')
            self.assertEqual(len(obj['sources']), 1)
            cat.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        with open(self.db, 'rb') as f:
            db_bytes = f.read()
        self.assertNotIn(m['sparse']['marker'], db_bytes)
        self.assertLess(len(db_bytes), 5 * 2**20)

    # ----------------------------------------------------------------- parser

    def test_implicit_vr_little_endian(self):
        # 手工构造 Implicit VR LE 文件（文件元数据仍是 Explicit VR LE）
        uid = uid_for('implicit')
        tgt = uid_for('implicit-tgt')

        def meta_el(tag, vr, payload):
            return dicomio._el(tag, vr, payload)   # 文件元数据始终是 Explicit VR LE

        def ui(s):
            b = s.encode()
            return b + (b'\x00' if len(b) % 2 else b'')

        def el(tag, payload):
            return struct.pack('<HHI', tag[0], tag[1], len(payload)) + payload

        meta_body = (meta_el((0x0002, 0x0001), 'OB', b'\x00\x01')
                     + meta_el((0x0002, 0x0002), 'UI', ui(CT))
                     + meta_el((0x0002, 0x0003), 'UI', ui(uid))
                     + meta_el((0x0002, 0x0010), 'UI', ui(dicomio.TS_IMPLICIT_LE)))
        meta = meta_el((0x0002, 0x0000), 'UL', struct.pack('<I', len(meta_body))) + meta_body
        item = el((0x0008, 0x1150), ui(CT)) + el((0x0008, 0x1155), ui(tgt))
        seq = el((0x0008, 0x1199), struct.pack('<HHI', 0xFFFE, 0xE000, len(item)) + item)
        ds = (el((0x0008, 0x0016), ui(CT)) + el((0x0008, 0x0018), ui(uid))
              + el((0x0020, 0x000D), ui(uid_for('study')))
              + el((0x0020, 0x0052), ui(uid_for('series'))) + seq)
        path = os.path.join(self.data, 'implicit.dcm')
        with open(path, 'wb') as f:
            f.write(b'\x00' * 128 + b'DICM' + meta + ds)
        with self.open() as cat:
            res = cat.scan([self.data])
            self.assertEqual(res['status'], 'complete')
            obj = cat.get_object(uid)
            self.assertIsNotNone(obj)
            self.assertEqual(obj['references'][0]['ref_sop_instance_uid'], tgt)
            self.assertEqual(obj['references'][0]['status'], 'missing')

    # -------------------------------------------------------------------- CLI

    def test_cli_smoke(self):
        self.write('cli.dcm')
        env = dict(os.environ)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        scan = subprocess.run(
            [sys.executable, os.path.join(HERE, 'cli.py'), 'scan', self.db, self.data],
            capture_output=True, text=True, env=env)
        self.assertEqual(scan.returncode, 0, scan.stderr)
        self.assertIn('complete', scan.stdout)
        status = subprocess.run(
            [sys.executable, os.path.join(HERE, 'cli.py'), 'status', self.db, '--json'],
            capture_output=True, text=True, env=env)
        self.assertEqual(status.returncode, 0, status.stderr)
        parsed = json.loads(status.stdout)
        self.assertEqual(parsed['overall'], 'complete')
        obj = subprocess.run(
            [sys.executable, os.path.join(HERE, 'cli.py'), 'object', self.db,
             uid_for('sop-cli.dcm')],
            capture_output=True, text=True, env=env)
        self.assertEqual(obj.returncode, 0, obj.stderr)
        self.assertIn('sources', obj.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
