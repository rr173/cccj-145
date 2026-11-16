"""Minimal DICOM Part-10 reader (identity metadata only) + deterministic writer.

Design rules for the reader:
  * PixelData (7FE0,0010) is NEVER loaded into memory.  A defined-length
    PixelData value is skipped with seek(); an encapsulated (undefined
    length) PixelData stops parsing because nothing the catalog indexes can
    follow it.
  * Only the tags the catalog needs are materialised; every other value is
    skipped by seeking, so memory use stays tiny regardless of file size.
  * Sequences are walked recursively (defined and undefined length) so that
    ReferencedSOPSequence (0008,1199) items are found wherever they nest.
  * Anything unexpected raises DicomError with an explicit reason; the
    caller records that reason per file instead of guessing.

Supported transfer syntaxes:
  * Explicit VR Little Endian (1.2.840.10008.1.2.1) and the whole explicit-VR
    family (compressed syntaxes included - pixel data is never decoded)
  * Implicit VR Little Endian (1.2.840.10008.1.2)
  * Explicit VR Big Endian (1.2.840.10008.1.2.2)
  * Deflated Explicit VR LE is rejected with a clear error.

The writer produces deterministic, pure-synthetic files for the sample
generator and the tests.  It only implements what the samples need.
"""

import os
import struct

TS_IMPLICIT_LE = '1.2.840.10008.1.2'
TS_EXPLICIT_LE = '1.2.840.10008.1.2.1'
TS_EXPLICIT_BE = '1.2.840.10008.1.2.2'
TS_DEFLATED_LE = '1.2.840.10008.1.2.1.99'

PIXEL_DATA = (0x7FE0, 0x0010)
ITEM = (0xFFFE, 0xE000)
ITEM_DELIMITATION = (0xFFFE, 0xE00D)
SEQUENCE_DELIMITATION = (0xFFFE, 0xE0DD)

REFERENCED_SOP_SEQUENCE = (0x0008, 0x1199)
REFERENCED_SOP_CLASS_UID = (0x0008, 0x1150)
REFERENCED_SOP_INSTANCE_UID = (0x0008, 0x1155)

# Explicit-VR value representations whose length field is 4 bytes (with 2
# reserved bytes in between), as 2-byte strings.
LONG_VRS = {b'OB', b'OD', b'OF', b'OL', b'OV', b'OW', b'SQ',
            b'UC', b'UR', b'UT', b'UN', b'SV', b'UV'}
LONG_VRS_STR = {v.decode('ascii') for v in LONG_VRS}

# Dataset tags the catalog indexes: tag -> (keyword, VR).
DATASET_TAGS = {
    (0x0008, 0x0016): ('SOPClassUID', 'UI'),
    (0x0008, 0x0018): ('SOPInstanceUID', 'UI'),
    (0x0008, 0x0020): ('StudyDate', 'DA'),
    (0x0008, 0x0030): ('StudyTime', 'TM'),
    (0x0008, 0x0060): ('Modality', 'CS'),
    (0x0008, 0x103E): ('SeriesDescription', 'LO'),
    (0x0010, 0x0020): ('PatientID', 'LO'),
    (0x0020, 0x000D): ('StudyInstanceUID', 'UI'),
    (0x0020, 0x0052): ('SeriesInstanceUID', 'UI'),
    (0x0020, 0x0011): ('SeriesNumber', 'IS'),
    (0x0020, 0x0013): ('InstanceNumber', 'IS'),
}

# File meta tags the catalog needs: tag -> (keyword, VR).
META_TAGS = {
    (0x0002, 0x0002): ('MediaStorageSOPClassUID', 'UI'),
    (0x0002, 0x0003): ('MediaStorageSOPInstanceUID', 'UI'),
    (0x0002, 0x0010): ('TransferSyntaxUID', 'UI'),
}

# Tags found inside ReferencedSOPSequence items.
REF_ITEM_TAGS = {
    REFERENCED_SOP_CLASS_UID: ('ReferencedSOPClassUID', 'UI'),
    REFERENCED_SOP_INSTANCE_UID: ('ReferencedSOPInstanceUID', 'UI'),
}

# VR lookup used only for implicit-VR datasets (wanted tags + sequences).
IMPLICIT_VR = {}
for _tag, (_kw, _vr) in list(DATASET_TAGS.items()) + list(META_TAGS.items()) + list(REF_ITEM_TAGS.items()):
    IMPLICIT_VR[_tag] = _vr
IMPLICIT_VR[REFERENCED_SOP_SEQUENCE] = 'SQ'

MAX_VALUE_LENGTH = 1 << 20   # never read a single value larger than 1 MiB
MAX_DEPTH = 8                # sequence nesting guard


class DicomError(Exception):
    """Explicit, per-file reason why a file could not be catalogued."""


class _CleanEOF(Exception):
    """EOF exactly at an element boundary (fine at the top level only)."""


class _StopParsing(Exception):
    """Internal: encapsulated pixel data reached; nothing needed beyond."""


def _read_exact(f, n, what):
    data = f.read(n)
    if len(data) != n:
        raise DicomError('unexpected end of file while reading %s' % what)
    return data


def _decode_text(raw):
    # String-ish VRs are padded with a trailing space (UI with a NUL).
    return raw.decode('latin-1').rstrip(' \x00')


def _read_element_header(f, explicit, endian):
    """Return (tag, vr, length); length may be 0xFFFFFFFF (undefined)."""
    raw = f.read(4)
    if raw == b'':
        raise _CleanEOF()
    if len(raw) < 4:
        raise DicomError('unexpected end of file while reading element tag')
    tag = struct.unpack(endian + 'HH', raw)
    if tag in (ITEM, ITEM_DELIMITATION, SEQUENCE_DELIMITATION):
        length = struct.unpack(endian + 'I', _read_exact(f, 4, 'item length'))[0]
        return tag, None, length
    if explicit:
        vr_raw = _read_exact(f, 2, 'value representation')
        if vr_raw in LONG_VRS:
            _read_exact(f, 2, 'reserved bytes')
            length = struct.unpack(endian + 'I', _read_exact(f, 4, 'value length'))[0]
        else:
            length = struct.unpack(endian + 'H', _read_exact(f, 2, 'value length'))[0]
        try:
            vr = vr_raw.decode('ascii')
        except UnicodeDecodeError:
            raise DicomError('invalid VR bytes %r' % (vr_raw,))
        return tag, vr, length
    length = struct.unpack(endian + 'I', _read_exact(f, 4, 'value length'))[0]
    return tag, IMPLICIT_VR.get(tag, 'UN'), length


def _handle_element(f, tag, vr, length, explicit, endian, out, refs, depth, wanted):
    if tag == PIXEL_DATA:
        if length == 0xFFFFFFFF:
            # Encapsulated (compressed) pixel data: items follow, and nothing
            # the catalog indexes can appear afterwards.
            raise _StopParsing()
        f.seek(length, os.SEEK_CUR)   # skip pixel bytes without reading them
        return
    if vr == 'SQ':
        _parse_sequence(f, tag, length, explicit, endian, out, refs, depth)
        return
    if length == 0xFFFFFFFF:
        raise DicomError('undefined-length element %s that is neither a sequence nor pixel data'
                         % (_fmt_tag(tag),))
    if wanted is not None and tag in wanted:
        keyword, _vr = wanted[tag]
        if length > MAX_VALUE_LENGTH:
            raise DicomError('element %s value too large (%d bytes)' % (keyword, length))
        out[keyword] = _decode_text(_read_exact(f, length, keyword))
    else:
        f.seek(length, os.SEEK_CUR)


def _parse_elements(f, end, explicit, endian, out, refs, depth, wanted):
    if depth > MAX_DEPTH:
        raise DicomError('sequence nesting deeper than %d levels' % MAX_DEPTH)
    while True:
        if end is not None and f.tell() >= end:
            return
        try:
            tag, vr, length = _read_element_header(f, explicit, endian)
        except _CleanEOF:
            if end is None:
                return
            raise DicomError('unexpected end of file inside element stream')
        if tag in (ITEM_DELIMITATION, SEQUENCE_DELIMITATION):
            return
        if tag == ITEM:
            raise DicomError('item element (FFFE,E000) outside a sequence')
        _handle_element(f, tag, vr, length, explicit, endian, out, refs, depth, wanted)


def _parse_sequence(f, sq_tag, length, explicit, endian, out, refs, depth):
    is_ref_seq = (sq_tag == REFERENCED_SOP_SEQUENCE)
    seq_end = None if length == 0xFFFFFFFF else f.tell() + length
    while True:
        if seq_end is not None and f.tell() >= seq_end:
            return
        try:
            tag, _vr, ilen = _read_element_header(f, explicit, endian)
        except _CleanEOF:
            raise DicomError('unexpected end of file inside sequence %s' % _fmt_tag(sq_tag))
        if tag == SEQUENCE_DELIMITATION:
            if seq_end is not None:
                raise DicomError('sequence delimitation item inside a defined-length sequence')
            return
        if tag != ITEM:
            raise DicomError('expected item (FFFE,E000) in sequence %s, found %s'
                             % (_fmt_tag(sq_tag), _fmt_tag(tag)))
        item_end = None if ilen == 0xFFFFFFFF else f.tell() + ilen
        if is_ref_seq:
            local = {}
            _parse_elements(f, item_end, explicit, endian, local, [], depth + 1, REF_ITEM_TAGS)
            cls = local.get('ReferencedSOPClassUID', '')
            inst = local.get('ReferencedSOPInstanceUID', '')
            if not cls or not inst:
                raise DicomError('ReferencedSOPSequence item lacks '
                                 'ReferencedSOPClassUID/ReferencedSOPInstanceUID')
            refs.append((cls, inst))
        else:
            # Values nested inside other sequences are not indexed (they would
            # be misread as top-level identity); only nested references matter.
            _parse_elements(f, item_end, explicit, endian, {}, refs, depth + 1, None)


def _fmt_tag(tag):
    return '(%04X,%04X)' % tag


def read_identity(path):
    """Read file-meta, identity metadata and typed references from *path*.

    Returns {'meta': {...}, 'dataset': {...},
             'references': [(ref_class_uid, ref_instance_uid), ...]} sorted.
    Raises DicomError with an explicit reason on any problem.
    PixelData is never read into memory.
    """
    with open(path, 'rb') as f:
        head = f.read(132)
        if len(head) < 132:
            raise DicomError('file too small (%d bytes) for the 128-byte preamble + DICM prefix'
                             % len(head))
        if head[128:132] != b'DICM':
            raise DicomError("missing 'DICM' prefix after the 128-byte preamble; "
                             'not a DICOM Part-10 file')
        meta = {}
        while True:
            pos = f.tell()
            try:
                tag, vr, length = _read_element_header(f, True, '<')
            except _CleanEOF:
                raise DicomError('no file meta information elements found')
            if tag[0] != 0x0002:
                f.seek(pos)
                break
            _handle_element(f, tag, vr, length, True, '<', meta, None, 0, META_TAGS)
        ts = meta.get('TransferSyntaxUID', '')
        if not ts:
            raise DicomError('file meta information does not include TransferSyntaxUID')
        if ts == TS_IMPLICIT_LE:
            explicit, endian = False, '<'
        elif ts == TS_EXPLICIT_BE:
            explicit, endian = True, '>'
        elif ts == TS_DEFLATED_LE:
            raise DicomError('unsupported transfer syntax %s (deflated)' % ts)
        else:
            # Explicit VR Little Endian and the compressed explicit-VR family.
            explicit, endian = True, '<'
        dataset = {}
        refs = []
        try:
            _parse_elements(f, None, explicit, endian, dataset, refs, 0, DATASET_TAGS)
        except _StopParsing:
            pass
        return {'meta': meta, 'dataset': dataset, 'references': sorted(set(refs))}


# ---------------------------------------------------------------------------
# Deterministic writer for pure-synthetic sample files (Explicit VR LE only).
# ---------------------------------------------------------------------------

_IMPL_CLASS_UID = '2.25.12345678901234567890123456789012345678'


def _el(tag, vr, payload):
    head = struct.pack('<HH', tag[0], tag[1]) + vr.encode('ascii')
    if vr in LONG_VRS_STR:
        return head + b'\x00\x00' + struct.pack('<I', len(payload)) + payload
    return head + struct.pack('<H', len(payload)) + payload


def _ui(value):
    raw = value.encode('ascii')
    return raw if len(raw) % 2 == 0 else raw + b'\x00'


def _tx(value):
    raw = value.encode('ascii')
    return raw if len(raw) % 2 == 0 else raw + b' '


def _sq(tag, items):
    payload = b''.join(struct.pack('<HHI', 0xFFFE, 0xE000, len(it)) + it for it in items)
    return _el(tag, 'SQ', payload)


def write_dicom(path, *, sop_class_uid, sop_instance_uid, study_uid, series_uid,
                modality='CT', series_number='1', instance_number='1',
                study_date='20240102', study_time='120000',
                series_description='', patient_id='', patient_name='',
                references=(), pixel=b'', pixel_length=None,
                meta_sop_class_uid=None, meta_sop_instance_uid=None, omit=()):
    """Write a deterministic synthetic DICOM Part-10 file.

    references: iterable of (ReferencedSOPClassUID, ReferencedSOPInstanceUID).
    pixel: inline pixel bytes (padded to even length).
    pixel_length: if given, a sparse file is produced whose PixelData element
        declares this many bytes; the bytes themselves are never written
        (filesystem hole).  Mutually exclusive with a non-empty ``pixel``.
    meta_sop_class_uid / meta_sop_instance_uid: override file-meta UIDs (used
        to create meta-vs-dataset mismatch samples).
    omit: set of dataset keywords to leave out (e.g. {'StudyInstanceUID'}).
    """
    ms_class = meta_sop_class_uid or sop_class_uid
    ms_inst = meta_sop_instance_uid or sop_instance_uid
    meta_body = (
        _el((0x0002, 0x0001), 'OB', b'\x00\x01')
        + _el((0x0002, 0x0002), 'UI', _ui(ms_class))
        + _el((0x0002, 0x0003), 'UI', _ui(ms_inst))
        + _el((0x0002, 0x0010), 'UI', _ui(TS_EXPLICIT_LE))
        + _el((0x0002, 0x0012), 'UI', _ui(_IMPL_CLASS_UID))
    )
    meta = _el((0x0002, 0x0000), 'UL', struct.pack('<I', len(meta_body))) + meta_body

    ds = b''

    def add(tag, vr, payload, keyword):
        nonlocal ds
        if keyword not in omit:
            ds += _el(tag, vr, payload)

    add((0x0008, 0x0016), 'UI', _ui(sop_class_uid), 'SOPClassUID')
    add((0x0008, 0x0018), 'UI', _ui(sop_instance_uid), 'SOPInstanceUID')
    add((0x0008, 0x0020), 'DA', _tx(study_date), 'StudyDate')
    add((0x0008, 0x0030), 'TM', _tx(study_time), 'StudyTime')
    add((0x0008, 0x0060), 'CS', _tx(modality), 'Modality')
    add((0x0008, 0x103E), 'LO', _tx(series_description), 'SeriesDescription')
    if references:
        items = [
            _el(REFERENCED_SOP_CLASS_UID, 'UI', _ui(cls))
            + _el(REFERENCED_SOP_INSTANCE_UID, 'UI', _ui(inst))
            for cls, inst in references
        ]
        ds += _sq(REFERENCED_SOP_SEQUENCE, items)
    add((0x0010, 0x0010), 'PN', _tx(patient_name), 'PatientName')
    add((0x0010, 0x0020), 'LO', _tx(patient_id), 'PatientID')
    add((0x0020, 0x000D), 'UI', _ui(study_uid), 'StudyInstanceUID')
    add((0x0020, 0x0052), 'UI', _ui(series_uid), 'SeriesInstanceUID')
    add((0x0020, 0x0011), 'IS', _tx(str(series_number)), 'SeriesNumber')
    add((0x0020, 0x0013), 'IS', _tx(str(instance_number)), 'InstanceNumber')

    header = b'\x00' * 128 + b'DICM' + meta + ds
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    if pixel_length is not None:
        if pixel_length <= 0 or pixel_length % 2:
            raise ValueError('pixel_length must be a positive, even number')
        pixel_head = (struct.pack('<HH', 0x7FE0, 0x0010) + b'OB'
                      + b'\x00\x00' + struct.pack('<I', pixel_length))
        with open(path, 'wb') as f:
            f.write(header)
            f.write(pixel_head)
            f.seek(pixel_length - 1, os.SEEK_CUR)  # hole: pixel bytes never written
            f.write(b'\x00')
        return path
    if pixel:
        if len(pixel) % 2:
            pixel += b'\x00'
        header += _el(PIXEL_DATA, 'OB', pixel)
    with open(path, 'wb') as f:
        f.write(header)
    return path
