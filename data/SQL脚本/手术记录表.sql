CREATE TABLE hdc_userv2.emr_surgical_record (
    doc_id VARCHAR(64) NOT NULL,               -- 文档唯一ID（主键）
    registerno VARCHAR(50),                    -- 登记号
    visitnumber VARCHAR(50),                   -- 就诊号
    medicalno VARCHAR(18),                     -- 病案号
    patient_name VARCHAR(50),                 -- 患者姓名
    gender VARCHAR(10),                        -- 性别
    age VARCHAR(8),                            -- 年龄
    department VARCHAR(50),                    -- 科室
    bedno VARCHAR(10),                         -- 床号
    surgery_date TIMESTAMP(3),                 -- 手术日期
    pre_op_diagnosis TEXT,                     -- 术前诊断
    intra_op_diagnosis TEXT,                   -- 术中诊断
    surgical_name VARCHAR(80),                 -- 手术名称
    surgeon VARCHAR(50),                       -- 手术医师
    surgical_assistants VARCHAR(50),           -- 助手名称
    anesthesia_method VARCHAR(100),            -- 麻醉方法
    anesthesiologist VARCHAR(50),              -- 麻醉医生
    surgical_procedure TEXT,                   -- 手术经过
    intra_op_events TEXT,                      -- 术中出现的情况及处理
    note TEXT,                                 -- 备注
    surgeon_sign VARCHAR(50),                  -- 术者签名
    businessfieldcode VARCHAR(10),             -- 数据域
    t_timestamp TIMESTAMP(3) NOT NULL,         -- 时间戳字段
    papat_relpatientid VARCHAR(50) NOT NULL,    -- 全局患者id
    paadm_relvisitnumber VARCHAR(50) NOT NULL,  -- 全局就诊id
    -- 主键约束
    CONSTRAINT pk_emr_surgical_record_docid PRIMARY KEY (doc_id)
);

-- 表注释
COMMENT ON TABLE hdc_userv2.emr_surgical_record IS '手术记录表';

-- 字段注释批量定义
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.doc_id IS '文档唯一ID';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.registerno IS '登记号';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.visitnumber IS '就诊号';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.medicalno IS '病案号';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.patient_name IS '患者姓名';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.gender IS '性别';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.age IS '年龄';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.department IS '科室';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.bedno IS '床号';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.surgery_date IS '手术日期';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.pre_op_diagnosis IS '术前诊断';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.intra_op_diagnosis IS '术中诊断';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.surgical_name IS '手术名称';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.surgeon IS '手术医师';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.surgical_assistants IS '助手名称';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.anesthesia_method IS '麻醉方法';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.anesthesiologist IS '麻醉医生';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.surgical_procedure IS '手术经过';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.intra_op_events IS '术中出现的情况及处理';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.note IS '备注';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.surgeon_sign IS '术者签名';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.businessfieldcode IS '数据域';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.t_timestamp IS '时间戳字段';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.papat_relpatientid IS '全局患者id';
COMMENT ON COLUMN hdc_userv2.emr_surgical_record.paadm_relvisitnumber IS '全局就诊id';

-- CDR业务查询索引（与全病历表统一索引规范）
CREATE INDEX idx_emr_surgical_patientid ON hdc_userv2.emr_surgical_record(papat_relpatientid);
CREATE INDEX idx_emr_surgical_visitid ON hdc_userv2.emr_surgical_record(paadm_relvisitnumber);
CREATE INDEX idx_emr_surgical_op_time ON hdc_userv2.emr_surgical_record(surgery_date);
CREATE INDEX idx_emr_surgical_medicalno ON hdc_userv2.emr_surgical_record(medicalno);
CREATE INDEX idx_emr_surgical_dept ON hdc_userv2.emr_surgical_record(department);