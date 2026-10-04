"""EnglishCoach: local Markdown storage and review tools. No AI/API dependency."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=8))
BEGIN = '<!-- EnglishCoach managed:start -->'
END = '<!-- EnglishCoach managed:end -->'
STATE = re.compile(r'```json\n(.*?)\n```', re.S)
SCENARIOS = [
    ('项目汇报', 'Your manager asks for a short project update. Explain the goal, progress, one blocker and your next step.'),
    ('AI 项目介绍', 'Introduce a supplier-document AI agent to a nontechnical manager. Explain the problem, its value and how people review its output.'),
    ('领导沟通', 'You need your manager to decide between two project priorities. Explain the trade-off and ask for a clear decision.'),
    ('会议讨论', 'A colleague proposes launching a pilot immediately. Share your view, ask a clarifying question and suggest a next step.'),
    ('供应商沟通', 'A supplier has missed a document deadline. Ask politely for an update and agree on a new delivery date.'),
    ('工作总结', 'Tell a colleague what you accomplished this week, what you learned and what you will do next week.'),
]

def now():
    return datetime.now(TZ)

def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Visible .tmp names let Obsidian's watcher observe the final rename.
    fd, tmp = tempfile.mkstemp(prefix='englishcoach-write-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if path.read_text(encoding='utf-8') != text:
            raise OSError('Write verification failed')
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

@contextmanager
def lock(base):
    # OS advisory lock: released even if the process crashes. File is not a DB.
    path = base / '.englishcoach.lock'
    f = path.open('a+b')
    f.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise ValueError('另一项保存正在进行，请稍后重试。')
    try:
        if os.fstat(f.fileno()).st_size == 0:
            f.write(b'0')
            f.flush()
        yield
    finally:
        try:
            if os.name == 'nt':
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            f.close()

def render(state):
    kind = state['kind']
    lines = [f"# {state['title']}", '']
    if kind == 'session':
        lines += [f"状态：{state['status']} · 模块：{state['module']} · 角色：{state['persona']}",
                  f"记录来源：{state['source']} · 完整度：{state['completeness']}", '',
                  f"场景：{state['prompt']}", '']
        for t in state['turns']:
            lines += [f"## 第 {t['id']} 轮", '', '**你的表达**', '', t['user'], '',
                      '**教练回应**', '', t['coach'], '',
                      f"提示使用：{'是' if t.get('hints') else '否'}", '']
        if state.get('summary'):
            lines += ['## 本次收获', '', state['summary'], '']
    else:
        lines += [state['content'], '', f"下次复习：{state.get('due', '—')}", '']
    lines += ['<details><summary>工具状态（请勿直接修改）</summary>', '', '```json',
              json.dumps(state, ensure_ascii=False, indent=2), '```', '', '</details>']
    return '\n'.join(lines) + '\n'

def document(state):
    body = render(state)
    meta = {'cssclasses': ['englishcoach'], 'ec_id': state['id'], 'ec_kind': state['kind'],
            'ec_version': state['version'], 'ec_hash': digest(body)}
    header = '\n'.join(f'{k}: {json.dumps(v, ensure_ascii=False)}' for k, v in meta.items())
    return f'---\n{header}\n---\n\n{BEGIN}\n{body}{END}\n\n## 我的补充\n\n'

def read(path):
    text = path.read_text(encoding='utf-8')
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise ValueError(f'管理区标记有变化，保留原文件并停止：{path.name}')
    body = text.split(BEGIN + '\n', 1)[1].split(END, 1)[0]
    match = re.search(r'^ec_hash: "([a-f0-9]+)"$', text, re.M)
    if not match or digest(body) != match[1]:
        raise ValueError(f'管理区被手动编辑，保留原文件并停止：{path.name}')
    states = STATE.findall(body)
    return json.loads(states[-1]), text

def update(path, state, expected):
    old, text = read(path)
    if old['version'] != expected:
        raise ValueError('版本已变化，请重新读取后再保存。')
    state['version'] = expected + 1
    body = render(state)
    prefix, rest = text.split(BEGIN + '\n', 1)
    suffix = rest.split(END, 1)[1]
    prefix = re.sub(r'^ec_version: .*$', f"ec_version: {state['version']}", prefix, flags=re.M)
    prefix = re.sub(r'^ec_hash: .*$', f'ec_hash: "{digest(body)}"', prefix, flags=re.M)
    atomic(path, prefix + BEGIN + '\n' + body + END + suffix)

class Coach:
    def __init__(self, vault):
        self.vault = Path(vault).resolve()
        if not (self.vault / '.obsidian').is_dir():
            raise ValueError('Vault 不可访问或缺少 .obsidian；未保存，请恢复连接后重试。')
        self.base = self.vault / 'EnglishCoach'
        self.base.mkdir(exist_ok=True)

    def states(self, kind=None):
        result = []
        for folder in ('Sessions', 'Journal', 'Expressions', 'Mistakes'):
            for p in (self.base / folder).rglob('*.md'):
                s, _ = read(p)
                if kind is None or s['kind'] == kind:
                    result.append((p, s))
        return result

    def find(self, identity):
        if not re.fullmatch(r'[a-z0-9-]{1,80}', identity):
            raise ValueError('无效记录 ID')
        matches = [(p, s) for p, s in self.states() if s['id'] == identity]
        if len(matches) != 1:
            raise ValueError('找不到唯一记录 ID')
        return matches[0]

    def init(self):
        for folder in ('Sessions', 'Journal', 'Expressions', 'Mistakes', 'Templates', 'Reviews', 'Weekly', 'Scenarios', 'Assets'):
            (self.base / folder).mkdir(exist_ok=True)
        defaults = {
            'Profile.md': '# 我的英语成长档案\n\n职业方向：采购数字化、AI Agent、项目管理。\n\n目标：职场沟通、雅思口语、用英语表达生活。\n\n默认 15 分钟；低精力日 3–5 分钟。\n\n## 待了解\n\n当前水平、雅思目标与时间、喜欢的称呼。\n',
            'Companion.md': '# 你的学习伙伴 Momo\n\n![[EnglishCoach/Assets/cat-welcome.svg|200]]\n\n我会陪你慢慢说。今天的一句话，也值得被记住。\n\n完成练习后更新小猫状态；中断不会扣分。\n',
            'Templates/Session.md': '# 练习记录\n\n场景 → 我的表达 → 教练回应 → 再次表达 → 总结。\n\n工具自动生成正式记录。个人补充请写在“我的补充”下。\n',
        }
        for name, content in defaults.items():
            p = self.base / name
            if not p.exists():
                atomic(p, '---\ncssclasses: [englishcoach]\n---\n\n' + content)
        for i, (title, prompt) in enumerate(SCENARIOS, 1):
            p = self.base / 'Scenarios' / f'{i:02d}.md'
            if not p.exists():
                atomic(p, f'# {title}\n\n{prompt}\n\n追问：原因、风险、下一步、需要谁支持。\n')
        for p in (ROOT / 'assets').glob('*.svg'):
            target = self.base / 'Assets' / p.name
            if not target.exists():
                shutil.copyfile(p, target)
        snippet = self.vault / '.obsidian' / 'snippets' / 'englishcoach.css'
        css = (ROOT / 'assets' / 'englishcoach.css').read_text(encoding='utf-8')
        if snippet.exists() and snippet.read_text(encoding='utf-8') != css:
            raise ValueError('同名 CSS 已有不同内容，未覆盖；请人工核对。')
        atomic(snippet, css)
        self.home()
        return {'ok': True, 'home': str(self.base / 'Home.md'), 'css': '设置 → 外观 → CSS 代码片段 → 启用 englishcoach'}

    def context(self):
        states = self.states()
        today = now().date().isoformat()
        sessions = [s for _, s in states if s['kind'] == 'session']
        return {'profile': (self.base / 'Profile.md').read_text(encoding='utf-8'),
                'unfinished': [s for s in sessions if s['status'] != 'completed'],
                'recent': sorted(sessions, key=lambda s: s['created'], reverse=True)[:3],
                'due': [s for _, s in states if s['kind'] == 'card' and s['due'] <= today][:3],
                'today_scenario': SCENARIOS[now().date().toordinal() % len(SCENARIOS)]}

    def start(self, data):
        module = data.get('module', 'work')
        if module not in ('work', 'ielts', 'journal'):
            raise ValueError('module 必须是 work/ielts/journal')
        identity = data.get('id') or ('s-' + now().strftime('%Y%m%d-%H%M%S') + '-' + os.urandom(3).hex())
        if not re.fullmatch(r's-[a-z0-9-]{1,75}', identity):
            raise ValueError('无效 session ID')
        if any(s['id'] == identity for _, s in self.states()):
            raise ValueError('记录 ID 已存在，请读取并继续')
        folder = 'Journal' if module == 'journal' else 'Sessions'
        path = self.base / folder / now().strftime('%Y/%m') / f'{identity}.md'
        state = dict(id=identity, kind='session', version=1, created=now().isoformat(),
                     title=data.get('title', '今日英语练习'), module=module,
                     persona=data.get('persona', 'friend' if module == 'journal' else 'coach'),
                     source=data.get('source', 'codex-text'), completeness=data.get('completeness', 'partial'),
                     prompt=data.get('prompt', SCENARIOS[now().date().toordinal() % 6][1]),
                     status='in_progress', turns=[], summary='')
        atomic(path, document(state))
        self.home()
        return {'path': str(path), 'state': state}

    def append(self, data):
        p, s = self.find(data['id'])
        if s['status'] == 'completed':
            raise ValueError('练习已结束')
        turn = data['turn']
        if not isinstance(turn.get('id'), int) or turn['id'] < 1:
            raise ValueError('turn.id 必须为正整数')
        for field in ('user', 'coach'):
            if not isinstance(turn.get(field), str):
                raise ValueError(f'turn.{field} 必须为文本')
        for old in s['turns']:
            if old['id'] == turn['id']:
                if old == turn:
                    return {'ok': True, 'duplicate': True, 'state': s}
                raise ValueError('轮次 ID 冲突，未覆盖')
        if turn['id'] != len(s['turns']) + 1:
            raise ValueError('轮次必须连续')
        s['turns'].append(turn)
        update(p, s, data['version'])
        self.home()
        return {'ok': True, 'state': s}

    def finish(self, data):
        p, s = self.find(data['id'])
        if s['status'] == 'completed':
            if s['summary'] != data['summary']:
                raise ValueError('已结束的总结不同，未覆盖')
            self.home()
            return {'ok': True, 'duplicate': True, 'state': s}
        s['summary'] = data['summary']
        s['status'] = 'completed'
        s['completeness'] = data.get('completeness', s['completeness'])
        s['finished'] = now().isoformat()
        update(p, s, data['version'])
        self.home()
        return {'ok': True, 'path': str(p), 'state': s}

    def card(self, data):
        _, session = self.find(data['session_id'])
        if session['kind'] != 'session':
            raise ValueError('来源必须是练习')
        identity = 'c-' + digest(data['session_id'] + '\n' + data['content'])[:20]
        existing = [(p, s) for p, s in self.states() if s['id'] == identity]
        if existing:
            return {'ok': True, 'duplicate': True, 'state': existing[0][1]}
        cards = [s for _, s in self.states() if s['kind'] == 'card' and s['session_id'] == session['id']]
        if len(cards) >= 3:
            raise ValueError('每次练习最多沉淀 3 张卡片')
        folder = 'Mistakes' if data.get('category') == 'mistake' else 'Expressions'
        s = dict(id=identity, kind='card', version=1, title=data['title'], content=data['content'],
                 session_id=session['id'], due=(now().date()+timedelta(days=1)).isoformat(), reviews=[])
        atomic(self.base / folder / f'{identity}.md', document(s))
        self.home()
        return {'ok': True, 'state': s}

    def review(self, data):
        p, s = self.find(data['id'])
        if s['kind'] != 'card' or data['result'] not in ('again', 'hard', 'good', 'easy'):
            raise ValueError('需要卡片 ID 和 again/hard/good/easy')
        for previous in s['reviews']:
            if previous['id'] == data['event_id']:
                if previous['result'] != data['result']:
                    raise ValueError('复习事件 ID 冲突，未覆盖')
                return {'ok': True, 'duplicate': True, 'state': s}
        successes = sum(r['result'] in ('good', 'easy') for r in s['reviews'])
        gap = 1 if data['result'] in ('again', 'hard') else [3, 7, 14, 30][min(successes, 3)]
        s['reviews'].append(dict(id=data['event_id'], date=now().isoformat(), result=data['result']))
        s['due'] = (now().date()+timedelta(days=gap)).isoformat()
        update(p, s, data['version'])
        self.home()
        return {'ok': True, 'state': s}

    def redact(self, data):
        p, s = self.find(data['id'])
        if s['kind'] != 'session':
            raise ValueError('需要练习 ID')
        ids = data['turn_ids']
        if not isinstance(ids, list) or not ids or any(not isinstance(i, int) for i in ids):
            raise ValueError('需要明确要排除的轮次 ID 列表')
        if not set(ids).issubset({t['id'] for t in s['turns']}):
            raise ValueError('轮次不存在')
        # Clear all session-derived material: a summary/card can incorporate any turn.
        for t in s['turns']:
            if t['id'] in ids:
                turn_id = t['id']
                t.clear()
                t.update(id=turn_id, user='[此轮按要求不留存]', coach='[衍生反馈已清除]', hints=False)
        s['summary'] = ''
        s['prompt'] = '[隐私清理后移除场景文本]'
        s['title'] = '英语练习（已清理私人内容）'
        s['completeness'] = 'partial'
        s['status'] = 'summary_pending'
        update(p, s, data['version'])
        removed = []
        for card_path, card in self.states('card'):
            if card['session_id'] == s['id']:
                card_path.unlink()
                removed.append(card['id'])
        self.weekly()
        self.home()
        return {'ok':True, 'state':s, 'removed_cards':removed,
                'scope':'Local managed content only; personal supplements and external sync/version histories require separate review.'}

    def home(self):
        states = self.states()
        sessions = sorted([s for _, s in states if s['kind']=='session'], key=lambda s:s['created'], reverse=True)
        complete = [s for s in sessions if s['status']=='completed']
        active = [s for s in sessions if s['status']!='completed']
        due = [s for _, s in states if s['kind']=='card' and s['due']<=now().date().isoformat()][:3]
        latest = complete[0] if complete else None
        pet = 'completed' if latest and latest.get('finished', '').startswith(now().date().isoformat()) else ('encourage' if active else 'welcome')
        body = ['# Your English Garden', '', '> 每一句真实的表达，都在慢慢长成你的英语。', '',
                f'![[EnglishCoach/Assets/cat-{pet}.svg|220]]', '',
                '## 今天，想怎么开始？', '',
                '> [!tip] 在 Codex 中说', '> 开始今天的练习 / 今天想聊日记 / 练雅思口语 / 继续上次练习', '',
                '[[EnglishCoach/Profile|我的档案]] · [[EnglishCoach/Companion|认识 Momo]]', '',
                f'**已完成 {len(complete)} 次练习** · 今天待复习 {len(due)} 张', '', '## 最近的脚印', '']
        for p, s in sorted([(p,s) for p,s in states if s['kind']=='session'], key=lambda x:x[1]['created'], reverse=True)[:5]:
            body.append(f"- [[{p.relative_to(self.vault).with_suffix('').as_posix()}|{s['created'][:10]} · {s['title']}]] · {s['status']}")
        if not sessions:
            body.append('第一片叶子，等你说出第一句话。')
        body += ['', '## 温柔复习', '']
        for s in due:
            p, _ = self.find(s['id'])
            body.append(f"- [[{p.relative_to(self.vault).with_suffix('').as_posix()}|{s['title']}]]")
        if not due:
            body.append('今天没有到期卡片，可以轻松开始。')
        body += ['', '## 我的书架', '', '[[EnglishCoach/Weekly/Overview|成长回顾]] · [[EnglishCoach/Templates/Session|记录说明]]', '']
        path = self.base / 'Home.md'
        body = '\n'.join(body)
        if path.exists():
            old = path.read_text(encoding='utf-8')
            if old.count(BEGIN)!=1 or old.count(END)!=1:
                raise ValueError('首页管理标记变化，未覆盖；练习保存可能已完成，请读取核对。')
            prefix, rest = old.split(BEGIN,1)
            atomic(path, prefix + BEGIN + '\n' + body + END + rest.split(END,1)[1])
        else:
            atomic(path, f'---\ncssclasses: [englishcoach, englishcoach-home]\n---\n\n{BEGIN}\n{body}{END}\n\n## 我的书桌便签\n\n')

    def weekly(self):
        sessions = [s for _, s in self.states('session') if s['created'][:10] >= (now().date()-timedelta(days=6)).isoformat()]
        completed = [s for s in sessions if s['status']=='completed']
        # Descriptive evidence, never invented fluency scores.
        text = '# 最近七天的成长\n\n' + f'完成练习：{len(completed)} 次。\n\n'
        for s in completed:
            text += f"## {s['title']} · {s['created'][:10]}\n\n{s['summary']}\n\n"
        text += '尚未进行同题前后对比时，不推断流利度提升。\n'
        p = self.base / 'Weekly' / 'Overview.md'
        if p.exists():
            old = p.read_text(encoding='utf-8')
            if BEGIN not in old or END not in old:
                raise ValueError('回顾页管理标记变化，未覆盖')
            prefix, rest = old.split(BEGIN,1)
            text = prefix + BEGIN + '\n' + text + END + rest.split(END,1)[1]
        else:
            text = '---\ncssclasses: [englishcoach]\n---\n\n' + BEGIN + '\n' + text + END + '\n\n## 我的感想\n\n'
        atomic(p, text)
        return {'ok': True, 'path': str(p)}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init','context','start','append','finish','card','review','redact','home','weekly','read'])
    parser.add_argument('--vault')
    parser.add_argument('--input', help='UTF-8 JSON payload; use ignored runtime/ for private data')
    parser.add_argument('--id')
    args = parser.parse_args()
    try:
        config = json.loads((ROOT/'local.settings.json').read_text(encoding='utf-8')) if (ROOT/'local.settings.json').exists() else {}
        coach = Coach(args.vault or config.get('vault',''))
        data = json.loads(Path(args.input).read_text(encoding='utf-8-sig')) if args.input else {}
        with lock(coach.base):
            if args.command == 'read':
                p,s=coach.find(args.id); result={'path':str(p),'state':s}
            elif args.command in ('start','append','finish','card','review','redact'):
                result=getattr(coach,args.command)(data)
            else:
                result=getattr(coach,args.command)() or {'ok':True}
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(json.dumps({'ok':False,'error':str(exc),'saved':'unconfirmed; read the record before retry'},ensure_ascii=False),file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    sys.exit(main())
