CREATE TABLE hdc_userv2.emr_outpatient_and_emergency (
    doc_id VARCHAR(64) NOT NULL,               -- 文档唯一ID（主键）
    registerno VARCHAR(50),                    -- 登记号
    patient_name VARCHAR(50),                 -- 患者姓名
    gender VARCHAR(10),                        -- 性别
    age VARCHAR(8),                            -- 年龄
    birthdate DATE,                            -- 出生日期
    marital_status VARCHAR(10),                -- 婚姻状况
    occupation VARCHAR(20),                    -- 职业
    address VARCHAR(70),                       -- 住址
    company VARCHAR(70),                       -- 工作单位
    visitnumber VARCHAR(50),                   -- 就诊号
    admission_datetime TIMESTAMP(3),           -- 就诊日期时间
    department VARCHAR(50),                    -- 科室名称
    chief_complaint TEXT,                      -- 主诉
    present_illness_history TEXT,              -- 现病史
    past_medical_history TEXT,                 -- 既往史
    physical_examination TEXT,                 -- 体格检查
    tcm_four_findings TEXT,                    -- 中医"四诊"观察结果
    investigations TEXT,                       -- 辅助检查结果
    allergies TEXT,                            -- 过敏史
    diagnosis TEXT,                            -- 诊断
    treatment TEXT,                            -- 治疗意见
    physician_sign VARCHAR(50),                -- 医师签名
    businessfieldcode VARCHAR(10),             -- 数据域
    papat_relpatientid VARCHAR(50) NOT NULL,    -- 全局患者id
    paadm_relvisitnumber VARCHAR(50) NOT NULL, -- 全局就诊id
    t_timestamp TIMESTAMP(3) NOT NULL,         -- 时间戳字段
    -- 主键约束
    CONSTRAINT pk_emr_outpatient_emergency_docid PRIMARY KEY (doc_id)
);

-- 表注释
COMMENT ON TABLE hdc_userv2.emr_outpatient_and_emergency IS '门急诊病历表';

-- 字段注释
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.doc_id IS '文档唯一ID';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.registerno IS '登记号';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.patient_name IS '患者姓名';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.gender IS '性别';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.age IS '年龄';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.birthdate IS '出生日期';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.marital_status IS '婚姻状况';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.occupation IS '职业';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.address IS '住址';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.company IS '工作单位';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.visitnumber IS '就诊号';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.admission_datetime IS '就诊日期时间';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.department IS '科室名称';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.chief_complaint IS '主诉';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.present_illness_history IS '现病史';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.past_medical_history IS '既往史';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.physical_examination IS '体格检查';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.tcm_four_findings IS '中医"四诊"观察结果';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.investigations IS '辅助检查结果';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.allergies IS '过敏史';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.diagnosis IS '诊断';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.treatment IS '治疗意见';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.physician_sign IS '医师签名';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.businessfieldcode IS '数据域';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.papat_relpatientid IS '全局患者id';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.paadm_relvisitnumber IS '全局就诊id';
COMMENT ON COLUMN hdc_userv2.emr_outpatient_and_emergency.t_timestamp IS '时间戳字段';

-- CDR业务查询索引（与住院病历表统一索引规范）
CREATE INDEX idx_emr_outpatient_patientid ON hdc_userv2.emr_outpatient_and_emergency(papat_relpatientid);
CREATE INDEX idx_emr_outpatient_visitid ON hdc_userv2.emr_outpatient_and_emergency(paadm_relvisitnumber);
CREATE INDEX idx_emr_outpatient_visit_time ON hdc_userv2.emr_outpatient_and_emergency(admission_datetime);
CREATE INDEX idx_emr_outpatient_department ON hdc_userv2.emr_outpatient_and_emergency(department);