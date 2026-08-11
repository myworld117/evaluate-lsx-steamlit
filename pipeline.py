"""Pipeline xử lý dữ liệu LSX — module độc lập, không streamlit cache."""

import re
import unicodedata

import pandas as pd
import numpy as np


def _normalize_header(value: object) -> str:
    """Chuẩn hóa header để không phụ thuộc hoa/thường, dấu và khoảng trắng."""
    text = str(value).strip().replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _select_input_columns(
    frame: pd.DataFrame,
    sheet_name: str,
    schema: dict[str, tuple[str, ...]],
    required: set[str],
    defaults: dict[str, object] | None = None,
) -> pd.DataFrame:
    """Chỉ lấy field pipeline dùng và bỏ qua toàn bộ cột thừa."""
    defaults = defaults or {}
    normalized_columns: dict[str, object] = {}
    for column in frame.columns:
        normalized_columns.setdefault(_normalize_header(column), column)

    selected: dict[str, pd.Series] = {}
    missing: list[str] = []
    for canonical, aliases in schema.items():
        source_column = next(
            (
                normalized_columns[_normalize_header(alias)]
                for alias in aliases
                if _normalize_header(alias) in normalized_columns
            ),
            None,
        )
        if source_column is not None:
            selected[canonical] = frame[source_column]
        elif canonical in required:
            missing.append(aliases[0] if aliases else canonical)
        else:
            selected[canonical] = pd.Series(
                defaults.get(canonical, pd.NA),
                index=frame.index,
            )

    if missing:
        raise ValueError(
            f"Sheet {sheet_name} thiếu cột bắt buộc: {', '.join(missing)}"
        )

    return pd.DataFrame(selected, index=frame.index).copy()


MOCR_SCHEMA = {
    "LINK": ("LINK",),
    "DCSX_GC": (
        "Dây chuyền SX/nhà máy gia công",
        "DCSX/GC",
    ),
    "Ten_DCSX_GC": ("Tên dây chuyền SX/nhà máy gia công",),
    "Ma_so_lenh_tao": ("Mã số lệnh tạo", "Mã LSX", "LSX"),
    "Ngay_khoi_cong": ("Ngày khởi công thực tế",),
    "Ngay_hoan_tat": ("Ngày hoàn tất thực tế",),
    "Ma_SP": ("Mã SP",),
    "Ten_VP": ("Tên VP",),
    "DV": ("ĐV",),
    "Tinh_trang_lenh_SX": ("Tình trạng lệnh SX",),
    "SL_du_tinh": ("SL dự tính",),
    "San_luong_thuc_te": ("Sản lượng thực tế", "SL thực tế"),
    "Kho_SX": ("Kho SX",),
    "Ten_kho_SX": ("Tên kho sản xuất",),
    "Ma_NVL": ("Mã NVL",),
    "Ten_VP2": ("Tên VP2", "Tên VP.1"),
    "DV3": ("ĐV3", "ĐV.1"),
    "Luong_dung_tieu_chuan_goc": ("Lượng dùng tiêu chuẩn",),
    "SL_dung_thuc": ("SL dùng thực",),
}

MOCR_REQUIRED = {
    "DCSX_GC",
    "Ma_so_lenh_tao",
    "Ma_SP",
    "Tinh_trang_lenh_SX",
    "San_luong_thuc_te",
    "Ma_NVL",
    "Luong_dung_tieu_chuan_goc",
    "SL_dung_thuc",
}

BOM_SCHEMA = {
    "Ma_SP_BOM": ("Thành phẩm/Bán TP",),
    "Ma_NVL_BOM": ("Mã NVL",),
    "Luong_dung_to_hop": ("Lượng dùng tổ hợp",),
    "Mau_so": ("Mẫu số",),
}

BOMR20_SCHEMA = {
    "NVL_chinh": ("chính",),
    "NVL_thay_the": ("thay thế",),
    "Pham_vi_SP": ("Mã NVL chính",),
    "SL": ("thay thế/chính",),
}

INVR17_SCHEMA = {
    "Ma_NVL_INVR": ("Mã SP", "Mã NVL"),
    "Ma_CT_INVR": ("Mã CT",),
    "Ghi_chu_INVR": ("Ghi chú",),
    "LSX_INVR": ("LSX",),
    "SL_bien_dong_INVR": ("SL biến động",),
}


def _build_5402_quantity_map(
    invr17: pd.DataFrame,
) -> dict[tuple[str, str], float]:
    """Cộng SL biến động phiếu 5402 theo đúng cặp (LSX, Mã NVL)."""
    ma_ct = invr17["Ma_CT_INVR"].fillna("").astype(str).str.strip()
    quantity = pd.to_numeric(
        invr17["SL_bien_dong_INVR"], errors="coerce"
    ).fillna(0)
    quantity_map: dict[tuple[str, str], float] = {}

    for index in invr17.index[ma_ct.str.startswith("5402")]:
        material_value = invr17.at[index, "Ma_NVL_INVR"]
        if pd.isna(material_value):
            continue
        material = str(material_value).strip()
        if not material:
            continue

        lsx_values: set[str] = set()
        direct_lsx_value = invr17.at[index, "LSX_INVR"]
        if not pd.isna(direct_lsx_value):
            direct_lsx = str(direct_lsx_value).strip()
            if direct_lsx:
                lsx_values.add(direct_lsx)

        note_value = invr17.at[index, "Ghi_chu_INVR"]
        if not pd.isna(note_value) and str(note_value).strip():
            note = str(note_value).strip()
            lsx_values.update(
                token.strip()
                for token in note.replace(",", " ")
                .replace(";", " ")
                .split()
                if token.strip()
            )

        for lsx in lsx_values:
            key = (lsx, material)
            quantity_map[key] = quantity_map.get(key, 0.0) + float(
                quantity.at[index]
            )

    return quantity_map


def _apply_5402_sp_corrections(
    df: pd.DataFrame,
    invr17: pd.DataFrame,
    quantity_map: dict[tuple[str, str], float] | None = None,
    tolerance: float = 0.01,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Chuyển SP không đạt thành đạt khi phiếu 5402 khớp LSX, NVL và SL."""
    if quantity_map is None:
        quantity_map = _build_5402_quantity_map(invr17)
    quantities = pd.Series(
        [
            quantity_map.get(
                (str(lsx).strip(), str(material).strip()),
                np.nan,
            )
            for lsx, material in zip(df["Ma_so_lenh_tao"], df["Ma_NVL"])
        ],
        index=df.index,
        dtype="float64",
    )
    differences = pd.to_numeric(df["Chenh_lech"], errors="coerce")
    matched = (
        df["DA_SP"].eq("LSX không đạt")
        & quantities.notna()
        & differences.notna()
        & ((quantities.abs() - differences.abs()).abs() < tolerance)
    )
    corrected = df["DA_SP"].copy()
    corrected.loc[matched] = "LSX đạt"
    return corrected, matched, quantities


def process(data: dict) -> pd.DataFrame:
    """Chạy toàn bộ pipeline. Input: dict với keys: mocr, bom, bomr20, invr17."""
    df = _select_input_columns(
        data["mocr"],
        "MOCR27",
        MOCR_SCHEMA,
        MOCR_REQUIRED,
        defaults={
            "LINK": pd.NA,
            "Ten_DCSX_GC": "",
            "Ngay_khoi_cong": pd.NaT,
            "Ngay_hoan_tat": pd.NaT,
            "Ten_VP": "",
            "DV": "",
            "SL_du_tinh": pd.NA,
            "Kho_SX": "",
            "Ten_kho_SX": "",
            "Ten_VP2": "",
            "DV3": "",
        },
    )
    bom = _select_input_columns(
        data["bom"],
        "BOM ĐẾN 24.07",
        BOM_SCHEMA,
        set(BOM_SCHEMA),
    )
    bomr20 = _select_input_columns(
        data["bomr20"],
        "BOMR20 - NVL TT",
        BOMR20_SCHEMA,
        set(BOMR20_SCHEMA),
    )
    invr17 = _select_input_columns(
        data["invr17"],
        "INVR17",
        INVR17_SCHEMA,
        {
            "Ma_NVL_INVR",
            "Ma_CT_INVR",
            "LSX_INVR",
            "SL_bien_dong_INVR",
        },
        defaults={"Ghi_chu_INVR": ""},
    )

    # LINK trong file cũ là kết quả tra Mã LSX của MOCR27 trong cột LSX của
    # INVR17: tìm thấy thì LINK = Mã LSX, không tìm thấy thì #N/A. Khi file
    # nguồn mới không còn cột phụ này, dựng lại đúng điều kiện tra cứu để
    # người dùng không phải tự thêm công thức hoặc xóa cột trước khi upload.
    if df["LINK"].isna().all():
        invr_lsx = set(
            invr17["LSX_INVR"].fillna("").astype(str).str.strip()
        )
        mocr_lsx = df["Ma_so_lenh_tao"].fillna("").astype(str).str.strip()
        df["LINK"] = mocr_lsx.where(mocr_lsx.isin(invr_lsx), pd.NA)

    # Chỉ xét các dòng có LINK hợp lệ. Với file XLSB, lỗi Excel #N/A có thể
    # được pyxlsb đọc thành mã lỗi hexadecimal ``0x2a`` (mã lỗi #N/A), nên
    # cần loại cả hai biểu diễn trước khi tính XH và các công thức.
    link_text = df["LINK"].astype(str).str.strip().str.upper()
    link_is_na = df["LINK"].isna() | link_text.isin({"#N/A", "0X2A"})
    df = df.loc[~link_is_na].copy()

    # Chuẩn hóa trước khi tính XH để loại toàn bộ LSX không có phát sinh:
    # SL thực tế của LSX bằng 0 và SL dùng thực của tất cả NVL cũng bằng 0.
    # Dùng điều kiện từng dòng đều bằng 0 thay vì tổng, tránh bỏ sót LSX có
    # các giá trị bù trừ nhau.
    df["San_luong_thuc_te"] = pd.to_numeric(
        df["San_luong_thuc_te"], errors="coerce"
    ).fillna(0)
    df["SL_dung_thuc"] = pd.to_numeric(
        df["SL_dung_thuc"], errors="coerce"
    ).fillna(0)
    lsx_group = df.groupby("Ma_so_lenh_tao", dropna=False)
    lsx_sl_thuc_te_zero = lsx_group["San_luong_thuc_te"].transform(
        lambda values: values.eq(0).all()
    )
    lsx_sl_dung_thuc_zero = lsx_group["SL_dung_thuc"].transform(
        lambda values: values.eq(0).all()
    )
    df = df.loc[~(lsx_sl_thuc_te_zero & lsx_sl_dung_thuc_zero)].copy()

    # ── Bước 1: Xác định XH = số dòng NVL trong cùng LSX ────
    xh_count = df.groupby("Ma_so_lenh_tao")["Ma_so_lenh_tao"].transform("count")
    df["XH"] = xh_count.astype(int)

    # ── Chuẩn bị BOM lookup ─────────────────────────────────
    bom_lookup = bom.copy()
    bom_lookup = bom_lookup.dropna(subset=["Ma_NVL_BOM"])
    for c in ["Luong_dung_to_hop", "Mau_so"]:
        bom_lookup[c] = pd.to_numeric(bom_lookup[c], errors="coerce").fillna(0)
    bom_lookup["Mau_so"] = bom_lookup["Mau_so"].replace(0, 1)
    # Gom theo (Ma_SP, Ma_NVL) — mỗi cặp SP+NVL có 1 định mức duy nhất
    bom_lookup = bom_lookup.groupby(["Ma_SP_BOM", "Ma_NVL_BOM"]).first().reset_index()

    # ── Chuẩn bị BOMR20 lookup ──────────────────────────────
    # Lấy tỷ lệ quy đổi từ cột "thay thế/chính". Cột "TT thay thế" chỉ là
    # thứ tự của NVL thay thế, không phải hệ số quy đổi.
    pair_sl_lookup = bomr20.copy()
    pair_sl_lookup = pair_sl_lookup.dropna(subset=["NVL_chinh", "NVL_thay_the"])
    pair_sl_lookup["SL"] = pd.to_numeric(
        pair_sl_lookup["SL"], errors="coerce"
    ).fillna(1.0)
    # BOMR20 chỉ xác nhận hai mã có thể thay thế cho nhau. Chiều "chính" và
    # "thay thế" thực tế phải lấy theo BOM gốc của đúng Mã SP. Phạm vi toàn
    # dấu * (và dòng cũ để trống) dùng chung; mã SP cụ thể chỉ áp dụng cho SP
    # đó và được ưu tiên hơn tỷ lệ dùng chung.
    generic_partners = {}
    scoped_partners = {}
    generic_ratio_map = {}
    scoped_ratio_map = {}
    for row in pair_sl_lookup.itertuples(index=False):
        left = str(row.NVL_chinh).strip()
        right = str(row.NVL_thay_the).strip()
        scope = "" if pd.isna(row.Pham_vi_SP) else str(row.Pham_vi_SP).strip()
        ratio = float(row.SL)
        if not left or not right:
            continue
        if not np.isfinite(ratio) or ratio == 0:
            ratio = 1.0
        is_generic = not scope or set(scope) == {"*"}
        if is_generic:
            generic_partners.setdefault(left, []).append(right)
            generic_partners.setdefault(right, []).append(left)
            generic_ratio_map[(left, right)] = ratio
            generic_ratio_map[(right, left)] = 1.0 / ratio
        else:
            scoped_partners.setdefault((scope, left), []).append(right)
            scoped_partners.setdefault((scope, right), []).append(left)
            scoped_ratio_map[(scope, left, right)] = ratio
            scoped_ratio_map[(scope, right, left)] = 1.0 / ratio
    generic_partners = {
        nvl: tuple(dict.fromkeys(partners))
        for nvl, partners in generic_partners.items()
    }
    scoped_partners = {
        key: tuple(dict.fromkeys(partners))
        for key, partners in scoped_partners.items()
    }

    def get_replacement_partners(ma_sp, nvl):
        """Lấy các mã thay thế đúng phạm vi SP, rồi bổ sung quan hệ dùng chung."""
        ma_sp = str(ma_sp).strip()
        nvl = str(nvl).strip()
        partners = (
            *scoped_partners.get((ma_sp, nvl), ()),
            *generic_partners.get(nvl, ()),
        )
        return tuple(dict.fromkeys(partners))

    def get_replacement_ratio(ma_sp, main_nvl, sub_nvl):
        """Ưu tiên tỷ lệ chỉ định cho SP; nếu không có thì dùng tỷ lệ chung."""
        key = (
            str(ma_sp).strip(),
            str(main_nvl).strip(),
            str(sub_nvl).strip(),
        )
        return scoped_ratio_map.get(
            key, generic_ratio_map.get(key[1:], 1.0)
        )

    # ── Bước 2: Tính Lượng dùng tiêu chuẩn ──────────────────
    # Dùng dict lookup theo (SP, NVL) thay vì merge (tránh lỗi Arrow string)
    bom_dict = {}
    for _, br in bom_lookup.iterrows():
        key = (str(br['Ma_SP_BOM']).strip(), str(br['Ma_NVL_BOM']).strip())
        bom_dict[key] = (float(br['Luong_dung_to_hop']), float(br['Mau_so']))

    # NVL-only fallback dict
    bom_nvl_only = bom_lookup.groupby("Ma_NVL_BOM").first().reset_index()
    bom_nvl_dict = {}
    for _, br in bom_nvl_only.iterrows():
        k = str(br['Ma_NVL_BOM']).strip()
        if k not in bom_nvl_dict:
            bom_nvl_dict[k] = (float(br['Luong_dung_to_hop']), float(br['Mau_so']))

    # Bước 1: SP+NVL lookup (chỉ SP+NVL, không NVL-only)
    def lookup_sp_nvl(sp, nvl):
        k = (str(sp).strip(), str(nvl).strip())
        return bom_dict.get(k, (np.nan, np.nan))

    tmp = df.apply(lambda r: lookup_sp_nvl(r['Ma_SP'], r['Ma_NVL']), axis=1, result_type='expand')
    df['Luong_dung_to_hop'] = tmp[0]
    df['Mau_so'] = tmp[1]
    df["_match_type"] = "not_found"
    df.loc[df["Luong_dung_to_hop"].notna(), "_match_type"] = "in_bom"

    # ── Bước 5 (làm trước): NVL thay thế ────────────────────
    ma_nvl_str = df["Ma_NVL"].fillna("").astype(str)

    # Chỉ công nhận thay thế khi NVL chính có trong BOM của đúng Mã SP.
    # Không dùng BOM của SP khác để gắn nhãn thay thế cho dòng hiện tại.
    sp_bom_nvls = {}
    for (ma_sp_bom, ma_nvl_bom) in bom_dict:
        sp_bom_nvls.setdefault(ma_sp_bom, set()).add(ma_nvl_bom)

    def find_main_for_substitute(ma_sp, nvl):
        """Lấy NVL chính từ BOM gốc của đúng SP, không theo chiều BOMR20."""
        sp_nvls = sp_bom_nvls.get(str(ma_sp).strip(), set())
        nvl = str(nvl).strip()
        if nvl in sp_nvls:
            return None
        for main_nvl in get_replacement_partners(ma_sp, nvl):
            if main_nvl in sp_nvls:
                return main_nvl
        return None

    # Tập NVL có trong từng LSX để nhận biết trường hợp dùng đồng thời hai mã.
    lsx_nvl_set = df.groupby("Ma_so_lenh_tao")["Ma_NVL"].apply(lambda x: set(x.fillna("").astype(str).str.strip()))

    def check_substitute(row):
        nvl = str(row["Ma_NVL"]).strip()
        lsx = str(row["Ma_so_lenh_tao"]).strip()
        ma_sp = str(row["Ma_SP"]).strip()
        lsx_nvls = lsx_nvl_set.get(lsx, set())
        # Tìm partner trong cùng LSX: ưu tiên cặp mà cả 2 NVL đều có mặt.
        # Nếu dòng hiện tại là NVL thay thế và NVL chính có trong BOM của
        # đúng SP thì vẫn đánh dấu _is_substitute, kể cả khi cả hai cùng có
        # mặt trong LSX; phần tính TC đặc biệt bên dưới cần biết cả cặp.
        main_nvl = find_main_for_substitute(ma_sp, nvl)
        if main_nvl is not None:
            return True, main_nvl in lsx_nvls, main_nvl, nvl
        # Nếu dòng hiện tại nằm trong BOM gốc, tìm mã ngoài BOM có quan hệ
        # thay thế và đang xuất hiện trong cùng LSX.
        sp_nvls = sp_bom_nvls.get(ma_sp, set())
        if nvl in sp_nvls:
            for sub_nvl in get_replacement_partners(ma_sp, nvl):
                if sub_nvl in lsx_nvls and sub_nvl not in sp_nvls:
                    return False, True, nvl, sub_nvl
        return False, False, None, None

    tmp_sub = df.apply(check_substitute, axis=1, result_type='expand')
    df["_is_substitute"] = tmp_sub[0].astype(bool)     # thực sự là NVL thay thế
    df["_is_both_in_lsx"] = tmp_sub[1].astype(bool)    # cả 2 cùng có trong LSX
    df["_pair_main"] = tmp_sub[2]                       # NVL chính
    df["_pair_sub"] = tmp_sub[3]                        # NVL thay thế
    df["_is_in_pair"] = df["_is_substitute"] | df["_is_both_in_lsx"]
    # _main_nvl = NVL chính theo BOM của đúng Mã SP.
    df["_main_nvl"] = None
    mask_sub2 = df["_is_substitute"]
    if mask_sub2.any():
        df.loc[mask_sub2, "_main_nvl"] = [
            find_main_for_substitute(sp, nvl)
            for sp, nvl in zip(
                df.loc[mask_sub2, "Ma_SP"], df.loc[mask_sub2, "Ma_NVL"]
            )
        ]

    # Với NVL thay thế KHÔNG có trong BOM → dùng BOM của NVL chính
    # của đúng Mã SP (không lấy định mức từ SP khác).
    sub_not_in_bom = df["_is_substitute"] & (df["_match_type"] == "not_found")
    if sub_not_in_bom.any():
        for idx in df[sub_not_in_bom].index:
            nvl = str(df.loc[idx, 'Ma_NVL']).strip()
            main_nvl = find_main_for_substitute(df.loc[idx, "Ma_SP"], nvl)
            ma_sp = str(df.loc[idx, "Ma_SP"]).strip()
            if main_nvl and (ma_sp, main_nvl) in bom_dict:
                ldth, ms = bom_dict[(ma_sp, main_nvl)]
                df.loc[idx, 'Luong_dung_to_hop'] = ldth
                df.loc[idx, 'Mau_so'] = ms
                df.loc[idx, '_match_type'] = "substitute"

    # NVL-only fallback: cho dòng chưa tìm thấy SAU KHI đã check substitute
    mask_need_fallback = df["Luong_dung_to_hop"].isna() & (df["_match_type"] == "not_found")
    if mask_need_fallback.any():
        def lookup_nvl(nvl):
            return bom_nvl_dict.get(str(nvl).strip(), (np.nan, np.nan))
        tmp2 = df.loc[mask_need_fallback].apply(
            lambda r: pd.Series(lookup_nvl(r['Ma_NVL'])), axis=1)
        df.loc[mask_need_fallback, 'Luong_dung_to_hop'] = tmp2[0].values
        df.loc[mask_need_fallback, 'Mau_so'] = tmp2[1].values
        df.loc[mask_need_fallback & df['Luong_dung_to_hop'].notna(), '_match_type'] = "in_bom"

    # SL = 1.0 mặc định, chỉ lấy từ BOMR20 cho NVL thay thế KHÔNG có trong BOM
    df["_SL"] = 1.0
    mask_sub_sl = df["_match_type"] == "substitute"
    if mask_sub_sl.any():
        df.loc[mask_sub_sl, "_SL"] = [
            get_replacement_ratio(ma_sp, main, sub)
            for ma_sp, main, sub in zip(
                df.loc[mask_sub_sl, "Ma_SP"],
                df.loc[mask_sub_sl, "_main_nvl"],
                df.loc[mask_sub_sl, "Ma_NVL"],
            )
        ]

    # Tính Lượng dùng tiêu chuẩn
    df["Luong_dung_to_hop"] = pd.to_numeric(df["Luong_dung_to_hop"], errors="coerce").fillna(0)
    df["Mau_so"] = pd.to_numeric(df["Mau_so"], errors="coerce").fillna(1)
    df["Mau_so"] = df["Mau_so"].replace(0, 1)
    df["San_luong_thuc_te"] = pd.to_numeric(df["San_luong_thuc_te"], errors="coerce").fillna(0)

    mask_has_bom = df["_match_type"].isin(["in_bom", "substitute"])
    df["_bom_per_unit"] = df["Luong_dung_to_hop"] / df["Mau_so"]
    df["Luong_dung_tieu_chuan"] = df["Luong_dung_tieu_chuan_goc"].copy()
    # Nếu Mã SP = Mã NVL → dùng thẳng SL thực tế (theo Excel)
    sp_eq_nvl = df["Ma_SP"].fillna("").astype(str).str.strip() == df["Ma_NVL"].fillna("").astype(str).str.strip()
    mask_bom_not_spnvl = mask_has_bom & ~sp_eq_nvl
    df.loc[mask_bom_not_spnvl, "Luong_dung_tieu_chuan"] = (
        df.loc[mask_bom_not_spnvl, "_bom_per_unit"] * df.loc[mask_bom_not_spnvl, "_SL"] * df.loc[mask_bom_not_spnvl, "San_luong_thuc_te"]
    )
    # SP = NVL: dùng SL thực tế
    df.loc[sp_eq_nvl, "Luong_dung_tieu_chuan"] = df.loc[sp_eq_nvl, "San_luong_thuc_te"]

    # ── Bước 3: Chuẩn hóa định mức theo cặp NVL chính–thay thế
    df["SL_dung_thuc"] = pd.to_numeric(df["SL_dung_thuc"], errors="coerce").fillna(0)
    pair_rows = df[df["_pair_main"].notna() & df["_pair_sub"].notna()]
    pair_groups = pair_rows.groupby(
        ["Ma_so_lenh_tao", "Ma_SP", "_pair_main", "_pair_sub"],
        dropna=False,
    )
    for (lsx, ma_sp, main_nvl, sub_nvl), group in pair_groups:
        main_nvl = str(main_nvl).strip()
        sub_nvl = str(sub_nvl).strip()
        ma_sp = str(ma_sp).strip()
        ratio = get_replacement_ratio(ma_sp, main_nvl, sub_nvl)
        if ratio == 0:
            ratio = 1.0

        # B = định mức gốc của toàn bộ sản lượng LSX.
        if (ma_sp, main_nvl) in bom_dict:
            ldth, mau_so = bom_dict[(ma_sp, main_nvl)]
            production_qty = pd.to_numeric(
                group["San_luong_thuc_te"], errors="coerce"
            ).fillna(0).iloc[0]
            original_standard = (ldth / mau_so if mau_so else 0) * production_qty
        else:
            original_standard = group.loc[
                group["Ma_NVL"].astype(str).str.strip() == main_nvl,
                "Luong_dung_tieu_chuan",
            ].sum()

        main_mask = group["Ma_NVL"].astype(str).str.strip() == main_nvl
        sub_mask = group["Ma_NVL"].astype(str).str.strip() == sub_nvl
        main_used = group.loc[main_mask, "SL_dung_thuc"].sum()
        sub_used = group.loc[sub_mask, "SL_dung_thuc"].sum()
        sub_used_as_main = sub_used / ratio
        total_used_as_main = main_used + sub_used_as_main

        if total_used_as_main > 0:
            # Phân bổ B theo lượng dùng thực tế đã quy đổi về NVL gốc.
            row_standard = (
                original_standard
                * group["SL_dung_thuc"]
                / total_used_as_main
            )
            df.loc[group.index, "Luong_dung_tieu_chuan"] = row_standard
        elif main_mask.any():
            # Không có lượng dùng thực tế ở cả hai dòng: giữ B ở dòng chính,
            # không nhân đôi định mức sang dòng thay thế.
            df.loc[group.index, "Luong_dung_tieu_chuan"] = 0
            main_indices = group.index[main_mask]
            df.loc[main_indices[0], "Luong_dung_tieu_chuan"] = original_standard
        else:
            # Chỉ còn dòng thay thế nhưng chưa có SL dùng thực: quy đổi B
            # sang đơn vị của NVL thay thế.
            df.loc[group.index, "Luong_dung_tieu_chuan"] = original_standard * ratio

    # ── Tính Chênh lệch ─────────────────────────────────────
    df["Chenh_lech"] = df["Luong_dung_tieu_chuan"] - df["SL_dung_thuc"]

    # ── Bước 4: Phân loại NVL ───────────────────────────────
    def phan_loai_nvl(ma):
        ma = str(ma).strip() if pd.notna(ma) else ""
        if not ma: return "Khác"
        first = ma[0]
        if first == "4":
            p3 = ma[:3].upper()
            m = {"4TT": "Thép tấm (4TT)", "4TC": "Thép cuộn (4TC)",
                 "4TO": "Thép ống (4TO)", "4LK": "Linh kiện (4LK)",
                 "4TG": "Thép gia công (4TG)", "4KG": "Block khuôn gá (4KG)"}
            return m.get(p3, f"Thép khác ({p3})")
        if first == "5": return "Vật tư (5)"
        if first == "3": return "BTP (3)"
        if first == "1": return "TP (1)"
        if first == "6": return "CCDC"
        if first == "8": return "NVL KH gửi (8)"
        return f"Khác ({first})"

    df["Phan_loai"] = df["Ma_NVL"].apply(phan_loai_nvl)

    # ── NVL thay thế flag ───────────────────────────────────
    # Chỉ hiển thị ở dòng NVL thay thế khi:
    #   - mã NVL hiện tại không có trong BOM chính của đúng Mã SP; và
    #   - mã NVL chính có trong BOM chính của đúng Mã SP.
    # Vì vậy dòng NVL chính/đã có sẵn trong BOM không bị gắn cặp thay thế.
    df["NVL_thay_the_flag"] = ""
    row_sp = df["Ma_SP"].fillna("").astype(str).str.strip()
    row_nvl = df["Ma_NVL"].fillna("").astype(str).str.strip()
    main_for_row = pd.Series(
        [find_main_for_substitute(sp, nvl) for sp, nvl in zip(row_sp, row_nvl)],
        index=df.index,
    )
    current_in_bom = [
        nvl in sp_bom_nvls.get(sp, set())
        for sp, nvl in zip(row_sp, row_nvl)
    ]
    mask_sub = (
        main_for_row.notna()
        & ~pd.Series(current_in_bom, index=df.index)
    )
    if mask_sub.any():
        main_str = main_for_row.loc[mask_sub].fillna("").astype(str)
        sub_str = row_nvl.loc[mask_sub]
        df.loc[mask_sub, "NVL_thay_the_flag"] = main_str + "-" + sub_str

    # ── Bước 6: Tính 8 Công thức ────────────────────────────
    mask_xh1 = df["XH"] == 1
    # Gộp trạng thái: "Đang SX" riêng, còn lại → "*hoàn thành" (như Excel)
    df["_tt_gop"] = np.where(df["Tinh_trang_lenh_SX"].fillna("").astype(str).str.strip() == "Đang SX",
                             "Đang SX", "*hoàn thành")

    g1 = df[mask_xh1].groupby(["_tt_gop", "Ma_NVL"])
    s1 = g1["Chenh_lech"].transform("sum")
    std1 = g1["Luong_dung_tieu_chuan"].transform("sum")
    df.loc[mask_xh1, "CT2_TongChenhLech"] = s1                # col 17: CT2 = Tổng chênh lệch
    df.loc[mask_xh1, "CT1_PctChenhLech"] = np.where(std1 != 0, s1 / std1, 0)  # col 18: CT1 = CT2 / tổng Lượng dùng tiêu chuẩn

    mask_xh2 = df["XH"] == 2
    g3 = df[mask_xh2].groupby(["Ma_so_lenh_tao", "_tt_gop"])
    s3 = g3["Chenh_lech"].transform("sum")
    std3 = g3["Luong_dung_tieu_chuan"].transform("sum")
    df.loc[mask_xh2, "CT4_TongChenhLech"] = s3                # col 19: CT4 = Tổng chênh lệch
    df.loc[mask_xh2, "CT3_PctChenhLech"] = np.where(std3 != 0, s3 / std3, 0)  # col 20: CT3 = CT4 / tổng Lượng dùng tiêu chuẩn

    g5 = df.groupby(["Ma_so_lenh_tao", "_tt_gop", "Ma_NVL"])
    s5 = g5["Chenh_lech"].transform("sum")
    std5 = g5["Luong_dung_tieu_chuan"].transform("sum")
    df["CT6_TongChenhLech"] = s5                               # col 21: CT6 = Tổng chênh lệch
    df["CT5_PctChenhLech"] = np.where(std5 != 0, s5 / std5, 0)  # col 22: CT5 = CT6 / tổng Lượng dùng tiêu chuẩn

    mask_xh3 = df["XH"] == 3
    df["_nvl_pair_key"] = df["Ma_NVL"]
    sub_rows = df["_match_type"] == "substitute"
    df.loc[sub_rows, "_nvl_pair_key"] = df.loc[sub_rows, "_main_nvl"].fillna(df.loc[sub_rows, "Ma_NVL"])
    g7 = df[mask_xh3].groupby(["Ma_so_lenh_tao", "_tt_gop", "_nvl_pair_key"])
    s7 = g7["Chenh_lech"].transform("sum")
    std7 = g7["Luong_dung_tieu_chuan"].transform("sum")
    df.loc[mask_xh3, "CT8_TongChenhLech"] = s7                # col 23: CT8 = Tổng chênh lệch
    df.loc[mask_xh3, "CT7_PctChenhLech"] = np.where(std7 != 0, s7 / std7, 0)  # col 24: CT7 = CT8 / tổng Lượng dùng tiêu chuẩn

    # Các sheet logic dùng điều kiện =0, <>0 và >0. Khử nhiễu dấu phẩy động
    # trước khi hiển thị/đánh giá để các giá trị như 1.42e-14 được hiểu đúng
    # là 0; việc này không thay đổi các ngưỡng nghiệp vụ 3% và 5 đơn vị.
    ct_zero_tolerance = 1e-9
    ct_columns = [
        "CT2_TongChenhLech",
        "CT1_PctChenhLech",
        "CT4_TongChenhLech",
        "CT3_PctChenhLech",
        "CT6_TongChenhLech",
        "CT5_PctChenhLech",
        "CT8_TongChenhLech",
        "CT7_PctChenhLech",
    ]
    for column in ct_columns:
        near_zero = df[column].notna() & (df[column].abs() <= ct_zero_tolerance)
        df.loc[near_zero, column] = 0.0

    # ── Bước 7: LSX không lĩnh liệu ─────────────────────────
    lsx_total = df.groupby("Ma_so_lenh_tao")["SL_dung_thuc"].transform("sum")
    df["LSX_khong_linh_lieu"] = np.where(lsx_total == 0, "LSX không lĩnh liệu", "")

    # ── Bước 8: Chỉ đánh dấu đúng dòng NVL có trên phiếu 5402 ─
    quantity_5402_by_lsx_nvl = _build_5402_quantity_map(invr17)
    df["_5402_flag"] = [
        (str(lsx).strip(), str(material).strip())
        in quantity_5402_by_lsx_nvl
        for lsx, material in zip(df["Ma_so_lenh_tao"], df["Ma_NVL"])
    ]
    df["Phieu_linh_vuot_5402"] = np.where(df["_5402_flag"], df["Ma_so_lenh_tao"], "")

    # ── GHI CHÚ ─────────────────────────────────────────────
    df["Ghi_chu"] = ""
    df.loc[df["_match_type"] == "not_found", "Ghi_chu"] = "KHÔNG TÌM THẤY DỮ LIỆU THAY THẾ"

    # ═══════════════════════════════════════════════════════════
    # ĐÁNH GIÁ: XH=1,2,3,>3 → SP → LSX (theo logic sheet TH)
    # ═══════════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════════
    # ĐÁNH GIÁ THEO CÔNG THỨC EXCEL TỪ SHEET TH
    # ═══════════════════════════════════════════════════════════

    # Chuẩn bị biến
    is_mc = df["DCSX_GC"].fillna("").astype(str).str.strip() == "MC"
    is_dang_sx = df["Tinh_trang_lenh_SX"].fillna("").astype(str).str.strip() == "Đang SX"
    # Phân loại: "Thép" khi Mã NVL bắt đầu bằng "4T", còn lại "Còn lại"
    ploai = np.where(df["Ma_NVL"].fillna("").astype(str).str[:2] == "4T", "Thép", "Còn lại")
    ma_nvl_3 = df["Ma_NVL"].fillna("").astype(str).str[:3]  # 3 ký tự đầu
    has_thaythe = df["NVL_thay_the_flag"].fillna("").str.strip() != ""

    ct1_raw = pd.to_numeric(df["CT1_PctChenhLech"], errors="coerce")
    ct3_raw = pd.to_numeric(df["CT3_PctChenhLech"], errors="coerce")
    ct5_raw = pd.to_numeric(df["CT5_PctChenhLech"], errors="coerce")
    ct6_raw = pd.to_numeric(df["CT6_TongChenhLech"], errors="coerce")
    ct7_raw = pd.to_numeric(df["CT7_PctChenhLech"], errors="coerce")
    ct1 = ct1_raw.fillna(0)                                # col 18: CT1 = %
    ct2 = df["CT2_TongChenhLech"].fillna(0).astype(float)  # col 17: CT2 = Tổng chênh lệch
    ct3 = ct3_raw.fillna(0)                                # col 20: CT3 = %
    ct4 = df["CT4_TongChenhLech"].fillna(0).astype(float)  # col 19: CT4 = Tổng chênh lệch
    ct5 = ct5_raw.fillna(0)                                # col 22: CT5 = %
    ct6 = ct6_raw.fillna(0)                                # col 21: CT6 = Tổng chênh lệch
    ct7 = ct7_raw.fillna(0)                                # col 24: CT7 = %
    ct8 = df["CT8_TongChenhLech"].fillna(0).astype(float)  # col 23: CT8 = Tổng chênh lệch

    # ── XH=1: CT1=%, CT2=Tổng, CT5=%, CT6=Tổng ──────────────
    ktl_xh1 = (
        ~is_mc & (
            (~is_dang_sx & (ploai == "Thép") & (ma_nvl_3 == "4TC") & (ct1.abs() > 0.03) & (ct2.abs() > 5) & (ct5.abs() > 0.03) & (ct6.abs() > 5)) |
            (~is_dang_sx & (ploai == "Thép") & (ma_nvl_3 != "4TC") & (ct5.abs() > 0.03) & (ct6.abs() > 5)) |
            (~is_dang_sx & (ploai == "Còn lại") & (ct6 != 0) & (ct5 != 0)) |
            (is_dang_sx & (ploai == "Thép") & (ct5 > 0.03) & (ct6 > 5)) |
            (is_dang_sx & (ploai == "Còn lại") & (ct6 > 0) & (ct5 > 0))
        )
    )
    xh1_eval = np.where(df["LSX_khong_linh_lieu"].str.strip() != "", "",
               np.where(ktl_xh1, "LSX không đạt", "LSX đạt"))

    # ── XH=2: CT3=%, CT4=Tổng, CT5=%, CT6=Tổng ──────────────
    ktl_xh2_nosub = (
        ~is_mc & (
            # TH1: NOT Đang SX, Thép 4T*, |CT5|>3%, |CT6|>5
            (~is_dang_sx & (ploai == "Thép") & (ct5.abs() > 0.03) & (ct6.abs() > 5)) |
            # TH2: NOT Đang SX, Còn lại, |CT5|≠0
            (~is_dang_sx & (ploai == "Còn lại") & (ct5 != 0)) |
            # TH3: Đang SX, Thép 4T*, CT5>3%, CT6>5
            (is_dang_sx & (ploai == "Thép") & (ct5 > 0.03) & (ct6 > 5)) |
            # TH4: Đang SX, Còn lại, CT5>0
            (is_dang_sx & (ploai == "Còn lại") & (ct5 > 0))
        )
    )
    ktl_xh2_sub = (
        ~is_mc & (
            # TH5: có NVL thay thế, NOT Đang SX: |CT3|>3%, |CT4|>5, |CT5|>3%, |CT6|>5
            (~is_dang_sx & (ct3.abs() > 0.03) & (ct4.abs() > 5)
             & (ct5.abs() > 0.03) & (ct6.abs() > 5)) |
            # TH6: có NVL thay thế, Đang SX, CT3>0
            (is_dang_sx & (ct3 > 0))
        )
    )
    ktl_xh2 = np.where(has_thaythe, ktl_xh2_sub, ktl_xh2_nosub)
    xh2_raw = np.where(ktl_xh2, "LSX không đạt", "LSX đạt")
    ct_err_xh2 = has_thaythe & (ct3_raw.isna() | df["CT4_TongChenhLech"].isna() |
                                 ct5_raw.isna() | df["CT6_TongChenhLech"].isna() |
                                 np.isinf(ct3_raw.fillna(0)) | np.isinf(ct4) |
                                 np.isinf(ct5_raw.fillna(0)) | np.isinf(ct6))
    xh2_eval = np.where(df["LSX_khong_linh_lieu"].str.strip() != "", "",
               np.where(ct_err_xh2, "?", xh2_raw))

    # ── XH=3: CT7=%, CT8=Tổng, CT5=%, CT6=Tổng ──────────────
    ktl_xh3_nosub = ~is_mc & (
        # TH1: NOT Đang SX, Thép 4T*, |CT5|>3%, |CT6|>5
        ~is_dang_sx & (ploai == "Thép") & (ct5.abs() > 0.03) & (ct6.abs() > 5) |
        # TH2: NOT Đang SX, Còn lại, |CT5|≠0
        ~is_dang_sx & (ploai == "Còn lại") & (ct5 != 0) |
        # TH3: Đang SX, Thép 4T*, CT5>3%, CT6>5
        is_dang_sx & (ploai == "Thép") & (ct5 > 0.03) & (ct6 > 5) |
        # TH4: Đang SX, Còn lại, CT5>0
        is_dang_sx & (ploai == "Còn lại") & (ct5 > 0)
    )
    ktl_xh3_sub = (
        ~is_mc & ~is_dang_sx & (
            # TH5: có NVL thay thế, NOT Đang SX, Thép 4T*:
            # |CT7|>3%, |CT8|>5, |CT5|>3%, |CT6|>5.
            (
                (ploai == "Thép")
                & (ct7.abs() > 0.03)
                & (ct8.abs() > 5)
                & (ct5.abs() > 0.03)
                & (ct6.abs() > 5)
            )
            # TH6: có NVL thay thế, NOT Đang SX, không phải 4T*, CT7>0.
            | ((ploai == "Còn lại") & (ct7 > 0))
        )
    )
    ktl_xh3 = np.where(has_thaythe, ktl_xh3_sub, ktl_xh3_nosub)
    xh3_raw = np.where(ktl_xh3, "LSX không đạt", "LSX đạt")
    ct_err_xh3 = has_thaythe & (ct7_raw.isna() | df["CT8_TongChenhLech"].isna() |
                                 ct5_raw.isna() | df["CT6_TongChenhLech"].isna() |
                                 np.isinf(ct7_raw.fillna(0)) | np.isinf(ct8) |
                                 np.isinf(ct5_raw.fillna(0)) | np.isinf(ct6))
    xh3_eval = np.where(df["LSX_khong_linh_lieu"].str.strip() != "", "",
               np.where(ct_err_xh3, "?", xh3_raw))

    # ── XH>3: CT5=%, CT6=Tổng ────────────────────────────────
    ktl_xhgt3 = (
        (~is_dang_sx & (ct6 != 0) & (ct5 != 0)) |
        (is_dang_sx & (ct6 > 0) & (ct5 > 0))
    )
    xhgt3_raw = np.where(ktl_xhgt3, "LSX không đạt", "LSX đạt")
    ct_err_xhgt3 = (
        ct6_raw.isna() | ct5_raw.isna()
        | np.isinf(ct6_raw.fillna(0)) | np.isinf(ct5_raw.fillna(0))
    )
    xhgt3_eval = np.where(df["LSX_khong_linh_lieu"].str.strip() != "", "",
                 np.where(ct_err_xhgt3, "?", xhgt3_raw))

    # Gán vào cột XH (chỉ dòng có XH tương ứng, còn lại để trống)
    df["XH=1"] = np.where(df["XH"] == 1, xh1_eval, "")
    df["XH=2"] = np.where(df["XH"] == 2, xh2_eval, "")
    df["XH=3"] = np.where(df["XH"] == 3, xh3_eval, "")
    df["XH>3"] = np.where(~df["XH"].isin([1, 2, 3]), xhgt3_eval, "")

    # ── SP (AM): chọn theo XH ────────────────────────────────
    df["_xh1_eval"] = xh1_eval
    df["_xh2_eval"] = xh2_eval
    df["_xh3_eval"] = xh3_eval
    df["_xhgt3_eval"] = xhgt3_eval

    df["DA_SP"] = np.select(
        [
            df["XH"] == 1,
            df["XH"] == 2,
            df["XH"] == 3,
            ~df["XH"].isin([1, 2, 3]),
        ],
        [xh1_eval, xh2_eval, xh3_eval, xhgt3_eval],
        default=""
    )

    # ── Phiếu 5402: hiệu chỉnh từng SP/NVL trước khi chốt LSX ─
    # Chỉ dòng Đánh giá trên SP = "LSX không đạt" mới được hiệu chỉnh.
    # Phiếu phải cùng LSX, cùng Mã NVL và:
    # ABS(Tổng SL biến động 5402) = ABS(Chênh lệch lượng dùng), sai số < 0,01.
    (
        df["DA_SP"],
        df["_5402_nvl_match"],
        df["_5402_qty_nvl"],
    ) = _apply_5402_sp_corrections(
        df,
        invr17,
        quantity_map=quantity_5402_by_lsx_nvl,
    )

    # ── LSX (AO): đánh giá lại sau khi hiệu chỉnh từng SP/NVL ─
    lsx_groups = df.groupby("Ma_so_lenh_tao")
    lsx_kll_count = lsx_groups["LSX_khong_linh_lieu"].transform(lambda x: (x.str.strip() != "").sum())
    lsx_dat_count = lsx_groups["DA_SP"].transform(lambda x: (x == "LSX đạt").sum())
    lsx_kd_count = lsx_groups["DA_SP"].transform(lambda x: (x == "LSX không đạt").sum())
    lsx_qm_count = lsx_groups["DA_SP"].transform(lambda x: ((x == "x") | (x == "?")).sum())
    lsx_is_mc = lsx_groups["DCSX_GC"].transform(
        lambda values: values.fillna("").astype(str).str.strip().eq("MC").any()
    )

    # Thứ tự ưu tiên:
    # 1. Có không lĩnh liệu -> toàn LSX không lĩnh liệu.
    # 2. Nếu không, DCSX MC -> toàn LSX đạt.
    # 3. Nếu không, có đạt > 0, không đạt = 0, ? = 0 -> toàn LSX đạt.
    # 4. Còn lại -> toàn LSX không đạt.
    lsx_dat = (
        lsx_is_mc
        | ((lsx_dat_count > 0) & (lsx_kd_count == 0) & (lsx_qm_count == 0))
    )

    df["DA_LSX"] = np.where(
        lsx_kll_count > 0, "LSX không lĩnh liệu",  # Bước 1
        np.where(lsx_dat, "LSX đạt", "LSX không đạt")  # Bước 5
    )

    # ── Output ──────────────────────────────────────────────
    output_mapping = [
        ("XH", "XH"),
        ("DCSX_GC", "DCSX/GC"),
        ("Ma_so_lenh_tao", "Mã số lệnh tạo"),
        ("Ngay_khoi_cong", "Ngày khởi công thực tế"),
        ("Ngay_hoan_tat", "Ngày hoàn tất thực tế"),
        ("Ma_SP", "Mã SP"),
        ("Ten_VP", "Tên VP"),
        ("Tinh_trang_lenh_SX", "Tình trạng lệnh SX"),
        ("SL_du_tinh", "SL dự tính"),
        ("San_luong_thuc_te", "SL thực tế"),
        ("Ma_NVL", "Mã NVL"),
        ("Ten_VP2", "Tên VP2"),
        ("Luong_dung_tieu_chuan", "Lượng dùng tiêu chuẩn"),
        ("SL_dung_thuc", "SL dùng thực"),
        ("Chenh_lech", "Chênh lệch lượng dùng"),
        ("Phan_loai", "Phân loại"),
        ("NVL_thay_the_flag", "NVL thay thế"),
        ("CT2_TongChenhLech", "Công thức 2 (Tổng chênh lệch)"),   # col 17
        ("CT1_PctChenhLech", "Công thức 1 (%)"),                  # col 18
        ("CT4_TongChenhLech", "Công thức 4 (Tổng chênh lệch)"),   # col 19
        ("CT3_PctChenhLech", "Công thức 3 (%)"),                  # col 20
        ("CT6_TongChenhLech", "Công thức 6 (Tổng chênh lệch)"),   # col 21
        ("CT5_PctChenhLech", "Công thức 5 (%)"),                  # col 22
        ("CT8_TongChenhLech", "Công thức 8 (Tổng chênh lệch)"),   # col 23
        ("CT7_PctChenhLech", "Công thức 7 (%)"),                  # col 24
        ("LSX_khong_linh_lieu", "LSX không lĩnh liệu"),
        ("XH=1", "XH=1"),
        ("XH=2", "XH=2"),
        ("XH=3", "XH=3"),
        ("XH>3", "XH>3"),
        ("DA_SP", "ĐÁNH GIÁ TRÊN SP"),
        ("Phieu_linh_vuot_5402", "5402 - Phiếu Lĩnh Liệu"),
        ("DA_LSX", "ĐÁNH GIÁ TRÊN LSX"),
        ("Ghi_chu", "GHI CHÚ"),
    ]

    result = df[[m[0] for m in output_mapping]].copy()
    result.columns = [m[1] for m in output_mapping]
    result = result.reset_index(drop=True)

    return result
