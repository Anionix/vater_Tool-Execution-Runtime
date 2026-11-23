#!/usr/bin/env python3
"""IMPORT-001: planned -> dry-run -> remote-check -> create -> (close if verified).
Requires GitHub CLI `gh` and explicit `--execute` for network writes.
"""
import argparse,json,pathlib,re,subprocess,sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
def run(*parts):
 result=subprocess.run(['gh',*parts],capture_output=True,text=True)
 if result.returncode:raise RuntimeError(result.stderr.strip())
 return result.stdout
def main():
 a=argparse.ArgumentParser();a.add_argument('--repo',required=True)
 a.add_argument('--execute',action='store_true');a.add_argument('--all',action='store_true')
 a.add_argument('--limit',type=int,default=25);args=a.parse_args()
 if not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',args.repo):a.error('invalid owner/repo')
 if args.limit<1 or args.limit>351:a.error('invalid limit')
 candidates=[json.loads(s) for s in (ROOT/'backlog'/'github_issues.jsonl').read_text(encoding='utf-8').splitlines() if s]
 if not args.all:candidates=[x for x in candidates if x['kind']=='gate' or x['phase']=='trust' or x['task_id']=='task.scope.015']
 if not args.execute:
  print(json.dumps({'mode':'dry-run','repo':args.repo,'total_candidates':len(candidates),'first':[x['task_id'] for x in candidates[:args.limit]]},ensure_ascii=False,indent=2));return
 previous={x['title'] for x in json.loads(run('issue','list','--repo',args.repo,'--state','all','--limit','1000','--json','title'))}
 missing=[x for x in candidates if x['title'] not in previous]
 for x in missing[:args.limit]:
  url=run('issue','create','--repo',args.repo,'--title',x['title'],'--body',x['body']).strip()
  if x['status']=='done':run('issue','close',url,'--repo',args.repo,'--reason','completed')
  print(x['task_id'],url,flush=True)
 print(json.dumps({'created':min(args.limit,len(missing)),'existing':len(candidates)-len(missing),'pending':max(0,len(missing)-args.limit)}))
if __name__=='__main__':main()
