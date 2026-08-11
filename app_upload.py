"""Streamlit UI nhận file XLSB và chạy nguyên logic trong pipeline.py.

Chạy bằng: streamlit run app_upload.py
"""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO

import altair as alt
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


def _join_unique_text(values: pd.Series) -> str:
    """Ghép các ghi chú khác rỗng mà không lặp lại."""
    unique_values = dict.fromkeys(
        value
        for value in values.fillna("").astype(str).str.strip()
        if value
    )
    return "; ".join(unique_values)


@st.cache_data(max_entries=10, show_spinner=False)
def summarize_lsx(rows: pd.DataFrame) -> pd.DataFrame:
    """Tổng hợp dữ liệu NVL thành đúng một dòng cho mỗi LSX."""
    working = rows.copy()
    working["_has_substitute"] = (
        working[legacy_app.COL_NVL_THAY_THE]
        .fillna("")
        .astype(str)
        .str.strip()
        .ne("")
    )
    working["_has_5402"] = (
        working[legacy_app.COL_5402]
        .fillna("")
        .astype(str)
        .str.strip()
        .ne("")
    )

    summary = (
        working.groupby(legacy_app.COL_MA_LSX, dropna=False, sort=False)
        .agg(
            **{
                legacy_app.COL_DCSX: (legacy_app.COL_DCSX, "first"),
                legacy_app.COL_MA_SP: (legacy_app.COL_MA_SP, "first"),
                legacy_app.COL_TEN_VP: (legacy_app.COL_TEN_VP, "first"),
                legacy_app.COL_NGAY_KC: (legacy_app.COL_NGAY_KC, "first"),
                legacy_app.COL_NGAY_HT: (legacy_app.COL_NGAY_HT, "first"),
                legacy_app.COL_TINH_TRANG: (
                    legacy_app.COL_TINH_TRANG,
                    "first",
                ),
                legacy_app.COL_SL_THUC_TE: (
                    legacy_app.COL_SL_THUC_TE,
                    "max",
                ),
                legacy_app.COL_DA_LSX: (legacy_app.COL_DA_LSX, "first"),
                "Số NVL": (legacy_app.COL_MA_NVL, "nunique"),
                "Có NVL thay thế": ("_has_substitute", "max"),
                "Có phiếu 5402": ("_has_5402", "max"),
                legacy_app.COL_GHI_CHU: (
                    legacy_app.COL_GHI_CHU,
                    _join_unique_text,
                ),
            }
        )
        .reset_index()
    )
    summary["Có NVL thay thế"] = summary["Có NVL thay thế"].map(
        {True: "Có", False: "Không"}
    )
    summary["Có phiếu 5402"] = summary["Có phiếu 5402"].map(
        {True: "Có", False: "Không"}
    )
    return summary


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
    dcsx_options = ["Tất cả"] + sorted(
        result[legacy_app.COL_DCSX].dropna().astype(str).unique().tolist()
    )
    selected_dcsx = st.selectbox("DCSX/GC", dcsx_options)
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
    selected_substitute = st.selectbox(
        "NVL thay thế",
        ["Tất cả", "Có", "Không"],
    )
    selected_5402 = st.selectbox(
        "Phiếu 5402",
        ["Tất cả", "Có", "Không"],
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
if selected_dcsx != "Tất cả":
    filtered = filtered[
        filtered[legacy_app.COL_DCSX].astype(str) == selected_dcsx
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
if selected_substitute != "Tất cả":
    has_substitute = (
        filtered[legacy_app.COL_NVL_THAY_THE]
        .fillna("")
        .astype(str)
        .str.strip()
        .ne("")
    )
    filtered = filtered[
        has_substitute if selected_substitute == "Có" else ~has_substitute
    ]
if selected_5402 != "Tất cả":
    has_5402 = (
        filtered[legacy_app.COL_5402]
        .fillna("")
        .astype(str)
        .str.strip()
        .ne("")
    )
    filtered = filtered[has_5402 if selected_5402 == "Có" else ~has_5402]
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

lsx_summary = summarize_lsx(filtered)
status_counts = lsx_summary[legacy_app.COL_DA_LSX].value_counts()
lsx_total = len(lsx_summary)
lsx_passed = int(status_counts.get("LSX đạt", 0))
lsx_failed = int(status_counts.get("LSX không đạt", 0))
lsx_no_issue = int(status_counts.get("LSX không lĩnh liệu", 0))
evaluated_total = lsx_passed + lsx_failed
pass_rate = lsx_passed / evaluated_total if evaluated_total else 0

with st.container(horizontal=True):
    st.metric("Tổng LSX", f"{lsx_total:,}", border=True)
    st.metric("LSX đạt", f"{lsx_passed:,}", border=True)
    st.metric("LSX không đạt", f"{lsx_failed:,}", border=True)
    st.metric("Không lĩnh liệu", f"{lsx_no_issue:,}", border=True)
    st.metric(
        "Tỷ lệ đạt",
        f"{pass_rate:.1%}",
        help="LSX đạt / (LSX đạt + LSX không đạt)",
        border=True,
    )

if filtered.empty:
    st.warning("Không có dữ liệu phù hợp với bộ lọc hiện tại.")

overview_tab, action_tab, material_tab, detail_tab = st.tabs(
    [
        "Tổng quan",
        "LSX cần xử lý",
        "Phân tích NVL",
        "Dữ liệu chi tiết",
    ]
)

status_domain = ["LSX đạt", "LSX không đạt", "LSX không lĩnh liệu"]
status_colors = ["#2E7D32", "#C62828", "#EF6C00"]

with overview_tab:
    chart_left, chart_right = st.columns(2)
    with chart_left:
        with st.container(border=True):
            st.subheader("Trạng thái LSX theo DCSX/GC")
            status_by_dcsx = (
                lsx_summary.assign(
                    **{
                        legacy_app.COL_DCSX: lsx_summary[
                            legacy_app.COL_DCSX
                        ].fillna("(Trống)")
                    }
                )
                .groupby(
                    [legacy_app.COL_DCSX, legacy_app.COL_DA_LSX],
                    dropna=False,
                )
                .size()
                .reset_index(name="Số LSX")
            )
            if status_by_dcsx.empty:
                st.info("Không có dữ liệu để hiển thị.")
            else:
                dcsx_chart = (
                    alt.Chart(status_by_dcsx)
                    .mark_bar()
                    .encode(
                        x=alt.X("Số LSX:Q", title="Số LSX"),
                        y=alt.Y(
                            f"{legacy_app.COL_DCSX}:N",
                            title="DCSX/GC",
                            sort="-x",
                        ),
                        color=alt.Color(
                            f"{legacy_app.COL_DA_LSX}:N",
                            title="Đánh giá",
                            scale=alt.Scale(
                                domain=status_domain,
                                range=status_colors,
                            ),
                        ),
                        tooltip=[
                            legacy_app.COL_DCSX,
                            legacy_app.COL_DA_LSX,
                            "Số LSX",
                        ],
                    )
                    .properties(height=340)
                )
                st.altair_chart(dcsx_chart)

    with chart_right:
        with st.container(border=True):
            st.subheader("Top mã SP có LSX không đạt")
            top_failed_products = (
                lsx_summary[
                    lsx_summary[legacy_app.COL_DA_LSX] == "LSX không đạt"
                ]
                .groupby(
                    [legacy_app.COL_MA_SP, legacy_app.COL_TEN_VP],
                    dropna=False,
                )[legacy_app.COL_MA_LSX]
                .nunique()
                .reset_index(name="Số LSX không đạt")
                .nlargest(10, "Số LSX không đạt")
            )
            if top_failed_products.empty:
                st.info("Không có LSX không đạt trong phạm vi đang lọc.")
            else:
                product_chart = (
                    alt.Chart(top_failed_products)
                    .mark_bar(color="#C62828")
                    .encode(
                        x=alt.X("Số LSX không đạt:Q"),
                        y=alt.Y(
                            f"{legacy_app.COL_MA_SP}:N",
                            sort="-x",
                            title="Mã SP",
                        ),
                        tooltip=[
                            legacy_app.COL_MA_SP,
                            legacy_app.COL_TEN_VP,
                            "Số LSX không đạt",
                        ],
                    )
                    .properties(height=340)
                )
                st.altair_chart(product_chart)

    with st.container(border=True):
        st.subheader("Tóm tắt LSX")
        st.dataframe(
            lsx_summary,
            width="stretch",
            height=420,
            hide_index=True,
            key="lsx_summary_table",
        )

with action_tab:
    action_columns = [
        legacy_app.COL_MA_LSX,
        legacy_app.COL_DCSX,
        legacy_app.COL_MA_SP,
        legacy_app.COL_TEN_VP,
        legacy_app.COL_NGAY_HT,
        legacy_app.COL_TINH_TRANG,
        legacy_app.COL_SL_THUC_TE,
        "Số NVL",
        "Có NVL thay thế",
        "Có phiếu 5402",
        legacy_app.COL_DA_LSX,
        legacy_app.COL_GHI_CHU,
    ]
    failed_lsx = lsx_summary[
        lsx_summary[legacy_app.COL_DA_LSX] == "LSX không đạt"
    ]
    no_issue_lsx = lsx_summary[
        lsx_summary[legacy_app.COL_DA_LSX] == "LSX không lĩnh liệu"
    ]

    st.subheader(f"LSX không đạt ({len(failed_lsx):,})")
    st.dataframe(
        failed_lsx[action_columns],
        width="stretch",
        height=420,
        hide_index=True,
        key="failed_lsx_table",
    )
    st.subheader(f"LSX không lĩnh liệu ({len(no_issue_lsx):,})")
    st.dataframe(
        no_issue_lsx[action_columns],
        width="stretch",
        height=320,
        hide_index=True,
        key="no_issue_lsx_table",
    )

with material_tab:
    material_left, material_right = st.columns(2)
    with material_left:
        with st.container(border=True):
            st.subheader("Trạng thái theo sử dụng NVL thay thế")
            substitute_status = (
                lsx_summary.groupby(
                    ["Có NVL thay thế", legacy_app.COL_DA_LSX],
                    dropna=False,
                )
                .size()
                .reset_index(name="Số LSX")
            )
            if substitute_status.empty:
                st.info("Không có dữ liệu để hiển thị.")
            else:
                substitute_chart = (
                    alt.Chart(substitute_status)
                    .mark_bar()
                    .encode(
                        x=alt.X("Có NVL thay thế:N", title="NVL thay thế"),
                        y=alt.Y("Số LSX:Q", title="Số LSX"),
                        color=alt.Color(
                            f"{legacy_app.COL_DA_LSX}:N",
                            title="Đánh giá",
                            scale=alt.Scale(
                                domain=status_domain,
                                range=status_colors,
                            ),
                        ),
                        tooltip=[
                            "Có NVL thay thế",
                            legacy_app.COL_DA_LSX,
                            "Số LSX",
                        ],
                    )
                    .properties(height=330)
                )
                st.altair_chart(substitute_chart)

    with material_right:
        with st.container(border=True):
            st.subheader("Top NVL trong LSX không đạt")
            failed_lsx_codes = set(
                failed_lsx[legacy_app.COL_MA_LSX].dropna().astype(str)
            )
            failed_materials = filtered[
                filtered[legacy_app.COL_MA_LSX]
                .astype(str)
                .isin(failed_lsx_codes)
            ]
            top_failed_materials = (
                failed_materials.groupby(
                    [legacy_app.COL_MA_NVL, legacy_app.COL_TEN_VP2],
                    dropna=False,
                )[legacy_app.COL_MA_LSX]
                .nunique()
                .reset_index(name="Số LSX không đạt")
                .nlargest(10, "Số LSX không đạt")
            )
            st.dataframe(
                top_failed_materials,
                width="stretch",
                height=330,
                hide_index=True,
                key="top_failed_materials_table",
            )

with detail_tab:
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
