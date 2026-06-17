-- Schema：hdc_userv2，表名：emr_daily_course_record，表描述：日常病程记录表
CREATE TABLE hdc_userv2.emr_daily_course_record (
    doc_id VARCHAR(64) NOT NULL,               -- 文档唯一ID（主键）
    registerno VARCHAR(50),                    -- 登记号
    visitnumber VARCHAR(50),                   -- 就诊号
    medicalno VARCHAR(18),                     -- 病案号
    patient_name VARCHAR(50),                 -- 患者姓名
    gender VARCHAR(10),                        -- 性别
    age VARCHAR(8),                            -- 年龄
    department VARCHAR(50),                    -- 科室
    recording_time TIMESTAMP(3),               -- 记录日期时间
    progress_note TEXT,                        -- 住院病程（原LONGVARCHAR大文本）
    physician_sign VARCHAR(50),                -- 医师签名
    businessfieldcode VARCHAR(10),             -- 数据域
    t_timestamp TIMESTAMP(3) NOT NULL,         -- 时间戳字段
    papat_relpatientid VARCHAR(50) NOT NULL,    -- 全局患者id
    paadm_relvisitnumber VARCHAR(50) NOT NULL, -- 全局就诊id
    -- 主键约束
    CONSTRAINT pk_emr_daily_course_docid PRIMARY KEY (doc_id)
);

-- 表注释
COMMENT ON TABLE hdc_userv2.emr_daily_course_record IS '日常病程记录表';

-- 字段注释
COMMENT ON COLUMN hdc_userv2.emr_daily_course_record.doc_id IS '文档唯一ID';
COMMENT ON COLUMN hdc_userv2.emr_daily_course_record.registerno IS '登记号';
COMMENT ON COLUMN hdc_userv2.emr_daily_course_record.visitnumber IS '就诊号';
COMMENT ON COLUMN hdc_userv2.emr_daily_course_record.medicalno IS '病案号';
COMMENT ON COLUMN hdc_userv2.emr_daily_course_record.patient_name IS '患者姓名';
COMMENT ON COLUMN hdc_userv2.emr_daily_course_record.gender IS '性别';
COMMENT ON COLUMN hdc_userv2.emr_daily_course_record.age IS '年龄';
COMMENT ON COLUMN hdc_userv2.emr_daily_course_record.department IS '科室';
COMMENT ON COLUMN hdc_userv2.emr_daily_course_record.recording_time IS '记录日期时间';
COMMENT ON COLUMN hdc_userv2.emr_daily_course_record.progress_note IS '住院病程';
COMMENT ON COLUMN hdc_userv2.emr_daily_course_record.physician_sign IS '医师签名';
COMMENT ON COLUMN hdc_userv2.emr_daily_course_record.businessfieldcode IS '数据域';
COMMENT ON COLUMN hdc_userv2.emr_daily_course_record.t_timestamp IS '时间戳字段';
COMMENT ON COLUMN hdc_userv2.emr_daily_course_record.papat_relpatientid IS '全局患者id';
COMMENT ON COLUMN hdc_userv2.emr_daily_course_record.paadm_relvisitnumber IS '全局就诊id';

-- CDR查询优化索引
CREATE INDEX idx_emr_daily_patientid ON hdc_userv2.emr_daily_course_record(papat_relpatientid);
CREATE INDEX idx_emr_daily_visitid ON hdc_userv2.emr_daily_course_record(paadm_relvisitnumber);
CREATE INDEX idx_emr_daily_rec_time ON hdc_userv2.emr_daily_course_record(recording_time);
CREATE INDEX idx_emr_daily_medicalno ON hdc_userv2.emr_daily_course_record(medicalno);