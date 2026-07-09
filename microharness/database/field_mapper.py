"""
Binding field → Database column mapper.
Maps Chinese field names (from binding) to DB table column names.
Each document type has its own table and field mapping.
"""

TABLE_MAP = {
    "入院记录":  {"table": "hdc_userv2.emr_admission_record",  "doc_type": "AdmissionRecord"},
    "出院记录":  {"table": "hdc_userv2.emr_discharge_record",  "doc_type": "DischargeRecord"},
    "门急诊病历":{"table": "hdc_userv2.emr_outpatient_and_emergency", "doc_type": "OutpatientAndEmergency"},
    "首次病程记录":{"table":"hdc_userv2.emr_first_course_record","doc_type":"FirstMedicalRecord"},
    "日常病程记录":{"table":"hdc_userv2.emr_daily_course_record","doc_type":"DailyMedicalRecord"},
    "手术记录":  {"table": "hdc_userv2.emr_surgical_record",   "doc_type": "SurgeryRecord"},
}

# Mapping: Chinese field name (from binding) → DB column name
# Common fields across all document types
COMMON_FIELDS = {
    "姓名":      "patient_name",
    "性别":      "gender",
    "年龄":      "age",
    "病案号":    "medicalno",
    "登记号":    "registerno",
    "就诊号":    "visitnumber",
    "医师签名":  "physician_sign",
}

# Document-specific field mappings
DOC_FIELDS = {
    "入院记录": {
        "主诉":                "chief_complaint",
        "现病史":              "present_illness_history",
        "既往史":              "past_medical_history",
        "个人史":              "social_history",
        "婚育史":              "maritalandobstetric_history",
        "月经史":              "menstrual_history",
        "家族史":              "family_history",
        "体格检查":            "physical_examination",
        "专科情况":            "specific_findings",
        "辅助检查":            "investigations",
        "初步诊断":            "preliminary_diagnosis",
        "中医四诊观察结果":     "tcm_four_findings",
        "入院日期时间":         "admission_time",
        "入院科室":            "admission_depart",
        "民族":                "nation",
        "婚姻状况":            "marital_status",
        "出生地":              "birthplace",
        "职业":                "occupation",
        "记录时间":            "recording_time",
        "上级医师签名":         "attending_physician_sign",
    },
    "出院记录": {
        "入院情况":            "admission_status",
        "入院诊断":            "admission_diagnosis",
        "出院诊断":            "discharge_diagnosis",
        "诊疗经过":            "clinical_course",
        "出院情况":            "discharge_status",
        "出院医嘱":            "discharge_orders",
        "入院日期时间":         "admission_time",
        "出院日期时间":         "discharge_time",
    },
    "手术记录": {
        "手术名称":            "surgical_name",
        "麻醉方法":            "anesthesia_method",
        "手术日期":            "surgery_date",
        "术者":                "surgeon",
        "手术医师":            "surgeon",
        "手术助手":            "surgical_assistants",
        "麻醉医生":            "anesthesiologist",
        "术前诊断":            "pre_op_diagnosis",
        "术中诊断":            "intra_op_diagnosis",
        "手术经过":            "surgical_procedure",
        "术中出现情况及处理":   "intra_op_events",
        "科室":                "department",
        "病床":                "bedno",
        "备注":                "note",
    },
    "门急诊病历": {
        "主诉":                "chief_complaint",
        "现病史":              "present_illness_history",
        "既往史":              "past_medical_history",
        "体格检查":            "physical_examination",
        "中医四诊观察结果":     "tcm_four_findings",
        "辅助检查":            "investigations",
        "过敏史":              "allergies",
        "诊断":                "diagnosis",
        "治疗意见":            "treatment_advice",
        "就诊日期时间":         "admission_datetime",
        "科室":                "department",
        "婚姻状况":            "marital_status",
        "职业":                "occupation",
        "住址":                "address",
        "工作单位":            "company",
    },
    "首次病程记录": {
        "病历特点":            "case_characteristics",
        "诊断依据":            "diagnostic_basis",
        "初步诊断":            "preliminary_diagnosis",
        "鉴别诊断":            "differential_diagnosis",
        "诊疗计划":            "treatment_plan",
        "记录日期时间":         "recording_time",
        "科室":                "department",
        "上级医师签名":         "attending_physician_sign",
    },
    "日常病程记录": {
        "住院病程":            "progress_note",
        "科室":                "department",
        "记录日期时间":         "recording_time",
    },
}


def map_bindings_to_row(doc_title: str, bindings: list, meta: dict) -> dict:
    """
    Convert binding results to a database row dict.

    Args:
        doc_title: Chinese document title (e.g. "出院记录")
        bindings: list of {html_field, value, xml_path} from binding
        meta: extra metadata {register_no, visit_no, global_patient_id, global_visit_id}

    Returns:
        dict ready for INSERT: {column_name: value, ...}
    """
    import re, uuid
    from datetime import datetime as dt

    # businessfieldcode = part before "_" in global_visit_id (e.g., "00001_123" → "00001")
    gvid = meta.get("global_visit_id", "")
    bfc = gvid.split("_")[0] if "_" in gvid else doc_title

    row = {
        "emr_hosdocid": uuid.uuid4().hex[:16],
        "registerno": meta.get("register_no", ""),
        "visitnumber": meta.get("visit_no", ""),
        "papat_relpatientid": meta.get("global_patient_id") or meta.get("register_no", ""),
        "paadm_relvisitnumber": gvid or meta.get("visit_no", ""),
        "businessfieldcode": bfc,
        "t_timestamp": dt.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # Short VARCHAR columns with max lengths
    SHORT_COLS = {"marital_status": 10, "nation": 10, "occupation": 20, "gender": 10,
                  "birthplace": 50, "address": 70, "company": 70,
                  "admission_diagnosis": 100, "discharge_diagnosis": 100,
                  "surgical_name": 80, "anesthesia_method": 100,
                  "physician_sign": 50, "attending_physician_sign": 50,
                  "surgeon": 50, "surgical_assistants": 50, "anesthesiologist": 50,
                  "discharge_depart": 50, "admission_depart": 50, "department": 50,
                  "bedno": 10, "registerno": 50, "visitnumber": 50, "medicalno": 18}

    # Apply common field mapping
    for b in bindings:
        field_name = b.get("html_field") or ""
        raw_val = (b.get("html_value") or b.get("value") or "")
        value = (str(raw_val).strip()) if raw_val is not None else ""
        if not value or value == "None":
            continue

        # Normalize Chinese date formats to SQL standard
        # "2024-09-12 09时43分" → "2024-09-12 09:43:00"
        dm = re.match(r'(\d{4}-\d{2}-\d{2})\s+(\d{2})时(\d{2})分', value)
        if dm:
            value = f"{dm.group(1)} {dm.group(2)}:{dm.group(3)}:00"
        # "2026年06月05日" → "2026-06-05"
        dm2 = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日', value)
        if dm2:
            value = f"{dm2.group(1)}-{int(dm2.group(2)):02d}-{int(dm2.group(3)):02d}"
        # "2026年06月05日 10:30 -- ..." → take only date part
        dm3 = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日.*', value)
        if dm3 and not dm2:
            value = f"{dm3.group(1)}-{int(dm3.group(2)):02d}-{int(dm3.group(3)):02d}"

        # Check common fields
        col = COMMON_FIELDS.get(field_name)
        if col and col not in row:
            max_len = SHORT_COLS.get(col, 100)
            row[col] = value[:max_len]

        # Check doc-specific fields
        doc_map = DOC_FIELDS.get(doc_title, {})
        col = doc_map.get(field_name)
        if col:
            max_len = SHORT_COLS.get(col, 5000)
            row[col] = value[:max_len]

    return row


def get_table_for_doc(doc_title: str) -> str:
    """Get DB table name for a document type."""
    info = TABLE_MAP.get(doc_title, {})
    return info.get("table", doc_title)


def find_db_column(doc_title: str, section_name: str) -> str:
    """Fuzzy match a section name to a DB column name.
    E.g., '入院日期' → 'admission_time', '出院日期' → 'discharge_time'
    """
    doc_map = DOC_FIELDS.get(doc_title, {})
    # Exact match
    if section_name in doc_map:
        return doc_map[section_name]
    # Contains match
    for key, col in doc_map.items():
        if section_name in key or key in section_name:
            return col
    # Common fields
    if section_name in COMMON_FIELDS:
        return COMMON_FIELDS[section_name]
    return ""
