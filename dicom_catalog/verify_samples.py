#!/usr/bin/env python3
"""独立校验脚本：直接检查合成样例文件头与预期关系。

本脚本故意不导入产品的任何模块（dicomio / dicom_catalog / make_samples），
而是用自己的最小解析器读取文件头，验证样例树是否满足主例所声称的关系：

  1. obj1 的三份导出字节完全相同；
  2. obj1_conflict 与 obj1 的 SOPInstanceUID 相同但 InstanceNumber 不同；
  3. obj7 与 obj7_variant 字节不同、SOPInstanceUID 与 InstanceNumber 相同；
  4. obj2 的 ReferencedSOPSequence 引用 objX（跨 Series）、一个永不到达的 UID、obj1；
  5. obj5 引用 obj6 时写的 SOP Class 与 obj6 实际 SOPClassUID 不符；
  6. 除 metamismatch.dcm 外，所有文件的文件元数据 UID 与数据集 UID 一致；
  7. missinguid.dcm 缺 StudyInstanceUID；notdicom.dcm 无 DICM 前缀；
     truncated.dcm 解析必然失败；
  8. bigpixel.dcm 声明 1 GiB PixelData，文件是稀疏的，且标记字节在像素区内。

用法: python3 verify_samples.py SAMPLE_ROOT
退出码 0 = 全部通过，1 = 有失败项。
"""

import hashlib
import os
import struct
import sys

PIXEL_DATA = (0x7FE0, 0x0010)
REF_SEQ = (0x0008, 0x1199)
REF_CLASS = (0x0008, 0x1150)
REF_INST = (0x0008, 0x1155)
LONG_VRS = {b'OB', b'OD', b'OF', b'OL', b'OV', b'OW', b'SQ',
            b'UC', b'UR', b'UT', b'UN', b'SV', b'UV'}

WANTED = {
    (0x0002, 0x0002): 'MediaStorageSOPClassUID',
    (0x0002, 0x0003): 'MediaStorageSOPInstanceUID',
    (0x0008, 0x0016): 'SOPClassUID',
    (0x0008, 0x0018): 'SOPInstanceUID',
    (0x0020, 0x000D): 'StudyInstanceUID',
    (0x0020, 0x0052): 'SeriesInstanceUID',
    (0x0020, 0x0013): 'InstanceNumber',
}


class ParseFailure(Exception):
    pass


def _read_header(f):
    raw = f.read(4)
    if len(raw) < 4:
        raise ParseFailure('truncated tag')
    tag = struct.unpack('<HH', raw)
    if tag in ((0xFFFE, 0xE000), (0xFFFE, 0xE00D), (0xFFFE, 0xE0DD)):
        (length,) = struct.unpack('<I', _exact(f, 4))
        return tag, None, length
    vr = _exact(f, 2)
    if vr in LONG_VRS:
        _exact(f, 2)
        (length,) = struct.unpack('<I', _exact(f, 4))
    else:
        (length,) = struct.unpack('<H', _exact(f, 2))
    return tag, vr.decode('ascii', 'replace'), length


def _exact(f, n):
    data = f.read(n)
    if len(data) != n:
        raise ParseFailure('unexpected end of file')
    return data


def parse_file(path):
    """Independent minimal reader: identity tags, references, pixel extent."""
    info = {'tags': {}, 'references': [], 'pixel': None}
    with open(path, 'rb') as f:
        head = f.read(132)
        if len(head) < 132 or head[128:132] != b'DICM':
            raise ParseFailure('missing DICM prefix')
        while True:  # file meta (always explicit VR LE)
            pos = f.tell()
            tag, vr, length = _read_header(f)
            if tag[0] != 0x0002:
                f.seek(pos)
                break
            _walk(f, tag, vr, length, info, 0)
        _elements(f, None, info, 0)
    return info


def _elements(f, end, info, depth):
    if depth > 8:
        raise ParseFailure('nesting too deep')
    while True:
        if end is not None and f.tell() >= end:
            return
        pos = f.tell()
        try:
            tag, vr, length = _read_header(f)
        except ParseFailure:
            if end is None:
                return
            raise
        if tag in ((0xFFFE, 0xE00D), (0xFFFE, 0xE0DD)):
            return
        if tag == (0xFFFE, 0xE000):
            raise ParseFailure('item outside sequence')
        _walk(f, tag, vr, length, info, depth)


def _walk(f, tag, vr, length, info, depth):
    if tag == PIXEL_DATA:
        info['pixel'] = {'offset': f.tell(), 'length': length}
        if length == 0xFFFFFFFF:
            raise ParseFailure('encapsulated pixel data not expected in samples')
        f.seek(length, os.SEEK_CUR)
        return
    if vr == 'SQ':
        end = f.tell() + length
        while f.tell() < end:
            itag, _vr, ilen = _read_header(f)
            if itag != (0xFFFE, 0xE000):
                raise ParseFailure('expected item in sequence')
            iend = f.tell() + ilen
            if tag == REF_SEQ:
                local = {}
                _elements_into(f, iend, local)
                info['references'].append(
                    (local.get('class', ''), local.get('inst', '')))
            else:
                _elements(f, iend, info, depth + 1)
        return
    if tag in WANTED:
        if length > 1 << 20:
            raise ParseFailure('value too large')
        info['tags'][WANTED[tag]] = _exact(f, length).decode('latin-1').rstrip(' \x00')
    else:
        f.seek(length, os.SEEK_CUR)


def _elements_into(f, end, local):
    while f.tell() < end:
        tag, vr, length = _read_header(f)
        if tag == REF_CLASS:
            local['class'] = _exact(f, length).decode('latin-1').rstrip(' \x00')
        elif tag == REF_INST:
            local['inst'] = _exact(f, length).decode('latin-1').rstrip(' \x00')
        else:
            f.seek(length, os.SEEK_CUR)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = os.path.abspath(argv[1])
    checks = []

    def check(name, ok, detail=''):
        checks.append((name, ok, detail))

    def P(rel):
        return os.path.join(root, rel)

    parsed = {}

    def meta(rel):
        if rel not in parsed:
            parsed[rel] = parse_file(P(rel))
        return parsed[rel]

    obj1 = 'delivery1/scanner1/export_a/obj1.dcm'
    obj1_dup = 'delivery1/scanner1/export_a/obj1_dup.dcm'
    obj1_dup2 = 'delivery1/scanner2/other_folder/dup_of_obj1.dcm'
    obj1_conflict = 'delivery1/scanner1/export_a/obj1_conflict.dcm'
    obj2 = 'delivery1/scanner1/export_a/obj2.dcm'
    obj7 = 'delivery1/scanner1/export_a/obj7.dcm'
    obj7v = 'delivery1/scanner1/export_a/obj7_variant.dcm'
    obj5 = 'delivery1/scanner1/export_b/obj5.dcm'
    obj6 = 'delivery1/scanner1/export_b/obj6.dcm'
    objx = 'delivery2/scanner3/late/objX.dcm'
    sparse = 'delivery2/scanner3/late/bigpixel.dcm'

    # 1. byte-identical exports of obj1
    hashes = {rel: sha256(P(rel)) for rel in (obj1, obj1_dup, obj1_dup2)}
    check('obj1 三份导出字节相同', len(set(hashes.values())) == 1, str(hashes))

    # 2. conflict copy: same SOPInstanceUID, different InstanceNumber
    a, b = meta(obj1)['tags'], meta(obj1_conflict)['tags']
    check('冲突副本 SOPInstanceUID 相同',
          a['SOPInstanceUID'] == b['SOPInstanceUID'], '%s vs %s' % (a, b))
    check('冲突副本 InstanceNumber 不同',
          a.get('InstanceNumber') != b.get('InstanceNumber'),
          '%r vs %r' % (a.get('InstanceNumber'), b.get('InstanceNumber')))

    # 3. obj7 variant: different bytes, same identity metadata
    check('obj7 两版字节不同', sha256(P(obj7)) != sha256(P(obj7v)))
    t1, t2 = meta(obj7)['tags'], meta(obj7v)['tags']
    check('obj7 两版 SOPInstanceUID/InstanceNumber 相同',
          t1['SOPInstanceUID'] == t2['SOPInstanceUID']
          and t1.get('InstanceNumber') == t2.get('InstanceNumber'))

    # 4. obj2 references: objX (cross-series), a never-arriving UID, obj1
    refs2 = meta(obj2)['references']
    ref_insts = {inst for _cls, inst in refs2}
    x_tags = meta(objx)['tags']
    check('obj2 引用 objX（跨 Series）',
          x_tags['SOPInstanceUID'] in ref_insts
          and x_tags['SeriesInstanceUID'] != meta(obj2)['tags']['SeriesInstanceUID'],
          'refs=%s' % (refs2,))
    all_uids = set()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if rel.endswith('truncated.dcm') or rel.endswith('notdicom.dcm'):
                continue
            try:
                all_uids.add(meta(rel)['tags'].get('SOPInstanceUID'))
            except ParseFailure:
                pass
    never_refs = [i for i in ref_insts if i not in all_uids]
    check('obj2 引用一个目录中不存在的 UID', len(never_refs) == 1, str(never_refs))
    check('obj2 引用 obj1', meta(obj1)['tags']['SOPInstanceUID'] in ref_insts)

    # 5. obj5 -> obj6 with wrong SOP Class
    refs5 = meta(obj5)['references']
    obj6_uid = meta(obj6)['tags']['SOPInstanceUID']
    obj6_class = meta(obj6)['tags']['SOPClassUID']
    mismatched = [c for c, i in refs5 if i == obj6_uid and c != obj6_class]
    check('obj5 引用 obj6 的 SOP Class 不符', len(mismatched) == 1,
          'refs=%s, obj6 class=%s' % (refs5, obj6_class))

    # 6. file meta vs dataset consistency
    mismatch_rel = 'delivery1/problem/metamismatch.dcm'
    ok_all, bad_ok = True, False
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if rel in (os.path.relpath(P('delivery1/problem/notdicom.dcm'), root),
                       os.path.relpath(P('delivery1/problem/truncated.dcm'), root)):
                continue
            try:
                t = meta(rel)['tags']
            except ParseFailure:
                continue
            consistent = (t.get('MediaStorageSOPClassUID') == t.get('SOPClassUID')
                          and t.get('MediaStorageSOPInstanceUID') == t.get('SOPInstanceUID'))
            if rel == os.path.relpath(P(mismatch_rel), root):
                bad_ok = not consistent
            else:
                ok_all = ok_all and consistent
    check('其余文件 文件元数据UID==数据集UID', ok_all)
    check('metamismatch.dcm 确实不一致', bad_ok)

    # 7. problem files behave as expected
    check('missinguid.dcm 缺 StudyInstanceUID',
          'StudyInstanceUID' not in meta('delivery1/problem/missinguid.dcm')['tags'])
    try:
        meta('delivery1/problem/notdicom.dcm')
        check('notdicom.dcm 解析失败', False)
    except ParseFailure:
        check('notdicom.dcm 解析失败', True)
    try:
        meta('delivery1/problem/truncated.dcm')
        check('truncated.dcm 解析失败', False)
    except ParseFailure:
        check('truncated.dcm 解析失败', True)

    # 8. sparse pixel file
    st = os.stat(P(sparse))
    pix = meta(sparse)['pixel']
    check('bigpixel.dcm 声明 1 GiB PixelData',
          pix is not None and pix['length'] == (1 << 30), str(pix))
    check('bigpixel.dcm 是稀疏文件',
          st.st_blocks * 512 < st.st_size // 2,
          'blocks=%d size=%d' % (st.st_blocks, st.st_size))
    with open(P(sparse), 'rb') as f:
        f.seek(pix['offset'] + 4096)
        marker = f.read(36)
    check('像素区内含校验标记', marker == b'DICOM-CATALOG-SPARSE-PIXEL-MARKER-v1',
          repr(marker))

    failed = 0
    for name, ok, detail in checks:
        print('%s %s %s' % ('PASS' if ok else 'FAIL', name,
                            ('| ' + detail) if (detail and not ok) else ''))
        failed += 0 if ok else 1
    print('%d/%d checks passed' % (len(checks) - failed, len(checks)))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
