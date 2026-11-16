"""生成测试用合成 DICOM 的公共工具。"""
from __future__ import annotations

import io
import zipfile

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import (
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
)

CT_IMAGE_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
PIXEL_BYTES = b"\x01\x02\x03\x04\x05\x06\x07\x08"  # 2x2 16bit
UNCOMPRESSED = {str(ImplicitVRLittleEndian), str(ExplicitVRLittleEndian),
                str(ExplicitVRBigEndian)}

_uid_counter = [0]


def gen_uid() -> str:
    _uid_counter[0] += 1
    return f"1.2.826.0.1.3680043.9.7433.{_uid_counter[0]}"


def make_dicom_bytes(
    *,
    patient_id="PAT-001",
    patient_name="Zhang^San",
    birth_date="19700101",
    study_uid=None,
    series_uid=None,
    sop_uid=None,
    transfer_syntax=ExplicitVRLittleEndian,
    refs=(),                # 引用的原始 SOPInstanceUID 列表
    burned_in="NO",
    number_of_frames=None,
    private_tag=True,
    private_in_sequence=True,
    pixel_bytes=PIXEL_BYTES,
) -> bytes:
    study_uid = study_uid or gen_uid()
    series_uid = series_uid or gen_uid()
    sop_uid = sop_uid or gen_uid()

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_IMAGE_STORAGE
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = transfer_syntax

    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = CT_IMAGE_STORAGE
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.PatientID = patient_id
    ds.PatientName = patient_name
    ds.PatientBirthDate = birth_date
    ds.Modality = "CT"
    ds.BurnedInAnnotation = burned_in
    if number_of_frames is not None:
        ds.NumberOfFrames = str(number_of_frames)
    ds.Rows = 2
    ds.Columns = 2
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    if str(transfer_syntax) in UNCOMPRESSED:
        ds.PixelData = pixel_bytes
    else:
        # 压缩语法要求封装像素（内容无所谓，仅用于触发传输语法校验）
        ds.PixelData = encapsulate([pixel_bytes])
    if private_tag:
        ds.add_new(0x00111001, "LO", "PRIVATE-SECRET")
        ds.add_new(0x00090010, "LO", "PRIVATE CREATOR")
    if refs:
        items = []
        for r in refs:
            item = Dataset()
            item.ReferencedSOPClassUID = CT_IMAGE_STORAGE
            item.ReferencedSOPInstanceUID = r
            if private_in_sequence:
                item.add_new(0x00111002, "LO", "SEQ-PRIVATE-SECRET")
            items.append(item)
        ds.ReferencedImageSequence = items
    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds, enforce_file_format=True)
    return buf.getvalue()


def make_zip(files: list[tuple[str, bytes]]) -> bytes:
    """把若干 (文件名, DICOM字节) 打成 ZIP。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            zf.writestr(name, data)
    return buf.getvalue()


def read_dicom(data: bytes):
    return pydicom.dcmread(io.BytesIO(data))


def unzip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {m.filename: zf.read(m) for m in zf.infolist() if not m.is_dir()}
