# -*- coding: utf-8 -*-
import requests

model = 'qwen2.5:7b'

medical_record = '''入院记录：
姓名：吴秀荣
性别：女
年龄：56岁
主诉：胸背部疼痛不适疼痛3月，加重1月。

现病史：患者自诉约于2023年11月发现左侧乳腺肿块...

辅助检查：2024-09-12 腰椎MR 1，胸12椎体压缩骨折，考虑病理性骨折；2，胸11，腰3椎体及附件，腰2椎体，腰1附件内多发异常信号，转移瘤？其他待排...

初步诊断：胸椎骨折T12

手术：入院后完善检查，择期行"经皮椎体球囊扩张成形术"
住院时间：2024-09-12入院，2024-09-18出院，住院6天。'''

from microharness.ollama.prompts import JUDGE_SYSTEM_PROMPT, JUDGE_USER_PROMPT

questions = [
    '该患者住院天数是否超过7天？',
    '2024年9月之前是否发现乳腺肿块？',
    '该患者是否患有恶性肿瘤？是否做过手术？住院多久？',
]

for q in questions:
    user_prompt = JUDGE_USER_PROMPT.format(condition=q, record_content=medical_record)

    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': JUDGE_SYSTEM_PROMPT},
            {'role': 'user', 'content': user_prompt}
        ],
        'temperature': 0.1,
        'stream': False
    }
    try:
        resp = requests.post('http://localhost:11434/api/chat', json=payload, timeout=60)
        result = resp.json()['message']['content'].strip()
        print(f'Q: {q}')
        print(f'A: {result}')
        print()
    except Exception as e:
        print(f'Q: {q}')
        print(f'Error: {e}')
        print()
