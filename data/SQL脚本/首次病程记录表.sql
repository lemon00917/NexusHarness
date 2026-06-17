CREATE TABLE hdc_userv2.emr_first_course_record (
    doc_id VARCHAR(64) NOT NULL,               -- 文档唯一ID（主键）
    registerno VARCHAR(50),                    -- 登记号
    visitnumber VARCHAR(50),                   -- 就诊号
    medicalno VARCHAR(18),                     -- 病案号
    patient_name VARCHAR(50),                 -- 患者姓名
    gender VARCHAR(10),                        -- 性别
    age VARCHAR(8),                            -- 年龄
    department VARCHAR(50),                    -- 科室
    case_characteristics TEXT,                 -- 病例特点（原LONGVARCHAR）
    diagnostic_basis TEXT,                     -- 诊断依据
    preliminary_diagnosis TEXT,                -- 初步诊断
    differential_diagnosis TEXT,               -- 鉴别诊断
    treatment_plan TEXT,                       -- 诊疗计划
    physician_sign VARCHAR(50),                -- 医师签名
    attending_physician_sign VARCHAR(50),      -- 上级医师签名
    recording_time TIMESTAMP(3),               -- 记录日期时间
    businessfieldcode VARCHAR(10),             -- 数据域
    t_timestamp TIMESTAMP(3) NOT NULL,         -- 时间戳字段
    papat_relpatientid VARCHAR(50) NOT NULL,    -- 全局患者id
    paadm_relvisitnumber VARCHAR(50) NOT NULL,  -- 全局就诊id
    -- 主键约束
    CONSTRAINT pk_emr_first_course_docid PRIMARY KEY (doc_id)
);

-- 表注释
COMMENT ON TABLE hdc_userv2.emr_first_course_record IS '首次病程记录表';

-- 字段注释
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.doc_id IS '文档唯一ID';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.registerno IS '登记号';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.visitnumber IS '就诊号';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.medicalno IS '病案号';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.patient_name IS '患者姓名';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.gender IS '性别';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.age IS '年龄';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.department IS '科室';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.case_characteristics IS '病例特点';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.diagnostic_basis IS '诊断依据';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.preliminary_diagnosis IS '初步诊断';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.differential_diagnosis IS '鉴别诊断';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.treatment_plan IS '诊疗计划';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.physician_sign IS '医师签名';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.attending_physician_sign IS '上级医师签名';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.recording_time IS '记录日期时间';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.businessfieldcode IS '数据域';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.t_timestamp IS '时间戳字段';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.papat_relpatientid IS '全局患者id';
COMMENT ON COLUMN hdc_userv2.emr_first_course_record.paadm_relvisitnumber IS '全局就诊id';

-- CDR业务查询索引（与入院/日常病程/出院表统一）
CREATE INDEX idx_emr_first_patientid ON hdc_userv2.emr_first_course_record(papat_relpatientid);
CREATE INDEX idx_emr_first_visitid ON hdc_userv2.emr_first_course_record(paadm_relvisitnumber);
CREATE INDEX idx_emr_first_rec_time ON hdc_userv2.emr_first_course_record(recording_time);
CREATE INDEX idx_emr_first_medicalno ON hdc_userv2.emr_first_course_record(medicalno);