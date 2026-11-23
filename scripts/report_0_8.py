"""Compute an audit report strictly from actual JUnit records and environment."""
import importlib.metadata
import hashlib
import json
from pathlib import Path
import platform
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
FILES={'baseline':'baseline_0_8.xml','lifecycle':'lifecycle_0_8.xml','ledger':'ledger_0_8.xml'}

def parse(path):
    doc=ET.parse(path).getroot()
    suite=doc.find('testsuite') if doc.tag=='testsuites' else doc
    if suite is None: raise ValueError('no junit testsuite')
    out={k:int(suite.attrib[k]) for k in ('tests','failures','errors','skipped')}
    out['passed']=out['tests']-out['failures']-out['errors']-out['skipped']
    out['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    return out

def main():
    parsed={name:parse(ROOT/'evidence'/filename) for name,filename in FILES.items()}
    totals={key:sum(x[key] for x in parsed.values()) for key in ('tests','passed','failures','errors','skipped')}
    try: installed=importlib.metadata.version('mcp')
    except importlib.metadata.PackageNotFoundError:installed=None
    report={
      'report_name':'python-first-mcp-0.8.0-multiworker',
      'python_version':platform.python_version(),
      'platform':platform.platform(),
      'suites':parsed,'totals':totals,
      'official_sdk':{'installed_version':installed,'wire_tests_executed':0 if installed is None else None,
                      'wire_tests_skipped':totals['skipped'], 'status':'not_executed' if installed is None else 'inspect_junit'},
      'verified_scope':['local Python and SQLite','separate Python subprocess ownership','stubbed MCP handler','process supervision'],
      'unverified_scope':['actual MCP client/server','formal proofs','remote changes','malicious local database writer','wall-clock fault handling'],
      'public_release':'BLOCKED',
    }
    assert totals['failures']==0 and totals['errors']==0
    assert totals['skipped']==10 and installed is None, 'SDK status changed: reevaluate claims'
    (ROOT/'evidence'/'verification_0_8.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(totals,ensure_ascii=False))
if __name__=='__main__':main()
