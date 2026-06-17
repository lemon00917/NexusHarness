CREATE TABLE hdc_userv2.emr_discharge_record (
    doc_id VARCHAR(64) NOT NULL,               -- 文档唯一ID（主键）
    registerno VARCHAR(50),                    -- 登记号
    visitnumber VARCHAR(50),                   -- 就诊号
    medicalno VARCHAR(18),                     -- 病案号
    patient_name VARCHAR(50),                 -- 患者姓名
    gender VARCHAR(10),                        -- 性别
    age VARCHAR(8),                            -- 年龄
    discharge_depart VARCHAR(50),              -- 出院科室
    admission_time TIMESTAMP(3),               -- 入院日期时间
    discharge_time TIMESTAMP(3),              -- 出院日期时间
    admission_status TEXT,                     -- 入院情况（原LONGVARCHAR）
    admission_diagnosis VARCHAR(100),          -- 入院诊断
    discharge_diagnosis VARCHAR(100),          -- 出院诊断
    clinical_course TEXT,                      -- 诊疗经过（原LONGVARCHAR）
    discharge_status TEXT,                     -- 出院情况（原LONGVARCHAR）
    discharge_orders TEXT,                     -- 出院医嘱（原LONGVARCHAR）
    physician_sign VARCHAR(50),                -- 医师签名
    businessfieldcode VARCHAR(10),             -- 数据域
    t_timestamp TIMESTAMP(3) NOT NULL,         -- 时间戳字段
    papat_relpatientid VARCHAR(50) NOT NULL,    -- 全局患者id
    paadm_relvisitnumber VARCHAR(50) NOT NULL,  -- 全局就诊id
    -- 主键约束
    CONSTRAINT pk_emr_discharge_record_docid PRIMARY KEY (doc_id)
);

-- 表注释
COMMENT ON TABLE hdc_userv2.emr_discharge_record IS '出院记录表';

-- 字段注释批量定义
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.doc_id IS '文档唯一ID';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.registerno IS '登记号';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.visitnumber IS '就诊号';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.medicalno IS '病案号';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.patient_name IS '患者姓名';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.gender IS '性别';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.age IS '年龄';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.discharge_depart IS '出院科室';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.admission_time IS '入院日期时间';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.discharge_time IS '出院日期时间';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.admission_status IS '入院情况';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.admission_diagnosis IS '入院诊断';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.discharge_diagnosis IS '出院诊断';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.clinical_course IS '诊疗经过';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.discharge_status IS '出院情况';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.discharge_orders IS '出院医嘱';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.physician_sign IS '医师签名';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.businessfieldcode IS '数据域';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.t_timestamp IS '时间戳字段';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.papat_relpatientid IS '全局患者id';
COMMENT ON COLUMN hdc_userv2.emr_discharge_record.paadm_relvisitnumber IS '全局就诊id';

-- CDR业务查询索引（与入院/病程表对齐）
CREATE INDEX idx_emr_discharge_patientid ON hdc_userv2.emr_discharge_record(papat_relpatientid);
CREATE INDEX idx_emr_discharge_visitid ON hdc_userv2.emr_discharge_record(paadm_relvisitnumber);
CREATE INDEX idx_emr_discharge_time ON hdc_userv2.emr_discharge_record(discharge_time);
CREATE INDEX idx_emr_discharge_medicalno ON hdc_userv2.emr_discharge_record(medicalno);