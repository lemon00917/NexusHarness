# -*- coding: utf-8 -*-
import requests
import json

models = [
    'qwen2:1.5b',
    'qwen2.5:3b',
    'qwen2.5:7b',
    'qwen2:7b-instruct',
    'llama3:8b',
    'medaibase/medgemma1.5:4b',
]

questions = [
    ('Q1', '该患者是否年龄超过50岁且未患有癌症？'),
    ('Q2', '该患者是否患有恶性肿瘤？是否做过手术？住院多久？'),
    ('Q3', '该患者是否年龄超过50岁且患有癌症？'),
]

medical_record = '''入院记录：
姓名：吴秀荣
性别：女
年龄：56岁
主诉：胸背部疼痛不适疼痛3月，加重1月。

辅助检查：2024-09-12 腰椎MR 1，胸12椎体压缩骨折，考虑病理性骨折；2，胸11，腰3椎体及附件，腰2椎体，腰1附件内多发异常信号，转移瘤？其他待排...

初步诊断：胸椎骨折T12

治疗：入院后完善检查，择期手术。'''

prompt_system = '你是一个医疗病历分析助手。请根据病历内容回答问题。回答要简洁准确。回答只输出"符合"或"不符合"，不要解释。'

for model in models:
    print(f'\n{"="*60}')
    print(f'模型: {model}')
    print(f'{"="*60}')
    for qid, q in questions:
        payload = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': prompt_system},
                {'role': 'user', 'content': f'病历：{medical_record}\n\n问题：{q}\n\n回答：'}
            ],
            'temperature': 0.1,
            'stream': False
        }
        try:
            resp = requests.post('http://localhost:11434/api/chat', json=payload, timeout=60)
            result = resp.json()['message']['content'].strip()
            print(f'\n{qid}: {q}')
            print(f'A: {result[:100]}')
        except Exception as e:
            print(f'\n{qid}: Error - {e}')
