-- 创建入院记录表 emr_admission_record
CREATE TABLE hdc_userv2.emr_admission_record (
    doc_id VARCHAR(64) NOT NULL,               -- 文档唯一ID（主键）
    registerno VARCHAR(50),                    -- 登记号
    visitnumber VARCHAR(50),                   -- 就诊号
    medicalno VARCHAR(18),                     -- 病案号
    patient_name VARCHAR(50),                 -- 患者姓名
    gender VARCHAR(10),                       -- 性别
    age VARCHAR(8),                            -- 年龄
    nation VARCHAR(10),                        -- 民族
    marital_status VARCHAR(10),                -- 婚姻状况
    birthplace VARCHAR(50),                    -- 出生地
    occupation VARCHAR(20),                    -- 职业类别
    admission_time TIMESTAMP(3),               -- 入院日期时间
    admission_depart VARCHAR(50),              -- 入院科室
    chief_complaint TEXT,                      -- 主诉
    present_illness_history TEXT,              -- 现病史
    past_medical_history TEXT,                -- 既往史
    social_history TEXT,                       -- 个人史
    maritalandobstetric_history TEXT,          -- 婚育史
    menstrual_history TEXT,                     -- 月经史
    family_history TEXT,                       -- 家族史
    physical_examination TEXT,                 -- 体格检查
    specific_findings TEXT,                    -- 专科情况
    investigations TEXT,                       -- 辅助检查结果
    tcm_four_findings TEXT,                    -- 中医"四诊"观察结果
    preliminary_diagnosis TEXT,                -- 初步诊断
    physician_sign VARCHAR(50),                -- 医师签名
    recording_time TIMESTAMP(3),               -- 记录时间
    businessfieldcode VARCHAR(10),             -- 数据域
    t_timestamp TIMESTAMP(3) NOT NULL,         -- 时间戳字段
    papat_relpatientid VARCHAR(50) NOT NULL,    -- 全局患者id
    paadm_relvisitnumber VARCHAR(50) NOT NULL,  -- 全局就诊id
    -- 主键约束
    CONSTRAINT pk_emr_admission_record_docid PRIMARY KEY (doc_id)
);

-- 表注释
COMMENT ON TABLE hdc_userv2.emr_admission_record IS '入院记录表';

-- 字段注释批量添加
COMMENT ON COLUMN hdc_userv2.emr_admission_record.doc_id IS '文档唯一ID';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.registerno IS '登记号';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.visitnumber IS '就诊号';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.medicalno IS '病案号';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.patient_name IS '患者姓名';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.gender IS '性别';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.age IS '年龄';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.nation IS '民族';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.marital_status IS '婚姻状况';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.birthplace IS '出生地';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.occupation IS '职业类别';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.admission_time IS '入院日期时间';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.admission_depart IS '入院科室';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.chief_complaint IS '主诉';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.present_illness_history IS '现病史';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.past_medical_history IS '既往史';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.social_history IS '个人史';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.maritalandobstetric_history IS '婚育史';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.menstrual_history IS '月经史';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.family_history IS '家族史';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.physical_examination IS '体格检查';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.specific_findings IS '专科情况';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.investigations IS '辅助检查结果';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.tcm_four_findings IS '中医"四诊"观察结果';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.preliminary_diagnosis IS '初步诊断';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.physician_sign IS '医师签名';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.recording_time IS '记录时间';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.businessfieldcode IS '数据域';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.t_timestamp IS '时间戳字段';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.papat_relpatientid IS '全局患者id';
COMMENT ON COLUMN hdc_userv2.emr_admission_record.paadm_relvisitnumber IS '全局就诊id';

-- 常用业务索引（CDR查询优化，按需启用）
-- 1. 患者全局ID索引（按患者检索入院记录）
CREATE INDEX idx_emr_adm_patientid ON hdc_userv2.emr_admission_record(papat_relpatientid);
-- 2. 全局就诊号索引（单就诊维度查询）
CREATE INDEX idx_emr_adm_visitid ON hdc_userv2.emr_admission_record(paadm_relvisitnumber);
-- 3. 入院时间索引（时间范围筛选）
CREATE INDEX idx_emr_adm_admtime ON hdc_userv2.emr_admission_record(admission_time);
-- 4. 病案号索引（院内病案检索）
CREATE INDEX idx_emr_adm_medicalno ON hdc_userv2.emr_admission_record(medicalno);