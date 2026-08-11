"""Streamlit UI nhận file XLSB và chạy nguyên logic trong pipeline.py.

Chạy bằng: streamlit run app_upload.py
"""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO

import pandas as pd
import streamlit as st

# Tái sử dụng toàn bộ tên cột, định dạng bảng và hàm export của app cũ.
# Import app cũng thiết lập page config trước khi UI được render.
import app as legacy_app


REQUIRED_SHEETS = (
    "MOCR27",
    "BOM ĐẾN 24.07",
    "BOMR20 - NVL TT",
    "INVR17",
)


@st.cache_data(max_entries=3, show_spinner=False)
def process_uploaded_workbook(
    file_bytes: bytes,
    filename: str,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Đọc workbook upload và gọi nguyên pipeline hiện tại."""
    try:
        with pd.ExcelFile(BytesIO(file_bytes), engine="pyxlsb") as workbook:
            missing = [
                sheet
                for sheet in REQUIRED_SHEETS
                if sheet not in workbook.sheet_names
            ]
            if missing:
                raise ValueError(
                    "File thiếu sheet bắt buộc: " + ", ".join(missing)
                )

            mocr = pd.read_excel(workbook, sheet_name="MOCR27")
            artifact_columns = [
                column
                for column in mocr.columns
                if str(column).startswith("Unnamed")
            ]
            mocr = mocr.drop(columns=artifact_columns, errors="ignore")
            bom = pd.read_excel(workbook, sheet_name="BOM ĐẾN 24.07")
            bomr20 = pd.read_excel(workbook, sheet_name="BOMR20 - NVL TT")
            invr17 = pd.read_excel(workbook, sheet_name="INVR17")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(
            f"Không thể đọc file {filename}. Hãy kiểm tra định dạng XLSB."
        ) from exc

    data = {
        "mocr": mocr,
        "bom": bom,
        "bomr20": bomr20,
        "invr17": invr17,
        "metadata": {
            "n_mocr": len(mocr),
            "n_bom": len(bom),
            "n_bomr20": len(bomr20),
            "n_invr17": len(invr17),
        },
    }
    result = legacy_app.run_pipeline(data)
    return result, data["metadata"]


@st.cache_data(max_entries=3, show_spinner=False)
def export_excel(result: pd.DataFrame) -> bytes:
    """Tạo file Excel và cache theo nội dung kết quả."""
    return legacy_app.export_to_excel(result).getvalue()


st.session_state.setdefault("upload_result", None)
st.session_state.setdefault("upload_metadata", None)
st.session_state.setdefault("processed_fingerprint", None)
st.session_state.setdefault("processed_filename", None)

st.title("Đánh giá LSX từ file tải lên")
st.caption(
    "Upload một file XLSB có đủ MOCR27, BOM gốc, BOMR20 và INVR17. "
    "Ứng dụng gọi trực tiếp logic trong pipeline.py."
)

with st.container(border=True):
    uploaded_file = st.file_uploader(
        "File dữ liệu XLSB",
        type="xlsb",
        max_upload_size=95,
        key="lsx_workbook_upload",
        help="Giới hạn 95 MB để tương thích Cloudflare Free.",
    )
    process_clicked = st.button(
        "Xử lý dữ liệu",
        type="primary",
        icon=":material/play_arrow:",
        disabled=uploaded_file is None,
        width="stretch",
    )

if uploaded_file is None:
    st.info("Chọn file XLSB để bắt đầu xử lý.")
    st.stop()

uploaded_bytes = uploaded_file.getvalue()
fingerprint = sha256(uploaded_bytes).hexdigest()

if process_clicked:
    with st.spinner("Đang đọc file và chạy pipeline…"):
        try:
            result, metadata = process_uploaded_workbook(
                uploaded_bytes,
                uploaded_file.name,
            )
        except Exception as exc:
            st.session_state.upload_result = None
            st.session_state.upload_metadata = None
            st.session_state.processed_fingerprint = None
            st.error(str(exc))
            st.stop()

    st.session_state.upload_result = result
    st.session_state.upload_metadata = metadata
    st.session_state.processed_fingerprint = fingerprint
    st.session_state.processed_filename = uploaded_file.name
    st.success("Đã xử lý xong file XLSB.")

if st.session_state.processed_fingerprint != fingerprint:
    st.warning(
        "File đang chọn chưa được xử lý. Bấm **Xử lý dữ liệu** để chạy pipeline."
    )
    st.stop()

result = st.session_state.upload_result
metadata = st.session_state.upload_metadata
if result is None or metadata is None:
    st.stop()

with st.container(horizontal=True):
    st.metric("Dòng kết quả", f"{len(result):,}")
    st.metric(
        "LSX duy nhất",
        f"{result[legacy_app.COL_MA_LSX].nunique():,}",
    )
    st.metric(
        "NVL duy nhất",
        f"{result[legacy_app.COL_MA_NVL].nunique():,}",
    )
    st.metric(
        "NVL thay thế",
        f"{(result[legacy_app.COL_NVL_THAY_THE].fillna('').str.strip() != '').sum():,}",
    )

st.caption(
    f"Nguồn: {st.session_state.processed_filename} · "
    f"MOCR27 {metadata['n_mocr']:,} dòng · "
    f"BOM {metadata['n_bom']:,} · "
    f"BOMR20 {metadata['n_bomr20']:,} · "
    f"INVR17 {metadata['n_invr17']:,}"
)

with st.sidebar:
    st.header("Bộ lọc")
    evaluation_options = ["Tất cả"] + sorted(
        result[legacy_app.COL_DA_LSX].dropna().astype(str).unique().tolist()
    )
    selected_evaluation = st.selectbox(
        "Đánh giá trên LSX",
        evaluation_options,
    )
    status_options = ["Tất cả"] + sorted(
        result[legacy_app.COL_TINH_TRANG].dropna().astype(str).unique().tolist()
    )
    selected_status = st.selectbox("Tình trạng lệnh SX", status_options)
    xh_options = ["Tất cả"] + sorted(
        result[legacy_app.COL_XH].dropna().astype(str).unique().tolist(),
        key=lambda value: int(value) if value.isdigit() else 999,
    )
    selected_xh = st.selectbox("Số lần xuất hiện (XH)", xh_options)
    classification_options = ["Tất cả"] + sorted(
        result[legacy_app.COL_PHAN_LOAI]
        .dropna()
        .astype(str)
        .unique()
        .tolist()
    )
    selected_classification = st.selectbox(
        "Phân loại NVL",
        classification_options,
    )
    search = st.text_input(
        "Tìm kiếm",
        placeholder="Mã LSX, SP, NVL hoặc tên vật tư…",
    )

filtered = result.copy()
if selected_evaluation != "Tất cả":
    filtered = filtered[
        filtered[legacy_app.COL_DA_LSX].astype(str) == selected_evaluation
    ]
if selected_status != "Tất cả":
    filtered = filtered[
        filtered[legacy_app.COL_TINH_TRANG].astype(str) == selected_status
    ]
if selected_xh != "Tất cả":
    filtered = filtered[
        filtered[legacy_app.COL_XH].astype(str) == selected_xh
    ]
if selected_classification != "Tất cả":
    filtered = filtered[
        filtered[legacy_app.COL_PHAN_LOAI].astype(str)
        == selected_classification
    ]
if search.strip():
    needle = search.strip()
    search_columns = [
        legacy_app.COL_MA_LSX,
        legacy_app.COL_MA_SP,
        legacy_app.COL_TEN_VP,
        legacy_app.COL_MA_NVL,
        legacy_app.COL_TEN_VP2,
        legacy_app.COL_NVL_THAY_THE,
    ]
    search_mask = filtered[search_columns].astype(str).apply(
        lambda column: column.str.contains(
            needle,
            case=False,
            na=False,
            regex=False,
        )
    ).any(axis=1)
    filtered = filtered.loc[search_mask]

st.subheader(f"Kết quả chi tiết ({len(filtered):,}/{len(result):,} dòng)")
st.dataframe(
    legacy_app.style_dataframe(filtered),
    width="stretch",
    height=700,
    hide_index=True,
    key="uploaded_lsx_result",
)

with st.container(horizontal=True):
    st.download_button(
        "Tải Excel toàn bộ",
        data=export_excel(result),
        file_name="LSX_Evaluation_Upload.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        icon=":material/download:",
    )
    st.download_button(
        "Tải CSV đang lọc",
        data=filtered.to_csv(index=False).encode("utf-8-sig"),
        file_name="LSX_Evaluation_Filtered.csv",
        mime="text/csv",
        icon=":material/download:",
    )
