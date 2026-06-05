import re
from pathlib import Path

skill_md = Path('skills/1coos-calendar-cn-1.0.2/SKILL.md')
content = skill_md.read_text(encoding='utf-8')

bash_blocks = []
for match in re.finditer(r'```bash\s+(.*?)\s```', content, re.DOTALL):
    cmd = match.group(1).strip()
    cmd_lines = [l for l in cmd.splitlines() if not l.strip().startswith('# Output:')]
    bash_blocks.append('\n'.join(cmd_lines))

for i, cmd in enumerate(bash_blocks):
    print(f'Block {i}: {repr(cmd[:200])}')
    print()