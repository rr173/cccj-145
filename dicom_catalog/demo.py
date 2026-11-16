#!/usr/bin/env python3
"""主例演示：两个 Study、同 Study 的两个 Series、字节相同副本、冲突副本、
跨 Series 的迟到引用，以及 pending → resolved/missing/conflict 的完整生命周期。

运行: python3 demo.py
所有“手算预期”都写成常量并逐条断言；任何一步不符即抛 AssertionError。
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import make_samples
from dicom_catalog import Catalog

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
SAMPLES = os.path.join(DATA, 'demo_samples')
DB = os.path.join(DATA, 'demo_catalog.db')


def rss_bytes():
    with open('/proc/self/status') as f:
        for line in f:
            if line.startswith('VmRSS'):
                return int(line.split()[1]) * 1024
    return 0


def ref_status(view, target_uid):
    for ref in view['references']:
        if ref['ref_sop_instance_uid'] == target_uid:
            return ref['status']
    return None


def main():
    os.makedirs(DATA, exist_ok=True)
    for p in (DB, DB + '-wal', DB + '-shm'):
        if os.path.exists(p):
            os.remove(p)
    m = make_samples.build_samples(SAMPLES)
    U = m['uids']
    print('== 1. 生成纯合成样例 ==')
    print('  样例根目录:', m['root'])

    cat = Catalog(DB)

    # -- 2. 第一批扫描，模拟中途退出（处理到 obj2 之后停止） ----------------
    files = []
    for dirpath, _d, names in os.walk(m['delivery1']):
        files += [os.path.join(dirpath, n) for n in names]
    files.sort()
    stop_after = files.index(m['paths']['obj2']) + 1
    r = cat.scan([m['delivery1']], max_files=stop_after)
    assert r['status'] == 'running', r
    st = cat.status()
    print('\n== 2. 第一批扫描中途退出（模拟进程中断） ==')
    print('  批次状态: running, 目录总体状态:', st['overall'])
    assert st['overall'] == 'incomplete'

    # -- 3. 扫描未完成时查询：未到的目标只能是 pending ----------------------
    view = cat.get_object(U['obj-2'])
    s_x = ref_status(view, U['obj-x'])
    s_never = ref_status(view, U['obj-never'])
    s_obj1 = ref_status(view, U['obj-1'])
    print('\n== 3. 扫描进行中查询 obj2 的引用 ==')
    print('  obj2 -> objX     :', s_x, '（目标稍后到达）')
    print('  obj2 -> obj-never:', s_never, '（此时不能判 missing）')
    print('  obj2 -> obj1     :', s_obj1, '（obj1 已有冲突副本）')
    assert s_x == 'pending' and s_never == 'pending' and s_obj1 == 'conflict'

    # -- 4. 重新执行扫描：从未完成状态续接 ----------------------------------
    r = cat.scan([m['delivery1']])
    st = cat.status()
    print('\n== 4. 续传完成第一批 ==')
    print('  批次状态:', r['status'], '（含文件级错误 → partial）')
    assert r['status'] == 'partial'
    errors = cat.list_files(batch_id=r['batch_id'], errors_only=True)
    print('  文件级错误 %d 个:' % len(errors))
    for e in errors:
        print('   -', os.path.basename(e['path']), '=>', e['error_reason'][:80])
    assert len(errors) == 5  # metamismatch / missinguid / notdicom / truncated / parent_b
    view = cat.get_object(U['obj-2'])
    assert ref_status(view, U['obj-x']) == 'missing'      # 第一批扫完仍没有
    assert ref_status(view, U['obj-never']) == 'missing'
    assert ref_status(view, U['obj-1']) == 'conflict'

    # -- 5. 第二批到达：resolved / missing / conflict 的区别 ----------------
    rss_before = rss_bytes()
    r2 = cat.scan([m['delivery2']])
    rss_after = rss_bytes()
    assert r2['status'] == 'complete', r2
    view = cat.get_object(U['obj-2'])
    s_x = ref_status(view, U['obj-x'])
    print('\n== 5. 第二批（含 objX 与 1 GiB 稀疏像素文件）导入完成 ==')
    print('  obj2 -> objX     :', s_x, '（跨 Series 引用已解析）')
    print('  obj2 -> obj-never:', ref_status(view, U['obj-never']))
    print('  obj2 -> obj1     :', ref_status(view, U['obj-1']))
    obj5 = cat.get_object(U['obj-5'])
    print('  obj5 -> obj6     :', ref_status(obj5, U['obj-6']), '（SOP Class 不符）')
    assert s_x == 'resolved'
    assert ref_status(view, U['obj-never']) == 'missing'
    assert ref_status(view, U['obj-1']) == 'conflict'
    assert ref_status(obj5, U['obj-6']) == 'conflict'
    print('  扫描第二批前后进程 RSS 增量: %.1f MiB（像素未载入内存）'
          % ((rss_after - rss_before) / 2**20))
    assert rss_after - rss_before < 200 * 2**20, 'pixel data was loaded into memory!'

    # -- 6. 手算预期：每个 Series 的可用对象数 ------------------------------
    print('\n== 6. 每个 Series 的可用对象数（手算核对） ==')
    expect_series = {
        (U['study-a'], U['series-a1']): (2, 1),   # obj2,obj7 可用；obj1 冲突
        (U['study-a'], U['series-a2']): (1, 0),   # objX
        (U['study-a'], U['series-clash']): (1, 0),  # parent_a
        (U['study-b'], U['series-b1']): (2, 0),   # obj5,obj6
        (U['study-b'], U['series-b2']): (1, 0),   # bigpixel
    }
    got = {(s['study_uid'], s['series_uid']): (s['usable_objects'], s['conflicted_objects'])
           for s in cat.list_series()}
    for key in sorted(expect_series):
        print('  series …%s usable=%d conflicted=%d （预期 %s）'
              % (key[1][-12:], got[key][0], got[key][1], expect_series[key]))
        assert got[key] == expect_series[key], (key, got[key])

    # 对象级核对：obj1 冲突但来源与原因可查；字节相同副本合并为一个逻辑对象
    obj1 = cat.get_object(U['obj-1'])
    assert obj1['status'] == 'conflict'
    assert len(obj1['sources']) == 4          # 3 份字节相同 + 1 份冲突内容
    assert len(obj1['conflicts']) == 1
    assert obj1['metadata']['InstanceNumber'] == '1'   # 保留先到的内容，不被覆盖
    bases = sorted(s['basis'] for s in obj1['sources'])
    assert bases == ['conflicting-content', 'identical-bytes', 'identical-bytes', 'original']
    obj7 = cat.get_object(U['obj-7'])
    assert obj7['status'] == 'ok' and len(obj7['sources']) == 2
    assert sorted(s['basis'] for s in obj7['sources']) == ['metadata-identical', 'original']
    print('  obj1: 4 个来源（3 份字节相同 + 1 份冲突），冲突原因:', obj1['conflict_reason'][:60])
    print('  obj7: 2 个来源，去重依据 metadata-identical（字节不同、受索引元数据相同）')

    # -- 7. 完全重传：不增加逻辑对象或来源 ----------------------------------
    before = cat.status()['counts']
    r3 = cat.scan([m['delivery1'], m['delivery2']])
    after = cat.status()['counts']
    print('\n== 7. 完全重传同一文件集合 ==')
    print('  重传前 objects=%d sources=%d；重传后 objects=%d sources=%d'
          % (before['objects_ok'] + before['objects_conflict'], before['sources'],
             after['objects_ok'] + after['objects_conflict'], after['sources']))
    assert before['objects_ok'] == after['objects_ok']
    assert before['objects_conflict'] == after['objects_conflict']
    assert before['sources'] == after['sources']
    assert r3['status'] == 'partial'   # 重传仍如实报告同样的 5 个文件错误

    # -- 8. 稀疏大像素文件：不载入内存、不写入 SQLite ------------------------
    print('\n== 8. 稀疏大像素文件核对 ==')
    big = cat.get_object(U['obj-sparse'])
    assert big['status'] == 'ok' and len(big['sources']) == 1
    assert big['sources'][0]['size'] >= m['sparse']['pixel_length']
    cat.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    with open(DB, 'rb') as f:
        db_bytes = f.read()
    print('  目录数据库大小: %.1f KiB；声明像素: %d MiB'
          % (len(db_bytes) / 2**10, m['sparse']['pixel_length'] // 2**20))
    assert m['sparse']['marker'] not in db_bytes, 'pixel bytes leaked into SQLite'
    assert len(db_bytes) < 5 * 2**20
    print('  像素标记未出现在 SQLite 中 ✓')

    print('\n全部手算预期核对通过。ALL DEMO CHECKS PASSED')
    cat.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
