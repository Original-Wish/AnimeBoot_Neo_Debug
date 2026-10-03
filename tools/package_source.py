# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""核对资源与源码后创建源码ZIP；不编译EXE，不包含本地生成物。"""
import argparse,hashlib,json,zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
EXCLUDE={'.git','.venv','__pycache__','build','dist','.pytest_cache','audit'}

def sha256(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):result.update(chunk)
    return result.hexdigest()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();output=args.out.resolve()
    if output==ROOT or ROOT in output.parents:
        raise SystemExit('请把ZIP输出到工程目录之外，避免把生成包再打包进去。')
    lock=json.loads((ROOT/'assets/components.lock.json').read_text(encoding='utf-8'))
    for name,record in lock['files'].items():
        path=ROOT/name
        if path.stat().st_size!=record['size'] or sha256(path)!=record['sha256']:
            raise SystemExit('资源锁不匹配：'+name)
    files=[]
    for path in ROOT.rglob('*'):
        if not path.is_file():continue
        name=path.relative_to(ROOT)
        if any(part in EXCLUDE for part in name.parts) or path.suffix in ('.pyc','.pyo','.log','.part'):continue
        if path.suffix=='.py':compile(path.read_bytes(),str(name),'exec')
        files.append((path,name.as_posix()))
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path,name in sorted(files,key=lambda item:item[1]):archive.write(path,'AnimeBoot_Neo_Debug/'+name)
    with zipfile.ZipFile(output) as archive:
        names=archive.namelist()
        if len(names)!=len(set(names)) or archive.testzip():raise RuntimeError('ZIP校验失败')
        for name,record in lock['files'].items():
            data=archive.read('AnimeBoot_Neo_Debug/'+name)
            if hashlib.sha256(data).hexdigest()!=record['sha256']:raise RuntimeError('ZIP资源摘要错误：'+name)
    digest=sha256(output)
    output.with_suffix('.sha256').write_text(digest+'  '+output.name+'\n',encoding='utf-8')
    report=dict(version='2610.3',file_count=len(files),locked_assets=len(lock['files']),size=output.stat().st_size,sha256=digest)
    output.with_suffix('.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
