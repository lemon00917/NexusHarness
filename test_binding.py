import sys
sys.stdout.reconfigure(encoding='utf-8')
from microharness.rag.template_binding import TwoStageBinder

html_file = 'C:/Users/Administrator/Desktop/文档下载2026-5-22 17_33_21/3x3052531x1(外科入院记录_3_880_19).html'

binder = TwoStageBinder(
    stage1_model='qwen2.5:7b',
    stage2_model='qwen2.5:7b',
    xml_dir='D:/work/develop/AI/NexusHarness/data/临床文档模板'
)
result = binder.bind_file(html_file)

if result:
    print('HTML:', result.html_file)
    print('Template:', result.xml_template)
    print('Bindings:', len(result.field_bindings))
    for b in result.field_bindings[:10]:
        print(f'  {b.html_field} -> {b.xml_path}')
else:
    print('Failed')
