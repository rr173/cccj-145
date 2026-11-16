#!/usr/bin/env python3
"""生成可重复（逐字节确定）的纯合成 DICOM 样例树，不依赖任何真实影像。

目录结构（一次导入两个“交付批次”，模拟多台设备/多个导出目录）：

delivery1/                        第一批离线检查包
  scanner1/export_a/obj1.dcm            CT, StudyA/SeriesA1, inst 1
  scanner1/export_a/obj1_dup.dcm        obj1 的字节相同副本（同目录）
  scanner2/other_folder/dup_of_obj1.dcm obj1 的字节相同副本（另一设备目录）
  scanner1/export_a/obj1_conflict.dcm   与 obj1 同 SOPInstanceUID，元数据不同 → 身份冲突
  scanner1/export_a/obj2.dcm            引用 objX（跨 Series，第二批才到）、
                                        obj-never（永远不到）、obj1（冲突目标）
  scanner1/export_a/obj7.dcm            inst 7
  scanner1/export_a/obj7_variant.dcm    与 obj7 字节不同但受索引元数据完全相同
  scanner1/export_b/obj5.dcm            MR, StudyB/SeriesB1，引用 obj6 但写错 SOP Class
  scanner1/export_b/obj6.dcm            MR, StudyB/SeriesB1
  problem/metamismatch.dcm              文件元数据 UID 与数据集不一致 → 文件级错误
  problem/missinguid.dcm                缺 StudyInstanceUID → 文件级错误
  problem/notdicom.dcm                  非 DICOM → 文件级错误
  problem/truncated.dcm                 截断文件 → 文件级错误
  problem/parent_a.dcm                  SeriesClash 归入 StudyA（正常）
  problem/parent_b.dcm                  SeriesClash 又声明属于 StudyB → 文件级错误
delivery2/                        第二批（稍后到达）
  scanner3/late/objX.dcm                obj2 跨 Series 引用的对象（SeriesA2）
  scanner3/late/bigpixel.dcm            含 1 GiB 稀疏像素数据的对象

用法: python3 make_samples.py OUTDIR
"""

import hashlib
import os
import shutil
import sys

import dicomio

CT_CLASS = '1.2.840.10008.5.1.4.1.1.2'   # CT Image Storage
MR_CLASS = '1.2.840.10008.5.1.4.1.1.4'   # MR Image Storage

SPARSE_PIXEL_LENGTH = 1 << 30            # 1 GiB declared PixelData
SPARSE_MARKER = b'DICOM-CATALOG-SPARSE-PIXEL-MARKER-v1'
SPARSE_MARKER_OFFSET = 4096              # marker position inside pixel data

LABELS = ['study-a', 'study-b',
          'series-a1', 'series-a2', 'series-b1', 'series-b2', 'series-clash',
          'obj-1', 'obj-2', 'obj-5', 'obj-6', 'obj-7', 'obj-x', 'obj-never',
          'obj-parent-a', 'obj-parent-b', 'obj-sparse', 'obj-badmeta', 'obj-nouid']


def uid_for(label):
    """Deterministic, valid UID derived from a human-readable label."""
    n = int.from_bytes(
        hashlib.sha256(('dicom-catalog-sample:' + label).encode('utf-8')).digest()[:16],
        'big')
    return '2.25.%d' % n


UID = {label: uid_for(label) for label in LABELS}


def build_samples(root):
    root = os.path.abspath(root)
    if os.path.exists(root):
        shutil.rmtree(root)
    paths = {}

    def write(rel, **kw):
        path = os.path.join(root, rel)
        dicomio.write_dicom(path, **kw)
        return path

    study_a = dict(study_uid=UID['study-a'], study_date='20240102')
    study_b = dict(study_uid=UID['study-b'], study_date='20240103')

    # --- delivery 1 -------------------------------------------------------
    paths['obj1'] = write(
        'delivery1/scanner1/export_a/obj1.dcm',
        sop_class_uid=CT_CLASS, sop_instance_uid=UID['obj-1'], modality='CT',
        series_uid=UID['series-a1'], series_number='1', instance_number='1',
        series_description='CT head', patient_id='PT-A', patient_name='Alpha',
        pixel=b'\x11\x22' * 32, **study_a)
    paths['obj1_dup'] = os.path.join(root, 'delivery1/scanner1/export_a/obj1_dup.dcm')
    shutil.copyfile(paths['obj1'], paths['obj1_dup'])
    paths['obj1_dup2'] = os.path.join(root, 'delivery1/scanner2/other_folder/dup_of_obj1.dcm')
    os.makedirs(os.path.dirname(paths['obj1_dup2']), exist_ok=True)
    shutil.copyfile(paths['obj1'], paths['obj1_dup2'])

    paths['obj1_conflict'] = write(
        'delivery1/scanner1/export_a/obj1_conflict.dcm',
        sop_class_uid=CT_CLASS, sop_instance_uid=UID['obj-1'], modality='CT',
        series_uid=UID['series-a1'], series_number='1', instance_number='9',
        series_description='CT head RECON', patient_id='PT-CONFLICT',
        patient_name='Alpha', **study_a)

    paths['obj2'] = write(
        'delivery1/scanner1/export_a/obj2.dcm',
        sop_class_uid=CT_CLASS, sop_instance_uid=UID['obj-2'], modality='CT',
        series_uid=UID['series-a1'], series_number='1', instance_number='2',
        series_description='CT head', patient_id='PT-A', patient_name='Beta',
        references=[(CT_CLASS, UID['obj-x']),       # arrives in delivery2, SeriesA2
                    (CT_CLASS, UID['obj-never']),   # never arrives
                    (CT_CLASS, UID['obj-1'])],      # conflicted target
        **study_a)

    paths['obj7'] = write(
        'delivery1/scanner1/export_a/obj7.dcm',
        sop_class_uid=CT_CLASS, sop_instance_uid=UID['obj-7'], modality='CT',
        series_uid=UID['series-a1'], series_number='1', instance_number='7',
        series_description='CT head', patient_id='PT-A', patient_name='Gamma',
        **study_a)
    paths['obj7_variant'] = write(
        'delivery1/scanner1/export_a/obj7_variant.dcm',
        sop_class_uid=CT_CLASS, sop_instance_uid=UID['obj-7'], modality='CT',
        series_uid=UID['series-a1'], series_number='1', instance_number='7',
        series_description='CT head', patient_id='PT-A', patient_name='Gamma-Renamed',
        **study_a)

    paths['obj5'] = write(
        'delivery1/scanner1/export_b/obj5.dcm',
        sop_class_uid=MR_CLASS, sop_instance_uid=UID['obj-5'], modality='MR',
        series_uid=UID['series-b1'], series_number='1', instance_number='1',
        series_description='MR knee', patient_id='PT-B', patient_name='Delta',
        references=[(CT_CLASS, UID['obj-6'])],      # wrong class on purpose
        **study_b)
    paths['obj6'] = write(
        'delivery1/scanner1/export_b/obj6.dcm',
        sop_class_uid=MR_CLASS, sop_instance_uid=UID['obj-6'], modality='MR',
        series_uid=UID['series-b1'], series_number='1', instance_number='2',
        series_description='MR knee', patient_id='PT-B', patient_name='Epsilon',
        **study_b)

    # --- problem files ------------------------------------------------------
    paths['badmeta'] = write(
        'delivery1/problem/metamismatch.dcm',
        sop_class_uid=CT_CLASS, sop_instance_uid=UID['obj-badmeta'], modality='CT',
        series_uid=UID['series-a1'], series_number='1', instance_number='90',
        patient_id='PT-A', meta_sop_instance_uid=UID['obj-1'], **study_a)
    paths['nouid'] = write(
        'delivery1/problem/missinguid.dcm',
        sop_class_uid=CT_CLASS, sop_instance_uid=UID['obj-nouid'], modality='CT',
        series_uid=UID['series-a1'], series_number='1', instance_number='91',
        patient_id='PT-A', omit={'StudyInstanceUID'}, **study_a)
    paths['notdicom'] = os.path.join(root, 'delivery1/problem/notdicom.dcm')
    os.makedirs(os.path.dirname(paths['notdicom']), exist_ok=True)
    with open(paths['notdicom'], 'wb') as f:
        f.write(b'This is not a DICOM file, just placeholder bytes.\n' * 8)
    paths['truncated'] = os.path.join(root, 'delivery1/problem/truncated.dcm')
    with open(paths['obj2'], 'rb') as f:
        head = f.read(200)
    with open(paths['truncated'], 'wb') as f:
        f.write(head)
    paths['parent_a'] = write(
        'delivery1/problem/parent_a.dcm',
        sop_class_uid=CT_CLASS, sop_instance_uid=UID['obj-parent-a'], modality='CT',
        series_uid=UID['series-clash'], series_number='5', instance_number='1',
        patient_id='PT-A', **study_a)
    paths['parent_b'] = write(
        'delivery1/problem/parent_b.dcm',
        sop_class_uid=MR_CLASS, sop_instance_uid=UID['obj-parent-b'], modality='MR',
        series_uid=UID['series-clash'], series_number='5', instance_number='2',
        patient_id='PT-B', **study_b)

    # --- delivery 2 -------------------------------------------------------
    paths['objX'] = write(
        'delivery2/scanner3/late/objX.dcm',
        sop_class_uid=CT_CLASS, sop_instance_uid=UID['obj-x'], modality='CT',
        series_uid=UID['series-a2'], series_number='2', instance_number='1',
        series_description='CT head follow-up', patient_id='PT-A', patient_name='Zeta',
        **study_a)
    paths['sparse'] = write(
        'delivery2/scanner3/late/bigpixel.dcm',
        sop_class_uid=MR_CLASS, sop_instance_uid=UID['obj-sparse'], modality='MR',
        series_uid=UID['series-b2'], series_number='3', instance_number='1',
        series_description='MR big sparse', patient_id='PT-B', patient_name='Eta',
        pixel_length=SPARSE_PIXEL_LENGTH, **study_b)
    size = os.path.getsize(paths['sparse'])
    with open(paths['sparse'], 'r+b') as f:
        f.seek(size - SPARSE_PIXEL_LENGTH + SPARSE_MARKER_OFFSET)
        f.write(SPARSE_MARKER)

    return {
        'root': root,
        'delivery1': os.path.join(root, 'delivery1'),
        'delivery2': os.path.join(root, 'delivery2'),
        'uids': dict(UID),
        'classes': {'CT': CT_CLASS, 'MR': MR_CLASS},
        'paths': paths,
        'sparse': {'path': paths['sparse'], 'pixel_length': SPARSE_PIXEL_LENGTH,
                   'marker': SPARSE_MARKER, 'marker_offset': SPARSE_MARKER_OFFSET},
    }


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        print('usage: python3 make_samples.py OUTDIR')
        return 2
    manifest = build_samples(argv[1])
    n_files = sum(len(files) for _, _, files in os.walk(manifest['root']))
    print('samples written to %s (%d files)' % (manifest['root'], n_files))
    print('sparse pixel file: %s (%d bytes declared PixelData)'
          % (manifest['sparse']['path'], manifest['sparse']['pixel_length']))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
