"""检查 Git 跟踪文件或发布归档；仅报告文件名与风险类型。"""
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(r"(?:^|/)(?:data|logs|runtime|\.venv|__pycache__|\.git|source)(?:/|$)|(?:^|/)\.env(?:$|\.)|\.(?:db|sqlite|bin|pyc)$", re.I)
SECRETS = re.compile(r"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----)")
LOCAL_PATH = re.compile(r"[CD]:[\\/](?:Users[\\/](?!Public)|05_)", re.I)

def inspect(name, data):
    name = name.replace('\\', '/')
    problems = []
    if FORBIDDEN.search(name):
        problems.append('私人目录或运行数据')
    if '0922_界面示意_' in name:
        problems.append('已撤下的旧图片')
    if Path(name).suffix.lower() in {'.py', '.md', '.txt', '.json', '.yml', '.yaml', '.ps1', '.cmd', '.nuspec'}:
        text = data.decode('utf-8-sig', errors='replace')
        if SECRETS.search(text):
            problems.append('疑似凭据')
        if LOCAL_PATH.search(text):
            problems.append('本机个人路径')
    for problem in problems:
        print(f'{name}: {problem}')
    return len(problems)

def main():
    errors = 0
    count = 0
    if len(sys.argv) > 1:
        for argument in sys.argv[1:]:
            with zipfile.ZipFile(argument) as archive:
                for item in archive.infolist():
                    if not item.is_dir():
                        errors += inspect(item.filename, archive.read(item))
                        count += 1
    else:
        paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode('utf-8').split('\0')
        for name in filter(None, paths):
            errors += inspect(name, (ROOT / name).read_bytes())
            count += 1
    print(f'审查文件数={count}；风险命中数={errors}')
    return bool(errors)

if __name__ == '__main__':
    sys.exit(main())
