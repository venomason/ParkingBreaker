"""Reuse the existing benchmark with the current production role routing.
Default planning and self-test do not construct providers. No alternative app runtime.
"""
from __future__ import annotations
import argparse,hashlib,json,os,subprocess,sys,time
from dataclasses import asdict
from datetime import UTC,date,datetime
from decimal import Decimal
from pathlib import Path
from parkingbreaker.bench.cases import parse_answer_key
from parkingbreaker.bench.cli import (RunDeps,CountingSearcher,WorkItem,run_bench,save_page,redact,_secrets)
from parkingbreaker.bench.runner import run_case
from parkingbreaker.dotgov_source import load_dotgov_snapshot
from parkingbreaker.llm import Usage,LlmBudgetExceeded
from parkingbreaker.research import extractor,issuer_assessment,link_assessment
PROFILE={'intake':'gemini-3.8-flash','research':'deepseek-v4-pro','search':'brave','max_output_tokens':8192,'response_format':'json_object','stream':False,'sdk_retries':0,'thinking':'not explicitly sent; provider default, not a pinned reasoning mode','text_only':True,'maximum_intake_calls_per_run':7,'maximum_research_calls_per_run':3,'maximum_search_calls_per_run':6}
class RoutedClient:
 def __init__(self,intake,research,ledger=None):
  self.intake=intake;self.research=research;self.ledger=ledger
  self.spent={'intake':0,'research':0};self.calls={'intake':0,'research':0}
 def record(self,value):
  if self.ledger:
   with self.ledger.open('a') as f:f.write(json.dumps(value)+'\n');f.flush();os.fsync(f.fileno())
 def complete_json(self,system,user,schema,*,max_cost_cents):
  if system in {issuer_assessment.SYSTEM_PROMPT,link_assessment.SYSTEM_PROMPT}:role='intake';client=self.intake;budget=25;limit=7
  elif system==extractor.SYSTEM_PROMPT:role='research';client=self.research;budget=50;limit=3
  else:raise ValueError('Unreviewed prompt cannot select a provider')
  if self.calls[role]>=limit:raise RuntimeError('Model call-count ceiling reached; campaign stopped')
  ceiling=min(max_cost_cents,budget-self.spent[role])
  if ceiling<=0:raise LlmBudgetExceeded('Qualification phase budget exhausted')
  self.calls[role]+=1
  self.record({'event':'reserved','role':role,'call':self.calls[role],'cents':ceiling})
  try:answer,usage=client.complete_json(system,user,schema,max_cost_cents=ceiling)
  except LlmBudgetExceeded:
   self.record({'event':'cancelled_before_transport','role':role});raise
  except Exception:
   self.spent[role]+=ceiling;self.record({'event':'unknown_cost_stop','role':role,'retained_cents':ceiling})
   raise RuntimeError('Model outcome/cost unknown; campaign stopped without retry') from None
  self.spent[role]+=usage.cost_cents
  self.record({'event':'settled','role':role,'cents':usage.cost_cents,'phase_total':self.spent[role]})
  if usage.cost_cents>ceiling:raise RuntimeError('Provider reported overrun; campaign stopped')
  return answer,usage

def worker(item):
 from parkingbreaker.llm_config import build_llm_client
 from parkingbreaker.brave_search import BraveSearcher
 from parkingbreaker.http_fetcher import HttpFetcher
 row,=parse_answer_key(item.row_json)
 client=RoutedClient(build_llm_client(PROFILE['intake'],os.environ),build_llm_client(PROFILE['research'],os.environ),Path(item.pages_dir).parent/'model-reservations.jsonl')
 deps=RunDeps(HttpFetcher(),CountingSearcher(BraveSearcher(os.environ)),client,lambda:datetime.now(UTC),time.monotonic,time.sleep,load_dotgov_snapshot(Path(item.snapshot_path)),item.preset,item.commit,row.key_version,evidence_dir=item.pages_dir,evidence_writer=save_page)
 record=json.loads(run_case(row,date.fromisoformat(item.run_date),deps).to_json())
 return json.dumps(redact(record,_secrets(os.environ)))
def process(item):
 r=subprocess.run([sys.executable,'-B',str(Path(__file__).resolve()),'--worker'],input=json.dumps(asdict(item)),text=True,capture_output=True)
 if r.returncode:raise RuntimeError('Qualification worker stopped; no automatic retry and no raw provider output retained')
 return r.stdout
def qualify_private_scope(row):
 from parkingbreaker.bench.cases import build_issuer_query,build_scripted_answers
 from parkingbreaker.bench.fakes import fake_seams
 from parkingbreaker.prehold import PreholdDeps,PreholdRequest,run_prehold
 from parkingbreaker.service_policy import IssuerKind,STATE_BASIS,SERVICE_CONTROL
 today=date.today();answers=build_scripted_answers(row,today)
 _,_,_,snapshot=fake_seams(row,today)
 class ForbiddenEffect:
  def __getattr__(self,name):
   def refuse(*a,**k):raise AssertionError('Private-scope check touched a provider boundary')
   return refuse
 boundary=ForbiddenEffect();results=[]
 for repeat in range(1,4):
  result=run_prehold(PreholdRequest(build_issuer_query(row,today),row.notice.stage_text,IssuerKind.PRIVATE,answers.notice_issue_date,answers.printed_deadline,today),PreholdDeps(snapshot,boundary,boundary,boundary,boundary,lambda:datetime.now(UTC),25,STATE_BASIS,SERVICE_CONTROL),launch_allowlist=None)
  assert result.issuer is None and result.support.family.value=='BUSINESS_EXCLUSION'
  results.append({'case_id':row.case_id,'repeat':repeat,'input_issuer_kind':'PRIVATE','family':result.support.family.value,'provider_calls':0})
 return {'passed':True,'scope':'actual private prehold; no government-issuer research surrogate','results':results}
class Recorder:
 def __init__(self):self.calls=0
 def complete_json(self,*a,**k):self.calls+=1;return {},Usage()
def main():
 p=argparse.ArgumentParser();p.add_argument('--execute',action='store_true');p.add_argument('--self-test',action='store_true');p.add_argument('--worker',action='store_true');p.add_argument('--cases',type=Path);p.add_argument('--snapshot',type=Path);p.add_argument('--out',type=Path);p.add_argument('--cap-usd',type=Decimal,default=Decimal(62));a=p.parse_args()
 if a.worker:
  print(worker(WorkItem(**json.load(sys.stdin))));return
 if a.self_test:
  i=Recorder();r=Recorder();c=RoutedClient(i,r)
  for system in [issuer_assessment.SYSTEM_PROMPT,link_assessment.SYSTEM_PROMPT,extractor.SYSTEM_PROMPT]:c.complete_json(system,'',{},max_cost_cents=1)
  assert i.calls==2 and r.calls==1
  try:c.complete_json('unreviewed','',{},max_cost_cents=1)
  except ValueError:pass
  else:raise AssertionError('unknown route accepted')
  print(json.dumps({'routing_self_test_passed':True,'provider_called':False}));return
 if not a.execute:
  print(json.dumps({'mode':'offline_plan','profile':PROFILE,'required':'reviewed signed 23-row combined key, clean backend source, current dated DotGov snapshot, provider tier/privacy and exact capped run authority','repeats':3,'maximum_cases':23,'scheduled_research_runs':66,'private_scope_checks':3,'accounting_cap_usd':str(a.cap_usd),'provider_called':False}));return
 if Path('/home/ghostmonday/Vault/Ops/STOP').exists():raise RuntimeError('STOP is set')
 if not all([a.cases,a.snapshot,a.out]):p.error('--execute needs --cases, --snapshot and a new --out')
 rows=parse_answer_key(a.cases.read_text())
 if len(rows)!=23 or {r.case_id for r in rows}!={*(f'B{i:02}' for i in range(1,18)),*(f'S{i:02}' for i in range(1,7))}:raise RuntimeError('Expected reviewed 17 national and 6 SF rows')
 if any(not r.owner_signed_at or not r.reviewed_by or not r.reviewed_at for r in rows):raise RuntimeError('Owner-reviewed signatures missing; no provider constructed')
 if not Decimal(0)<a.cap_usd<=Decimal(62):raise RuntimeError('Campaign cap must be at most the proposed 62 USD; exact authority still required')
 if a.out.exists():raise RuntimeError('Use a fresh campaign directory; never resume an uncertain paid run automatically')
 a.out.mkdir(parents=True)
 (a.out/'role-profile.json').write_text(json.dumps({'profile':PROFILE,'cases_sha256':hashlib.sha256(a.cases.read_bytes()).hexdigest(),'snapshot_sha256':hashlib.sha256(a.snapshot.read_bytes()).hexdigest(),'scope':'research-only production seams; does not bypass intake-age, image review, packet approval or provider/target qualification','report_preset_label':'deepseek-v4-pro labels research model; issuer/delegation calls use Gemini as recorded here'},indent=2)+'\n')
 private=qualify_private_scope(next(r for r in rows if r.case_id=='S05'))
 (a.out/'private-scope.json').write_text(json.dumps(private,indent=2)+'\n')
 result=run_bench(preset='deepseek-v4-pro',cases=a.cases,case_ids=[r.case_id for r in rows if r.case_id!='S05'],repeat=3,out=a.out,cap_usd=a.cap_usd,search='brave',snapshot=a.snapshot,execute=process)
 print(json.dumps(asdict(result),default=str))
if __name__=='__main__':
 try:main()
 except Exception as e:
  print(json.dumps({'stopped':True,'reason':type(e).__name__,'detail':str(e) if isinstance(e,RuntimeError) else 'Input, source or campaign check failed; no raw credential-bearing error echoed'}));raise SystemExit(1)
